## Context

Ver `proposal.md` para la motivación. Lo que condiciona el diseño:

- El camino real es `collector (FluentBit) -> ingest -> spool -> pipeline -> detector -> Postgres`. `ingest` nombra cada lote `batch-{time_ns}-{seq}-{n}` y el pipeline lo borra recién después de persistirlo: una caída entre el commit y el borrado re-ejecuta el lote (at-least-once).
- El pipeline es de una sola réplica por diseño (`brokerless-log-transport` D5).
- Del lado de la aplicación, verificado en su código:
  - `anomalyId` es cualquier `Guid` no vacío y es la clave de idempotencia.
  - Una anomalía con `analyzedAt < ingestedAt` se rechaza como `INVALID`.
  - Una ventana de latencia se identifica por `(source, instanceId, stage, windowStart)` y reportarla de nuevo la **reemplaza**, no la suma.
  - Un 400 solo sale de campos del lote (modelo, `source`, tamaño) o de JSON que no deserializa. Una anomalía inválida se rechaza sola, con 200.
  - La etapa de análisis es de todo log analizado, no solo de las anomalías.

## Goals / Non-Goals

**Goals:**
- Que los tres tiempos de una alerta (análisis, alerta, total) y la serie de análisis de la KPI card salgan de instantes medidos.
- Que una aplicación caída, un reinicio del pipeline o un reinicio del puente no pierdan ni dupliquen anomalías.
- No tocar el contrato de la aplicación ni el esquema de wire de los collectors.

**Non-Goals:**
- Entrega exactly-once. Se entrega al menos una vez y la aplicación descarta lo repetido.
- Medir el tiempo que una línea pasó en el disco del collector durante una caída.
- Exponer los descartes por shed a la aplicación.

## Decisions

### D1: Outbox en Postgres más el puente, no reporte directo desde el pipeline

El pipeline solo escribe en su base, en la transacción que ya hace por lote. El puente lee de ahí y es el único proceso que habla con la aplicación.

