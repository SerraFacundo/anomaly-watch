# Handoff: demo integrada (pipeline + aplicación web)

Para continuar en una sesión local, con acceso al repo del equipo `IvanVijandi/Anomaly-Watch`.
Escrito el 13 de octubre de 2026 al cerrar la sesión en la nube.

## Qué se está haciendo

Una demo de 10 minutos, dentro de una exposición de 20, que corra entera en la laptop con un solo
`deploy/demo/demo.sh up`:

- 5 nodos con collectors FluentBit mandan logs.
- El pipeline los puntúa con `modernbert-ft-v3`.
- La aplicación web del equipo muestra en vivo los nodos conectados, las alertas y su evidencia.

El plan está como un mapa (wayfinder) en un change de openspec:

- **Mapa:** `openspec/changes/integrated-demo/proposal.md` (destino, notas, decisiones, lo que falta
  definir y lo que queda fuera).
- **Tickets:** `openspec/changes/integrated-demo/tasks.md`. Cada uno tiene tipo, quién lo tomó y de
  qué depende.
- **Glosario:** `CONTEXT.md` en la raíz. Nodo, collector (uno por nodo = agente FluentBit), pipeline
  (el servicio central, aunque el directorio siga siendo `services/collector/`), anomalía, alerta.

Leer esos tres archivos antes que nada. Todas las decisiones de alcance están en el ticket 0.

## Repos y ramas

| Repo | Rol | Rama |
|---|---|---|
| `SerraFacundo/anomaly-watch` | Pipeline, modelo, orquestación de la demo, mapa | `claude/tender-euler-p7i7zs` |
| `IvanVijandi/Anomaly-Watch` | App del equipo (.NET + React). **Es el repo real del equipo** | todavía sin la rama: ver la tarea inmediata |
| `blauerwolf/Anomaly-Watch` | Fork de la app. Ahí quedó la rama por error de acceso | `claude/tender-euler-p7i7zs` |

La sesión en la nube solo tenía acceso a `blauerwolf`, por eso el cambio del backend quedó ahí.

## Estado de los tickets

| # | Ticket | Estado |
|---|---|---|
| 0 | Destino y alcance | Cerrado |
| 1 | Medir v3 contra los logs de la flota | Cerrado. Resultados en `fleet-scores.md` |
| 2 | Backend: el heartbeat del collector pone en línea sus nodos | **En curso: código escrito, sin compilar.** Ver la tarea inmediata |
| 3 | Setup de la app: base limpia, collectors y nodos | Libre |
| 4 | Puente del pipeline a la app | Bloqueado por 3 |
| 5 | Flota de 5 nodos y escenarios del generador | Libre (el 1 ya está cerrado) |
| 6 | Orquestación: un solo `up` y `reset` | Bloqueado por 3 y 4 |
| 7 | Guion de 10 minutos | Bloqueado por 5 |
| 8 | Ensayo general y video | Bloqueado por 2, 6 y 7 |

## Tarea inmediata: llevar el ticket 2 al repo del equipo

El cambio está en `blauerwolf/Anomaly-Watch`, rama `claude/tender-euler-p7i7zs`, commit `8bdac5f`.
Draft PR de referencia: https://github.com/blauerwolf/Anomaly-Watch/pull/1

1. Traer la rama al clon del equipo:
   ```bash
   cd Anomaly-Watch            # origin = git@github.com:IvanVijandi/Anomaly-Watch.git
   git remote add blauerwolf https://github.com/blauerwolf/Anomaly-Watch.git
   git fetch blauerwolf claude/tender-euler-p7i7zs
   git checkout -b claude/tender-euler-p7i7zs blauerwolf/claude/tender-euler-p7i7zs
   ```
   Si `blauerwolf` no se puede leer, el cambio es chico y se puede rehacer a mano (ver "Qué cambia").
2. Validar. **Nunca se compiló**, porque la nube no podía bajar el SDK de .NET 10. El host es Bluefin
   (Fedora inmutable), así que conviene un contenedor:
   ```bash
   cd Backend/AnomalyWatch
   podman run --rm -v "$PWD":/src:z -w /src mcr.microsoft.com/dotnet/sdk:10.0 \
     sh -c "dotnet format --verify-no-changes && dotnet build && dotnet test Tests/Application.UnitTests && dotnet test Tests/Api.IntegrationTests"
   ```
   O hacer push a `origin`: `ci.yml` corre en cada push y hace restore, `format --verify-no-changes`,
   build Release y test.
3. Arreglar lo que falle, abrir el PR en `IvanVijandi/Anomaly-Watch` hacia `main` (la plantilla está
   en `.github/PULL_REQUEST_TEMPLATE.md`; se puede reusar la descripción del PR de blauerwolf) y
   cerrar el PR de `blauerwolf`.
