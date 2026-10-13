# Tickets: demo integrada

<!-- Cada sección es un ticket. Libre = abierto, sin "Tomado por" y con todo su "Bloqueado por" cerrado.
     Para tomar uno: poner tu nombre en "Tomado por" ANTES de trabajar. Para cerrarlo: marcar la casilla
     y escribir la "Resolución". El mapa es proposal.md. -->

## 0. Destino y alcance de la demo integrada

- [x] Cerrado
- **Tipo:** `grilling` (HITL)
- **Tomado por:** Facundo
- **Bloqueado por:** —

### Question

¿Qué tiene que mostrar la demo integrada, dónde corre y con qué alcance?

### Resolución

Sesión de grilling del 12 y 13 de octubre de 2026.

- **Formato.** La exposición dura 20 minutos como máximo; la demo, 10 como máximo.
- **Dónde corre.** Todo en la laptop (Podman). El deploy queda para un esfuerzo posterior.
- **Qué se muestra:** (1) alertas en vivo; (2) detalle de la alerta con su evidencia: líneas, score,
  plantilla y modelo; (3) nodos online y offline en tiempo real; (4) dashboard con riesgo global y
  actividad; (5) reglas de detección; (6) resiliencia: ingest cae y se recupera sin perder datos.
  Las métricas de latencia y las notificaciones no entran.
- **El modelo** corre local como servicio propio. Que esté "funcionando y conectado" se ve en la
  procedencia de cada alerta (`modernbert-ft-v3`, revisión y umbral) y en la página de modelos.
- **Collector = agente FluentBit, uno por nodo.** Un nodo está conectado cuando su collector
  entrega logs. El servicio central del pipeline se llama "pipeline" en los documentos nuevos.
- **Nodos online.** El backend de la app cambia para que el heartbeat de un collector pase sus
  nodos a `ONLINE`. Hoy solo actualiza la entidad Collector y el nodo queda `OFFLINE` para siempre.
- **Quién manda el heartbeat.** Un puente nuevo en este repo lee `agents.last_seen` y manda el
  heartbeat de cada collector. Redis no se usa: no ahorra cambios en el backend.
- **Datos.** La base de la app arranca sin el seed de demo. Las alertas de fondo las genera un
  precalentamiento que pasa por el pipeline real.
- **Flota:** 5 nodos. 3 sanos (uno de ellos, blanco del ataque en vivo) y 2 con problemas de
  baja intensidad que v3 sí detecta.
- **Límite del modelo, en vivo.** Se provoca un kernel panic para mostrar que v3 no lo marca.
  Facundo prefiere mostrar las falencias antes que esconderlas.
- **Cambios en el repo de la app:** permitidos, en la rama `claude/tender-euler-p7i7zs` con PR.
  La demo no espera la fusión.
- **Orquestación:** un solo `demo.sh up` y un solo `demo.sh reset` levantan y limpian todo.

---

## 1. Medir v3 contra los logs de la flota

- [x] Cerrado
- **Tipo:** `task` (HITL: los pesos de v3 están solo en la laptop)
- **Tomado por:** Claude (script) / Facundo (corrida)
- **Bloqueado por:** —

### Question

¿Qué líneas marca v3 y cuáles no, entre las que va a generar la flota? Hay que medirlo antes de
escribir el guion, por dos motivos: el kernel panic en vivo solo sirve si de verdad no se marca, y
los "problemas de baja intensidad" de los nodos con problemas solo sirven si de verdad se marcan.

Candidatas:

- **Operativas, que se espera que no marque:** kernel panic y oops, OOM killer, disco lleno,
  errores de I/O, servicio caído por systemd.
- **Fondo, que se espera que marque con score bajo o medio:** intentos SSH esporádicos, sondeos
  de `/wp-login.php`, 4xx raros.

El agente escribe el script que puntúa cada línea contra el checkpoint, siguiendo la tabla de
payloads del paso 4 de `deploy/demo/README.md`. Facundo lo corre en la laptop. La resolución
registra la tabla de scores y qué líneas usa cada escenario.

### Resolución

Medido el 13 de octubre de 2026 en la laptop, contra el detector en su contenedor. La tabla completa
está en [fleet-scores.md](fleet-scores.md).

- **Kernel panic: 6 de 6 sin marcar**, con scores entre 0.0014 y 0.0143, lejos del umbral de 0.5.
  El momento en vivo funciona. Para el escenario `panic` se usan las 6 líneas, en orden: oops, RIP,
  page fault y el `Kernel panic - not syncing` con su cierre `---[ end Kernel panic ...]---`.
- **Otras fallas operativas: 7 de 7 sin marcar** (OOM, error de EXT4, error de I/O, disco lleno en
  Postgres, servicio muerto por OOM y cola de mail llena). La de score más alto es el error de I/O, con
  0.0963. Respaldan con datos el cierre: "v3 no ve fallas operativas".
