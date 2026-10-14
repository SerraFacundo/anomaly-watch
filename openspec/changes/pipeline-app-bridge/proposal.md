## Why

El pipeline detecta anomalías y la aplicación web ya sabe recibirlas (`POST /api/v1/pipeline/anomalies` y `POST /api/v1/pipeline/latency-windows`), pero lo único que las une es `services/bridge/bridge.py`, escrito para la demo. Ese puente manda `detected_at` como `ingestedAt` y como `analyzedAt`, así que la etapa de análisis de cada alerta mide 0 ms, y no reporta ventanas de latencia: la KPI card de tiempo de respuesta (issue `IvanVijandi/Anomaly-Watch#28`) queda sin datos de análisis. Los intentos anteriores (PRs #5, #8 y #9) instrumentaban `tools/replay.py`, que desde `brokerless-log-transport` es solo un generador de carga.

## What Changes

- El pipeline sella dos instantes por registro analizado: `ingested_at` (cuándo `ingest` recibió el lote) y `analyzed_at` (cuándo obtuvo su score). Se guardan con cada anomalía.
- Cada anomalía recibe un `anomaly_id` estable, derivado del lote de spool y de su posición en él. Re-ejecutar un lote ya no duplica filas no colapsadas en la tabla `anomalies`.
- El pipeline agrega la latencia de análisis de todos los registros analizados en histogramas por minuto, con los límites de bucket del contrato de la aplicación, y los persiste en su Postgres.
- El puente reenvía las anomalías con sus instantes y su `anomaly_id` reales, y reenvía las ventanas de latencia de cada minuto que cambió.
- El cursor del puente pasa de un archivo en un volumen propio a la base del pipeline.
- Ante un rechazo del lote completo (400), el puente se detiene en ese lote y lo informa, en lugar de saltearlo.
- El servicio `bridge` pasa del overlay de la demo a `docker-compose.yml`, detrás de un perfil.
- Reemplaza al PR #5 (`report-pipeline-latency`): de ahí se rescatan los límites de bucket y el armado del histograma.

Fuera de alcance:

- Un campo `occurrences` en el contrato de anomalías. Las filas colapsadas se siguen reportando una vez.
- Que la aplicación mida sus ventanas de agrupamiento sobre el instante del log y no sobre el de recepción.
- El alta de nodos y collectors como flujo de producto (`tools/app_setup.py` sigue siendo de la demo).
- Que el detector informe la revisión real del modelo en lugar de `local`: se mantiene `BRIDGE_MODEL_REVISION`.
- Los heartbeats del puente, que no cambian.

## Capabilities

### New Capabilities
- `anomaly-reporting`: cómo una anomalía del pipeline llega a la aplicación web: su identidad, sus instantes de ingesta y análisis, la entrega durable y el tratamiento de duplicados y rechazos.
- `latency-reporting`: qué cuenta como muestra de latencia de análisis, cómo se agrega en ventanas por minuto y cómo llegan esas ventanas a la aplicación web.

### Modified Capabilities

Ninguna: el repo todavía no tiene specs archivadas en `openspec/specs/`.

## Impact

- **Código**: `services/collector/app.py`, `pipeline.py` y `store.py` (instantes, identidad, muestras de latencia); `services/bridge/bridge.py`; `docker-compose.yml` y `deploy/demo/compose.demo.yml`; un smoke nuevo `tools/smoke_bridge.py`.
- **Base del pipeline**: columnas nuevas en `anomalies` (`anomaly_id`, `ingested_at`, `analyzed_at`) y dos tablas nuevas (ventanas de latencia y cursor del puente). Todo con `IF NOT EXISTS`, sin migración manual.
- **Contrato con la aplicación**: no cambia. Se consume tal como lo definen `add-alert-classification` y `add-response-time-metric` (TDD_0016) del repo de la aplicación.
- **Dependencias**: ninguna nueva.
- **Documentación**: `ARCHITECTURE.md` y `deploy/demo/README.md`. La guía `integracion-ml.md` del equipo de la aplicación (circula fuera de los repos) documenta el reporte de latencia con nombres de campo que el endpoint no acepta (`bucketBoundaries`, `sampleCount`, `counts` en lugar de `boundsMs`, `count`, `bucketCounts`); corregirla queda del lado de la aplicación.
- **Base del change**: parte de `claude/tender-euler-p7i7zs` (`integrated-demo`), porque el puente todavía no está en `main`.
