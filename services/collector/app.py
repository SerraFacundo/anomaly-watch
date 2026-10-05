"""log-collector — spool in, anomalies out.

Single replica, and not by accident. `tools/parser.py` wraps Drain3's
TemplateMiner, which learns templates online: two replicas build two divergent
template trees, so the same log line gets a different `template_id` depending on
which one saw it. That breaks the cache key, the grouping in Postgres, and the
RAG context downstream. See `design.md` D5.

Exposes only a status endpoint. It is a worker, not an API.

Run:
    uv run uvicorn services.collector.app:app --port 8005
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive import RawArchive  # noqa: E402
from cache import ScoreCache  # noqa: E402
from common.spool import Spool  # noqa: E402
from config import Settings  # noqa: E402
from parser import LogParser  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from store import Store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("collector")

_state: dict[str, object] = {}


def make_scorer(endpoint: str, timeout_s: float = 120.0):
    """HTTP client for `detector-service`, same contract `tools/replay.py` uses."""
    import httpx

    client = httpx.Client(timeout=timeout_s)

    def score(records: list[dict]) -> dict:
        response = client.post(f"{endpoint}/score", json={"records": records})
        response.raise_for_status()
        return response.json()

    return score


async def run_loop() -> None:
    """Drain the spool forever.

    A batch is deleted only after it is fully processed and persisted. A crash
    mid-batch therefore replays it rather than losing it: at-least-once, and the
    duplicate costs one cache hit.
    """
    settings: Settings = _state["settings"]  # type: ignore[assignment]
    spool: Spool = _state["spool"]  # type: ignore[assignment]
    pipeline: Pipeline = _state["pipeline"]  # type: ignore[assignment]
    store: Store = _state["store"]  # type: ignore[assignment]
    archive: RawArchive = _state["archive"]  # type: ignore[assignment]

    while True:
        try:
            pending = spool.pending()

            # Recomputed even when there is nothing to do, so a drained backlog
            # drops the stage back immediately. Updating it only when work
            # arrives would leave /status reporting "shedding" through an idle
            # period, and an operator reading that cannot tell a quiet pipeline
            # from one that is throwing records away.
            depth = spool.stats().records
            previous_stage = pipeline.policy.stage
            stage = pipeline.policy.update_depth(depth)
            if stage != previous_stage:
                logger.warning("Shed stage %d -> %d at spool depth %d", previous_stage, stage, depth)

            if not pending:
                await asyncio.sleep(settings.poll_interval_s)
                continue

            for path in pending[:20]:
                records = spool.read(path)
                archive.write(records)
                outcome = await asyncio.to_thread(pipeline.process, records)
                await asyncio.to_thread(store.write, outcome.anomalies, outcome.counters)
                spool.done(path)
                _state["last_batch_at"] = time.time()
        except Exception:  # noqa: BLE001
            # Never let the loop die. A collector that exits on one bad batch
            # stops the whole pipeline, and the spool then grows until agents
            # are told to hold, which looks like a network problem.
            logger.exception("Collector loop error; retrying")
            await asyncio.sleep(2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    _state["settings"] = settings

    settings.drain_state_path.parent.mkdir(parents=True, exist_ok=True)
    # Persistence is what keeps a template id meaning the same thing across a
    # restart. Without it every restart renumbers every template and the
    # historical anomaly rows stop being interpretable.
    parser = LogParser(persistence_path=settings.drain_state_path)

    cache = ScoreCache(
        max_entries=settings.cache_max_entries,
        ttl_s=settings.cache_ttl_s,
        enabled=settings.cache_enabled,
    )
    store = Store(settings.dsn)
    store.ensure_schema()

    archive_dir = os.environ.get("COLLECTOR_RAW_ARCHIVE_DIR") or None
    _state.update(
        {
            "spool": Spool(settings.spool_dir),
            "parser": parser,
            "cache": cache,
            "store": store,
            "archive": RawArchive(Path(archive_dir) if archive_dir else None),
            "pipeline": Pipeline(parser, cache, settings, make_scorer(settings.detector_endpoint)),
            "started_at": time.time(),
            "last_batch_at": None,
        }
    )

    task = asyncio.create_task(run_loop())
    logger.info(
        "log-collector ready. Spool %s, cache %s, Drain3 state %s",
        settings.spool_dir,
        "on" if settings.cache_enabled else "OFF",
        settings.drain_state_path,
    )
    yield
    task.cancel()
    _state["archive"].close()  # type: ignore[union-attr]


app = FastAPI(title="log-collector", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "running": "pipeline" in _state}


@app.get("/readyz")
def readyz() -> JSONResponse:
    ready = "pipeline" in _state
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if ready else "not-ready"},
    )


@app.get("/status")
def status_report() -> dict:
    """Everything needed to tell a quiet pipeline from a blind one.

    The discard counters are the point: without them an operator reading an
    empty dashboard cannot distinguish "nothing happened" from "we threw it
    away", and a monitoring product that cannot answer that is worse than none.
    """
    pipeline: Pipeline | None = _state.get("pipeline")  # type: ignore[assignment]
    if pipeline is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not started.")
    settings: Settings = _state["settings"]  # type: ignore[assignment]
    spool: Spool = _state["spool"]  # type: ignore[assignment]
    stats = spool.stats()
    return {
        "spool": {"files": stats.files, "records": stats.records, "bytes": stats.bytes},
        "shed_stage": pipeline.policy.stage,
        "shed_thresholds": {"sample_at": settings.shed_sample_at, "level_at": settings.shed_level_at},
        "cache": _state["cache"].snapshot(settings.raw_rate),  # type: ignore[union-attr]
        "counters": pipeline.counters.as_dict(),
        "model": {
            "key": pipeline.model_key,
            "revision": pipeline.model_revision,
            "threshold": pipeline.threshold,
        },
        "templates": _state["parser"].template_count,  # type: ignore[union-attr]
        "raw_archive_enabled": _state["archive"].enabled,  # type: ignore[union-attr]
        "last_batch_at": _state.get("last_batch_at"),
    }