**Alternativa descartada: que el pipeline haga el `POST`** (lo de PR #9, mudado a `pipeline.py`). Lo no confirmado viviría en memoria: un reinicio dejaría anomalías en Postgres que la aplicación nunca ve, y los timeouts contra una aplicación caída frenarían el lazo que drena el spool hasta disparar backpressure sobre los collectors. Para que fuera correcto habría que persistir lo pendiente, que es esta decisión con más código.

### D2: `ingested_at` es la recepción en `ingest`, leída del nombre del lote

El `time_ns` del nombre del archivo de spool ya es ese instante. No cambia `ingest` ni el wire.

**Alternativas descartadas:**
- *`agent_ts`*: es opcional en el wire y lo sella un reloj que no controlamos. Un nodo con el reloj adelantado daría `analyzedAt < ingestedAt` y la aplicación rechazaría sus anomalías.
- *Lectura del lote por el pipeline*: esconde la espera en el spool, que es lo que crece cuando el detector no da abasto y lo que la métrica tiene que mostrar.

Coincide con TDD_0016: "`ingestedAt` es la entrada del log a la plataforma".

### D3: `analyzed_at` se sella por tanda del detector

Los registros resueltos por caché se sellan al consultarla; los que van al modelo, cuando vuelve su tanda de `/score`. Sellar una vez por lote de spool sería más simple, pero un lote de hasta 1000 registros con el modelo a unos 3 s cada 32 líneas tarda mucho más que una tanda, y el error iría entero a la métrica.

### D4: `anomaly_id = uuid5(namespace, "{lote}/{posición}")`

La posición es el índice del registro en el archivo de spool, antes de descartar nada. La columna lleva índice único y la inserción de filas no colapsadas pasa a `ON CONFLICT DO NOTHING`.

- Recrear el volumen de Postgres no repite ids: el nombre del lote lleva `time_ns`.
- Re-ejecutar un lote da los mismos ids, así que no duplica filas (hoy sí las duplica).
- Una fila colapsada conserva el id del primer ejemplar: el upsert no lo toca.

**Alternativas descartadas:** `uuid5` del `id` de la fila (lo actual: al recrear la base los ids reinician y la aplicación responde `DUPLICATE` a anomalías nuevas) y `uuid4` al marcar la línea (PR #9: no sobrevive a la re-ejecución).

Las filas anteriores a este change quedan con `anomaly_id` nulo; para ellas el puente sigue usando la derivación vieja, así lo ya enviado sigue siendo `DUPLICATE`.

### D5: Las muestras de latencia se guardan por minuto y por lote, solo inserción

Una fila por `(minuto de analyzed_at, lote de spool)` con cantidad, suma, máximo y los 20 conteos, clave primaria sobre ese par e inserción con `ON CONFLICT DO NOTHING`, en la misma transacción que las anomalías del lote.

- Re-ejecutar un lote no cuenta sus muestras dos veces.
- No hace falta sumar arreglos en SQL: el puente trae las filas de un minuto y las suma en Python.

**Alternativa descartada: agregador en memoria que reporta directo** (PR #5). Tras un reinicio el pipeline reportaría el mismo minuto con menos muestras y la aplicación reemplazaría la ventana buena por la parcial.

Los límites de bucket y la asignación `(inferior, superior]` se toman de `tools/latency.py` de PR #5.

### D6: El puente reenvía la ventana completa de cada minuto que cambió

Recorre las filas de latencia nuevas con un cursor, junta los minutos que tocaron, suma todas las filas de esos minutos y manda una ventana por minuto. Como la aplicación reemplaza, reenviar un minuto que recibió muestras tardías o que sigue abierto es seguro. El `instanceId` es fijo y configurable (`BRIDGE_INSTANCE_ID`), no uno por arranque: con uno distinto por arranque, el mismo minuto quedaría registrado dos veces.

Las filas de latencia ya enviadas se borran pasada una hora. Una muestra cae en el minuto en que terminó su análisis, así que un minuto deja de recibir filas a los pocos instantes; pasada una hora, ningún reenvío necesita rearmarlo. Medido en la demo con 5 agentes: unas 10.000 filas por hora de unos 400 bytes, así que retener 7 días eran unos 700 MB y una hora son unos 4 MB.

### D7: Los cursores viven en la base del pipeline

Una tabla con una fila por cursor (anomalías, latencia). Con el cursor en otro volumen, recrear solo la base lo deja por encima del id máximo y el puente no reenvía nada, sin error.

### D8: Un 400 detiene el puente en ese lote

No avanza el cursor, registra el cuerpo de la respuesta y reintenta en el siguiente ciclo. Un 400 es un problema de configuración (revisión vacía, token de otro entorno) que se repite en todos los lotes: saltearlo descarta todas las anomalías hasta que alguien lo note.

### D9: `bridge` en `docker-compose.yml`, detrás del perfil `bridge`

El stack del pipeline sigue levantando sin la aplicación. El overlay de la demo activa el perfil y le suma la red de la aplicación y el archivo de credenciales de los collectors.

## Risks / Trade-offs

- **Una entrega atrasada agrupa mal en la aplicación.** Las ventanas de sus reglas se miden sobre el instante de recepción: cinco `Failed password` repartidos en horas, entregados juntos tras una caída, abren un "SSH Brute Force". → No se mitiga acá; se informa al equipo de la aplicación. Pasa igual con cualquier diseño que reintente.
- **Cuando el pipeline descarta por shed, la latencia mejora.** Lo descartado no es muestra. → Se documenta en `ARCHITECTURE.md` que la métrica se lee junto a `discarded_by_reason` de `/status`.
- **Las filas colapsadas subinforman la evidencia.** Una alerta muestra una anomalía donde hubo muchas repeticiones de la misma plantilla. → Aceptado. Las líneas de seguridad no se colapsan (bypass), y las reglas sembradas de la aplicación solo coinciden con esas.
- **Re-ejecutar un lote sigue sumando `occurrences` de más** en las filas colapsadas. → Aceptado, ya era así.
- **`podman-compose` y los perfiles.** La demo corre con Podman rootless. → Verificar en la tarea de compose; si no los soporta, el servicio queda en el overlay como hoy.
- **Relojes de `ingest` y del pipeline.** Corren en el mismo host, así que `analyzed_at >= ingested_at` salvo un ajuste de reloj hacia atrás. → Si pasa, `analyzed_at` se iguala a `ingested_at` antes de guardar.

## Migration Plan

Las columnas y tablas se crean con `IF NOT EXISTS` al arrancar el pipeline. El puente arranca con los cursores en 0: reenvía lo que haya en `anomalies` y la aplicación responde `DUPLICATE` a lo ya conocido. El volumen `bridge-state` deja de usarse. Para volver atrás alcanza con desplegar la versión anterior: las columnas nuevas admiten nulos y nadie más las lee.

## Open Questions

- ¿`integrated-demo` se fusiona a `main` antes que este change? Solo cambia hacia dónde apunta el PR.
