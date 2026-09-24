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
