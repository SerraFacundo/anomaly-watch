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
