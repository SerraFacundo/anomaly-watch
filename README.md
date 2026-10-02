# anomaly-watch

Sistema de deteccion de anomalias en logs mediante IA para PyMES.

Ver [ARCHITECTURE.md](ARCHITECTURE.md) para el diseño completo del sistema.

## Estado actual

El sistema objetivo (FluentBit → Kafka → parser → detector → RAG → alertas) está
descrito en `ARCHITECTURE.md`. Lo que existe hoy es la **capa de modelo**, que se
construyó primero para poder medirla antes de diseñar el streaming:

```
archivo de log → Drain3 → detector-service (/score) → Postgres
```

Kafka y el resto de los servicios están deferidos a propósito: los números de
latencia y tamaño de lote que produce esta capa son la entrada para dimensionar
la topología de streaming.

## Inicio rápido

```bash
uv sync --extra detector --extra ml
uv run python ml/download_models.py --model baseline-bert
uv run python ml/smoke_score.py
```

Levantar el servicio de scoring:

```bash
uv run uvicorn services.detector.app:app --port 8003
```

Procesar un archivo de log de ejemplo:

```bash
uv run python tools/replay.py data/samples/HDFS_2k.log
```

### Reportar el tiempo de respuesta a AnomalyWatch

Con `--report-metrics`, el replay mide cuánto tarda cada línea desde que se lee
hasta que vuelve su score, y lo reporta a la API de AnomalyWatch como
histogramas por minuto. Es lo que alimenta la KPI card de tiempo de respuesta.

```bash
PIPELINE_SERVICE_TOKEN=<mismo valor que en el .env de AnomalyWatch> \
  uv run python tools/replay.py data/samples/HDFS_2k.log --report-metrics --aw-endpoint http://localhost:8080
```

| Opción | Qué hace |
|---|---|
| `--report-metrics` | Activa el reporte. Sin este flag no hay ninguna llamada a AnomalyWatch |
| `--aw-endpoint` | URL base de la API de AnomalyWatch. Obligatoria con `--report-metrics` |
| `--metrics-buffer` | Ventanas sin confirmar que se guardan para reintentar (default 60). Si se llena, se descartan las más viejas y se cuentan |
| `PIPELINE_SERVICE_TOKEN` | Variable de entorno con el token de servicio. Obligatoria con `--report-metrics` |

Si falta el endpoint o el token, el replay no arranca. Si AnomalyWatch no
responde, el replay termina igual y con el mismo exit code; el resumen final
informa cuántas ventanas se reportaron, cuántas quedaron sin entregar y cuántas
se descartaron.

### Reportar las anomalías a AnomalyWatch

Con `--report-anomalies`, el replay manda a la API de AnomalyWatch cada línea
que el detector marcó como anómala. AnomalyWatch la evalúa contra sus reglas de
detección, la agrupa en una alerta y notifica. Solo viajan las líneas marcadas;
el umbral lo sigue decidiendo el modelo.

La demo de punta a punta, contra un AnomalyWatch levantado y sembrado, reporta
las anomalías y el tiempo de respuesta en la misma corrida:

```bash
PIPELINE_SERVICE_TOKEN=<mismo valor que en el .env de AnomalyWatch> \
  uv run python tools/replay.py data/samples/HDFS_2k.log \
    --report-anomalies --report-metrics \
    --host web-front-01.prod.internal --aw-endpoint http://localhost:8080
```

| Opción | Qué hace |
|---|---|
| `--report-anomalies` | Activa el reporte. Sin este flag no se reporta ninguna anomalía |
| `--host` | Hostname del nodo de origen. Obligatorio: AnomalyWatch resuelve cada anomalía a un nodo de su inventario y rechaza los hosts desconocidos. En el seed de la demo, `web-front-01.prod.internal` |
| `--aw-endpoint` | URL base de la API de AnomalyWatch. Obligatoria |
| `--log-source` | Origen del log que viaja con cada anomalía (default: el nombre del archivo) |
| `--anomalies-buffer` | Anomalías sin confirmar que se guardan para reintentar (default 5000). Si se llena, se descartan las más viejas y se cuentan |
| `PIPELINE_SERVICE_TOKEN` | Variable de entorno con el token de servicio. Obligatoria |

Cada anomalía lleva un `anomaly_id` que se asigna al marcarla y es el mismo en
el JSONL de `--out`, en la tabla `anomalies` de `--store-postgres` y en
AnomalyWatch. Si un reporte se reenvía (por ejemplo, después de un timeout),
AnomalyWatch lo reconoce como duplicado y no abre una segunda alerta.

Al final, el replay imprime una línea `reported` con el resultado:

```
reported : 12 accepted, 0 duplicate(s), 0 rejected, 0 dropped, 0 undelivered; alerts created: ALT-1001
```

- **accepted:** AnomalyWatch las registró como evidencia.
- **duplicate(s):** ya estaban registradas; pasa cuando se reenvía un reporte.
- **rejected:** AnomalyWatch las rechazó, con el motivo entre paréntesis. `UNKNOWN_HOST` casi siempre significa un `--host` equivocado; `INVALID`, un dato fuera de rango. No se reenvían.
- **dropped:** se descartaron porque el buffer se llenó durante una caída larga.
- **undelivered:** seguían sin confirmar al terminar la corrida. Quedan en `--out` y `--store-postgres` con su `anomaly_id`.
- **alerts created:** los códigos de las alertas que abrió AnomalyWatch.

Si falta el endpoint, el token o el host, el replay no arranca. Si AnomalyWatch
no responde, el replay termina igual y con el mismo exit code.

## Tests

```bash
uv run pytest
```

## Estructura

| Ruta | Qué hay |
|---|---|
| `ml/` | Ciclo de vida de modelos: registro, descarga, dataset, evaluación, fine-tuning, promoción. Ver [ml/README.md](ml/README.md) |
| `services/detector/` | `detector-service` — scoring por HTTP, inferencia en CPU |
| `tools/` | Parser Drain3 y el harness de replay (transporte de la fase demo) |
| `data/samples/` | Log de ejemplo para el smoke test de CI |
| `openspec/` | Propuestas de cambio y especificaciones |

## Documentación

- [ARCHITECTURE.md](ARCHITECTURE.md) — arquitectura del sistema
- [ml/README.md](ml/README.md) — cómo se eligen, evalúan y promueven los modelos
- [CONTRIBUTING.md](CONTRIBUTING.md) — guía de contribución
