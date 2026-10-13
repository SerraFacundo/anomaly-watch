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

- [ ] Cerrado
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
- **Tomado por:** —
- **Bloqueado por:** —

### Question

En `blauerwolf/Anomaly-Watch`, rama `claude/tender-euler-p7i7zs`: que
`RecordCollectorHeartbeatUseCase` también pase a `ONLINE` los nodos cuyo `Collector` es el que
reporta, y actualice su `LastHeartbeat`. Requiere tests unitarios y de integración siguiendo los
existentes, y un PR. El paso a offline queda en "Not yet specified" del mapa hasta ver esto en
pantalla.

La resolución registra el link del PR y si `dotnet test` pasa.

---

## 3. Setup de la app: base limpia, collectors y nodos de la flota

- [ ] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** —
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

---

## 4. Puente del pipeline a la app

- [ ] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** —
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

---

## 5. Flota de 5 nodos y escenarios del generador

- [ ] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** —
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

---

## 6. Orquestación: un solo `up` y un solo `reset` para los dos stacks

- [ ] Cerrado
- **Tipo:** `task` (AFK)
- **Tomado por:** —
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
