## 1. Pipeline: instantes e identidad

- [x] 1.1 En `services/collector/store.py`, agregar a `anomalies` las columnas `anomaly_id UUID`, `ingested_at` y `analyzed_at` con `ADD COLUMN IF NOT EXISTS` y un índice único sobre `anomaly_id`; verificar que `ensure_schema()` corre dos veces seguidas sin error sobre una base con datos previos
- [x] 1.2 En `services/collector/app.py`, pasar a `Pipeline.process` el nombre del lote de spool y su instante de recepción (el `time_ns` del nombre); verificar con un lote de prueba que el instante coincide con el del nombre del archivo
- [x] 1.3 En `services/collector/pipeline.py`, conservar la posición de cada registro en el lote y sellar `analyzed_at` al consultar la caché o al volver cada tanda del detector, sin dejarlo por debajo de `ingested_at`; verificar con el scorer simulado que dos tandas reciben instantes distintos
- [x] 1.4 Generar `anomaly_id` como `uuid5` de `{lote}/{posición}` y escribirlo junto con los instantes; insertar las filas no colapsadas con `ON CONFLICT DO NOTHING` y no pisar `anomaly_id` en el upsert de las colapsadas; verificar procesando el mismo lote dos veces que las filas no colapsadas no se duplican y los ids no cambian
- [x] 1.5 Extender `tools/smoke_collector.py` con el caso de lote re-ejecutado y verificar que pasa

## 2. Pipeline: muestras de latencia

- [x] 2.1 Agregar a `services/collector/` los límites de bucket y la asignación a bucket de `tools/latency.py` de PR #5 (`refs/pull/5/head`); verificar con un `assert` que 1000 ms cae en el bucket de límite 1000 y que 120001 ms cae en el abierto
- [x] 2.2 Crear en `store.py` la tabla de latencia por `(minuto, lote)` con cantidad, suma, máximo y los 20 conteos, clave primaria sobre ese par; verificar que `ensure_schema()` la crea
- [x] 2.3 En `pipeline.py`, juntar una muestra por registro puntuado (modelo o caché) y ninguna por registros descartados o no interpretables, y devolverlas agregadas por minuto en el `Outcome`; verificar con un lote que mezcla aciertos de caché, fallos y un descarte por shed
- [x] 2.4 Escribir esas filas en la misma transacción de `Store.write`, con `ON CONFLICT DO NOTHING`; verificar que re-ejecutar un lote deja la misma cantidad de muestras

## 3. Puente

- [x] 3.1 Mover los cursores a una tabla en la base del pipeline y dejar de usar `BRIDGE_STATE`; verificar que tras reiniciar el puente no reenvía lo ya confirmado
- [x] 3.2 Reportar `anomaly_id`, `ingested_at` y `analyzed_at` de la fila, con la derivación anterior solo para filas sin `anomaly_id`; verificar contra la aplicación simulada que los instantes enviados son distintos entre sí
- [x] 3.3 Ante un 400, no avanzar el cursor, registrar el cuerpo de la respuesta y reintentar; verificar que al "corregir" la aplicación simulada las anomalías se entregan
- [x] 3.4 Agregar el lazo de latencia: minutos tocados por filas nuevas, suma de todas las filas de esos minutos, `POST /api/v1/pipeline/latency-windows` con `boundsMs`, `count`, `sumMs`, `maxMs` y `bucketCounts`, en reportes de hasta 1440 ventanas, con `BRIDGE_INSTANCE_ID` fijo; verificar que un minuto con muestras tardías se reenvía con el total
- [x] 3.5 Borrar las filas de latencia enviadas con más de una hora; verificar con una fila fechada en el pasado
- [x] 3.6 Escribir `tools/smoke_bridge.py`, al estilo de `tools/smoke_ingest.py`, con una aplicación simulada (`http.server`) y una conexión simulada, sin Postgres: entrega, reenvío como `DUPLICATE`, `UNKNOWN_HOST`, 400, aplicación caída y vuelta, ventanas; verificar que termina con código 0

## 4. Despliegue

- [x] 4.1 Mover el servicio `bridge` a `docker-compose.yml` bajo el perfil `bridge` y dejar en `deploy/demo/compose.demo.yml` solo la red de la aplicación y las credenciales; verificar que `docker compose config -q` pasa y que `podman-compose` acepta el perfil (si no, dejar el servicio en el overlay y anotarlo en `design.md`)
- [ ] 4.2 Quitar `BRIDGE_STATE` y el volumen `bridge-state` de `deploy/demo/compose.demo.yml`; verificar que `deploy/demo/demo.sh up` y `reset` siguen funcionando

## 5. Verificación de punta a punta y documentación

- [x] 5.1 Con `demo.sh up` y la aplicación en modo Production, lanzar el ataque SSH y verificar en el detalle de la alerta que `analysisMs` es mayor que cero y que `GET /api/v1/metrics/response-time` informa muestras en `analysis`
- [ ] 5.2 Detener la API de la aplicación dos minutos con logs entrando, levantarla y verificar que no faltan anomalías ni ventanas y que no se abrió ninguna alerta repetida
- [x] 5.3 Actualizar `ARCHITECTURE.md` (el puente, los instantes, que la latencia se lee junto a los descartes) y `deploy/demo/README.md`; verificar que no quedan referencias a `BRIDGE_STATE`
- [ ] 5.4 Avisar en el PR #5 que este change lo reemplaza, y abrir en `IvanVijandi/Anomaly-Watch` los dos issues que quedan del lado de la aplicación: los nombres de campo de latencia en la guía de integración para ML (hoy circula fuera del repo) y el agrupamiento por instante de recepción; verificar que los enlaces quedan en la descripción del PR