- **Tráfico normal de los nodos nuevos: 8 de 8 sin marcar**, todos por debajo de 0.006. Los nodos
  sanos no se van a llenar de falsas alarmas.
- **Fondo: 7 de 11 marcados.** Los nodos con problemas usan solo las líneas que se marcaron:
  - `db1`: `Failed password for root` (1.0), `Invalid user oracle` (1.0) y Postgres
    `password authentication failed` (0.9944). Que v3 marque la falla de login de Postgres es
    nuevo: no estaba medido.
  - `mail1`: `Failed password for invalid user` en sshd (1.0) y sondeos web a `/wp-login.php` (1.0),
    `/phpmyadmin/` (0.9931) y `POST /xmlrpc.php` (0.7449). Para eso, `mail1` sirve un webmail
    detrás de nginx.
- **Lo que v3 no marca y no se usa de fondo:** el cierre preauth de sshd (0.0004), el fallo de
  SASL de postfix (0.0001), el relay denegado de postfix (0.0000) y `GET /.git/config` (0.0010).
  Que los ataques contra el servidor de mail se le escapen es otro límite honesto para el cierre,
  junto con SQLi y XSS.

**Lo que esto destapa para otros tickets:**

- **La revisión que informa el detector es `local`, no el SHA del checkpoint** (`e1d3703d`). La
  app guarda esa revisión como procedencia de cada anomalía, así que en pantalla se va a leer
  `local`. Pasa a "Not yet specified" del mapa.
- **En una máquina real, un kernel panic la congela antes de que el agente mande la línea.** Lo
  que se ve de verdad es un nodo que deja de reportar. El guion tiene que decirlo cuando inyecte
  las líneas.

### Checklist para Facundo

Script: `tools/score_fleet_lines.py`, con 32 líneas en 4 grupos (`panic`, `operational`,
`background`, `normal`). Cada línea dice si se espera que v3 la marque (`flag`) o no (`pass`).

1. `git pull` en la rama `claude/tender-euler-p7i7zs`.
2. Levantar el stack si no está arriba: `deploy/demo/demo.sh up`, y esperar a que el detector esté
   sano.
3. Correr desde la raíz del repo:
   `podman compose exec -T detector python - < tools/score_fleet_lines.py > fleet-scores.md`
4. Pegar el contenido de `fleet-scores.md` en la conversación, o commitearlo junto a este archivo.

Con la tabla, Claude escribe la resolución: qué líneas usa cada escenario y qué hacer con los que
fallen.

---

## 2. Backend de la app: el heartbeat del collector pasa sus nodos a online

- [ ] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** Claude
- **Bloqueado por:** —

### Question

En `blauerwolf/Anomaly-Watch`, rama `claude/tender-euler-p7i7zs`: que
`RecordCollectorHeartbeatUseCase` también pase a `ONLINE` los nodos cuyo `Collector` es el que
reporta, y actualice su `LastHeartbeat`. Requiere tests unitarios y de integración siguiendo los
existentes, y un PR. El paso a offline queda en "Not yet specified" del mapa hasta ver esto en
pantalla.

La resolución registra el link del PR y si `dotnet test` pasa.

**En curso.** Draft PR: https://github.com/blauerwolf/Anomaly-Watch/pull/1. El código y los tests
están escritos; falta que el CI compile y pase los tests, porque el entorno donde se escribió no
tiene el SDK de .NET. El ticket se cierra con el CI en verde.

---

## 3. Setup de la app: base limpia, collectors y nodos de la flota

- [x] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** Claude
- **Bloqueado por:** —

### Question

¿Cómo queda la app lista para la demo sin pasos manuales? El seed de la app es manual
(`docker-compose.yml` de la app, servicio `migrate`) y hoy siembra todo junto: usuarios, nodos,
alertas y topología ficticios. Hay que:

- Averiguar cómo arrancar con los usuarios y la configuración, pero sin los datos ficticios.
- Escribir un script idempotente que, como administrador, cree por la API un collector por nodo
  (`POST /api/v1/collectors`, que devuelve la credencial una sola vez) y los 5 nodos
  (`POST /api/v1/nodes`), con el hostname igual al `host` que reporta FluentBit.
- Guardar las credenciales en `deploy/demo/app-credentials.env` con modo 600, fuera de git, igual
  que `tokens.env`.

La resolución registra el procedimiento, el formato del archivo de credenciales y los usuarios con
los que se entra a la app.

### Resolución

