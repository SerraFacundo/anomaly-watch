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

## Levantar con Docker

`docker-compose.yml` levanta `postgres` y `detector` (la capa de modelo). No es
la topologia final de `ARCHITECTURE.md` (Kafka, Ollama, api, rag-engine,
notifier): esos servicios todavia no tienen implementacion.

Este stack coexiste con el del producto (`Anomaly-Watch/`) en el mismo host,
en cualquier orden de arranque — nombre de proyecto fijo (`anomaly-watch-ml`)
y ningun puerto en comun:

| Servicio | Puerto (este repo) | Puerto (Anomaly-Watch) |
|---|---|---|
| Postgres | `127.0.0.1:5433` (`ML_POSTGRES_PORT`) | `5432` (`db`) |
| detector / api | `127.0.0.1:8003` (`ML_DETECTOR_PORT`) | `8080` (`api`) |
| — | — | `6379` redis, `3000` web, `8025` MailHog |

```bash
cp .env.example .env               # completar credenciales
uv run python ml/download_models.py  # poblar el cache de pesos en el host
docker compose up -d --build
docker compose run --rm replay data/samples/HDFS_2k.log --store-postgres
```

`replay` corre solo bajo demanda (`profiles: ["tools"]`); `docker compose up`
nunca lo levanta. Dentro del stack llega al detector y a Postgres por la red
interna, y a la API de Anomaly-Watch via `host.docker.internal` (sin unirse a
su red) si esta corriendo — ver `ML_AW_ENDPOINT` en `.env.example`.

**BREAKING:** `tools/replay.py --store-postgres` sin `--dsn` ya no apunta a
`localhost:5432` (la base del producto). Ahora usa `ANOMALY_WATCH_ML_DSN` si
esta definida, y si no, `localhost:5433` — el Postgres de este stack. Pasar
`--dsn` sigue funcionando como antes.

Si el volumen `postgres-data` viene de antes de este cambio (imagen
Debian-based de `pgvector/pgvector`), el nuevo proyecto usa un volumen propio
bajo `anomaly-watch-ml`; el anterior queda huerfano. Para limpiarlo:
`docker compose down -v` apunta solo a los recursos de este proyecto.

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