4. En `tasks.md` del repo del modelo, cerrar el ticket 2 con el link del PR nuevo y el resultado de
   los tests, y sumar una línea en "Decisions so far" de `proposal.md`.

### Qué cambia (por si hay que rehacerlo)

- `Domain/Entities/MonitoredNode.cs`: `RegisterHeartbeat(DateTime atUtc)`. Pone `LastHeartbeat`,
  deja `AgentStatus = Connected` y actualiza `UpdatedAt`. Si el nodo estaba `Offline`, pasa a
  `Online`; `Warning` y `Maintenance` se conservan.
- `Domain/Repositories/INodeRepository.cs`: `ListByCollectorAsync(string collectorCode)` y
  `UpdateRangeAsync(nodes)`, implementados en `EfNodeRepository` y en `FakeNodeRepository`
  (`Tests/Application.UnitTests/Fakes/MonitoringFakes.cs`; `UsersApiFactory` usa el mismo fake).
- `RecordCollectorHeartbeatUseCase`: recibe `INodeRepository`. Después de guardar el collector, lista
  los nodos con `Collector == collector.Code`, les registra el heartbeat y los actualiza.
- Tests: dos unitarios en `CollectorRegistryTests` (offline pasa a online y otro collector no cambia;
  maintenance se conserva) y uno de integración en `CollectorAgentCredentialEndpointTests`.
- openspec: `openspec/changes/mark-nodes-online-on-heartbeat/` (proposal, delta de `node-monitoring`,
  tasks).

Puntos a mirar si el build falla: el constructor nuevo del caso de uso (DI en `Program.cs` ya
registra `INodeRepository`) y el formato (`dotnet format`).

## Después: tickets libres

- **3. Setup de la app.** Arrancar la app con usuarios pero sin los datos ficticios. El seed es
  manual y siembra todo junto (`Application/Seeding/*`, registrados en `Api/Program.cs` ~456). Script
  idempotente que cree por API un collector por nodo (`POST /api/v1/collectors` devuelve la
  credencial **una sola vez**) y los 5 nodos (`POST /api/v1/nodes`, con `hostname` igual al `host`
  de FluentBit). Credenciales en `deploy/demo/app-credentials.env`, con modo 600 y en `.gitignore`.
- **5. Flota.** Pasar de 3 a 5 nodos (sumar `db1` y `mail1`). El fondo de problemas usa solo las
  líneas que v3 marca (ver la resolución del ticket 1). `attack <nodo> panic` usa las 6 líneas de
  kernel panic. Sumar un precalentamiento.

## Hechos que no están en el código y cuestan caro redescubrir

- **v3 no marca:** kernel panic, OOM, disco lleno, I/O, postfix (SASL, relay), `/.git/config`, SQLi
  ni XSS. **Sí marca:** SSH (`Failed password`, `Invalid user`), login fallido de Postgres (0.99),
  `/wp-login.php`, `/phpmyadmin/`, `/xmlrpc.php` y el traversal a `/etc/passwd`.
- **El detector informa `revision: "local"`,** no el SHA `e1d3703d`, y la app lo mostraría como
  procedencia de cada alerta. Está anotado en "Not yet specified" del mapa.
- **`POST /api/v1/pipeline/anomalies`** ya existe en la app (`PipelineController`). Se autentica con
  `PIPELINE_SERVICE_TOKEN`, acepta hasta 500 anomalías por lote, es idempotente por `anomalyId` y
  rechaza un host sin nodo con `UNKNOWN_HOST`. Una anomalía sin regla que coincida abre una alerta
  con 1 ocurrencia: Medium si el score es 0.9 o más, Low si no (`AlertClassifier`).
- **Modo `Showcase` de la app:** `[DisabledInShowcase]` apaga el endpoint del pipeline y el
  heartbeat. Correr en Production o Development.
- **Un collector se da por desconectado tras 5 minutos sin heartbeat**
  (`Collector.HeartbeatGracePeriod`). Si el nodo hereda ese plazo, el kernel panic en vivo tarda 5
  minutos en verse. El paso a offline está abierto en el mapa.
- **En una máquina real, un kernel panic la congela** antes de que el agente mande la línea. El
  guion tiene que decirlo al inyectar las líneas.
- **Podman y SELinux:** todo bind mount lleva `z`. nginx resuelve el upstream por request. Detalles
  en `deploy/demo/RUN-RECORD.md`.

## Preferencias de Facundo

Español. Sin emojis. Fórmulas en LaTeX. Prefiere mostrar las falencias del modelo antes que
esconderlas. Lo mínimo que funcione: quedan días para la exposición.