Validado en la laptop el 13 de octubre: `app_setup.py` creó los 5 collectors y nodos y la app los
muestra con su ficha. Se entra con `admin@anomalywatch.local` / `password123`. Ajustes que hizo
falta en el camino (todos en `demo.sh`): completar el `.env` de la app con las variables nuevas y
generar `TWO_FACTOR_ENCRYPTION_KEY`, y que `app_setup.py` diga qué espera mientras la API arranca.

Lo escrito:

- App (`blauerwolf/Anomaly-Watch`, rama `claude/tender-euler-p7i7zs`, commit `0c7a7eb`): `Seed:Skip`
  omite seeders por nombre de clase. La demo omite `MonitoredNodesSeeder`, `EventLogsSeeder`,
  `AlertsSeeder`, `NetworkTopologySeeder` y `CollectorsSeeder`. Quedan los usuarios (entre ellos
  `admin@anomalywatch.local` / `password123`), los roles, las reglas y los modelos ficticios.
- `tools/app_setup.py` hace login como admin, reintentando mientras la API arranca. Crea los
  collectors `collector-<host>` y los nodos `<host>`, y registra el modelo `modernbert-ft-v3`.
  Escribe `deploy/demo/app-credentials.env` (`COLLECTOR_<HOST>=<code>:<token>`, modo 600). Es
  idempotente y rota la credencial de un collector que existe pero no figura en el archivo.
  Probado contra una API falsa.

---

## 4. Puente del pipeline a la app

- [x] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** Claude
- **Bloqueado por:** 3

### Question

Un servicio nuevo en este repo, un contenedor más del stack, con dos lazos:

- **Anomalías.** Lee las filas nuevas de `anomalies` en la base del pipeline y las manda a
  `POST /api/v1/pipeline/anomalies` con `PIPELINE_SERVICE_TOKEN`, en lotes de hasta 500.
  - El `anomalyId` sale del id de la fila, para que un reenvío dé `DUPLICATE` y no una alerta
    repetida.
  - El cursor se persiste, para que un reinicio no reenvíe todo ni pierda filas.
- **Heartbeats.** Por cada agente con `last_seen` reciente, manda
  `POST /api/v1/collectors/{code}/heartbeat` con la credencial de su collector.

Si la app está caída, reintenta sin frenar el pipeline. En la resolución: qué hace con las filas
agrupadas (`occurrences`), qué hace con los rechazos `UNKNOWN_HOST`, y un smoke test al estilo de
`tools/smoke_ingest.py`.

### Resolución

Validado en la laptop el 13 de octubre: las anomalías de db1 y mail1 abrieron alertas Low y Medium
con su evidencia, modelo `modernbert-ft-v3@e1d3703d90af` y template; el ataque SSH abrió el
Critical "SSH Brute Force" con 5 líneas de evidencia; los 5 nodos quedan `ONLINE` y
`CONNECTED` por el heartbeat. Las 97 filas viejas de un `loadtest` previo se rechazaron con
`UNKNOWN_HOST`, como corresponde; el log ahora agrupa los rechazos por motivo.

Lo escrito: `services/bridge/bridge.py` es un servicio `bridge` en
`compose.demo.yml`: corre en la imagen de ingest, que ya trae psycopg, y está conectado a la red del
pipeline y a la de la app (`aw-app_default`).

- **Filas agrupadas:** se mandan una vez, cuando aparecen. Las ocurrencias que se suman después
  quedan en el pipeline.
- **`anomalyId`:** `uuid5` del id de la fila, así un reenvío es `DUPLICATE`. El cursor se guarda en
  un volumen después de cada lote aceptado.
- **Rechazos:** un `UNKNOWN_HOST` se loguea y se sigue. Un 400 de lote completo se loguea y se
  saltea. Con 5xx, 401 o 403 el cursor no avanza y se reintenta.
- **Revisión `local`** del detector: se reemplaza por `BRIDGE_MODEL_REVISION` (`e1d3703d...`, la
  que fija `demo.sh`).
- **Heartbeat** cada 30 s para cada agente con `last_seen` de menos de 120 s.

Probado contra una app falsa y una conexión falsa: cursor, idempotencia, reintento y heartbeats.

---

## 5. Flota de 5 nodos y escenarios del generador

- [ ] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** Claude
- **Bloqueado por:** 1

### Question

Llevar `deploy/demo/` de 3 nodos a 5:

- Sumar los 2 nodos con problemas, con su agente, su token y su tráfico.
- En `loggen.py`, darles un fondo de problemas de baja intensidad, con las líneas que el ticket 1
  confirmó que v3 marca.
- Agregar el escenario `demo.sh attack <nodo> panic`, con las líneas que el ticket 1 confirmó que v3
  no marca.
- Agregar un precalentamiento que deje alertas de fondo reales antes de exponer.

Un nodo más es un contenedor FluentBit más y algo de RAM: verificar que la laptop lo aguante junto
con la app.

**Escrito, falta validar en la laptop.**

- `loggen.py`: 5 nodos. `db1` corre sshd y postgres; `mail1` corre nginx (webmail), sshd y postfix.
  Postgres, postfix y kernel escriben en `/logs/<nodo>/syslog`, que FluentBit lee con
  `source syslog`.
- Fondo: un problema cada 90 a 180 s en cada nodo con problemas, solo con líneas que v3 marca. Así
  hay como mucho 4 fallos SSH cada 5 minutos, por debajo de la regla de fuerza bruta: el único
  Critical es el ataque en vivo.
- `attack <nodo> panic` escribe 5 líneas de kernel medidas.
- `warmup` escribe, sin SSH, problemas de Postgres en `db1` y sondeos web en `mail1`. Abren
  alertas Medium de fondo.

Comprobado contra `fleet-scores.md`: toda línea de fondo coincide con una medida como marcada, y
toda línea de panic con una medida como no marcada.

---

## 6. Orquestación: un solo `up` y un solo `reset` para los dos stacks

- [x] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** Claude
- **Bloqueado por:** 3, 4

### Question

Que `deploy/demo/demo.sh up` levante también la app (API, Postgres y Redis de la app, y el
frontend) desde un checkout hermano (`APP_DIR`, por defecto `../Anomaly-Watch`):

- En una red que el puente alcance.
- Con el mismo `PIPELINE_SERVICE_TOKEN` en los dos lados.
- En modo Production o Development, nunca `Showcase`.
- Corriendo el setup del ticket 3, y con el puente arrancando cuando la app esté sana.

`reset` limpia los dos stacks y las credenciales. Hay que contemplar los puertos que ya ocupan
los dos stacks (443 y 80 del pipeline, 8080 de la API, el del frontend) y las rarezas de
podman-compose documentadas en `RUN-RECORD.md`.

### Resolución

Validado en la laptop el 13 de octubre: un `demo.sh up` deja todo andando. Rarezas de Podman que
hubo que resolver, en `demo.sh` y `app.override.yml`:

- podman-compose convierte `depends_on` en `--requires`, que rechaza un `migrate` ya terminado:
  `api` y `web` se levantan con `--no-deps` (el orden lo ponen los pasos previos).
- El backup previo a migrar se saltea (`BACKUP_BEFORE_MIGRATE=false`): la base es descartable.
- Podman rootless no publica 80/443: nginx del pipeline sale por 8443/8880 en el host.
- El nginx de la web tiene `resolver 127.0.0.11` (DNS de Docker): un override monta un script que
  pone el DNS real del contenedor.
- podman-compose no recrea contenedores cuando cambia la imagen: el stack del pipeline se levanta
  con `--force-recreate` (ingest corría código de antes de aceptar el array de FluentBit).
- `demo.sh logs [servicio]`, porque el overlay necesita los tokens que carga `demo.sh`.

Lo escrito: `demo.sh up`:

1. Emite los tokens que falten (también para los nodos nuevos, sin tocar los otros).
2. Levanta la app como proyecto `aw-app` (`APP_DIR`, por defecto `../Anomaly-Watch`). El nombre
   propio evita que choque con el proyecto del pipeline, porque los dos directorios se llaman
   `anomaly-watch` en minúscula.
3. Corre `migrate`, el seed con `Seed__Skip` y `app_setup.py`.
4. Levanta el stack del pipeline con el puente.
5. Corre el precalentamiento.

`down` y `reset` alcanzan a los dos stacks. `reset` borra solo el proyecto `aw-app`, no un stack de
la app levantado a mano.

---

## 7. Guion de 10 minutos

- [ ] Cerrado
- **Tipo:** `grilling` (HITL)
- **Tomado por:** —
- **Bloqueado por:** 1, 5

### Question

¿Qué se ve y qué se dice, minuto a minuto, dentro de los 10 minutos de demo? Hay que decidir:

- El orden de las pantallas de la app.
- Qué nodo recibe el ataque en vivo y cuál tiene el kernel panic.
- Dónde entra la resiliencia.
- Qué reglas de detección se cargan antes (sale de "Not yet specified" del mapa).
- Qué se dice del límite de v3.
- El plan B para cada paso que puede fallar.

Reemplaza el guion del README de la demo. La resolución es el guion final, enlazado.

---

## 8. Ensayo general y video de respaldo

- [ ] Cerrado
- **Tipo:** `task` (HITL)
- **Tomado por:** —
- **Bloqueado por:** 2, 6, 7

### Question

Correr el guion completo en la laptop, desde `reset` y `up` hasta el cierre, cronometrado:

- Grabar la corrida como video de respaldo.
- Registrar números y defectos en un `RUN-RECORD` igual al de la primera corrida.

Cerrar este ticket archiva el change.
