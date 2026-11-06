"""ingest-service — the HTTP entry point for log records.

Agents hold their own disk buffer and post batches here; this service
authenticates them, validates against the frozen wire contract, and enqueues
the batch durably into Redis (or the fallback disk spool) for `log-collector`.

The one hard rule: a batch is acknowledged only after it is durably handed off.
An agent deletes its buffered copy on a 202, so an acknowledgement that is not
backed by durable storage is silent data loss with a success code attached.

Run:
    uv run uvicorn services.ingest.app:app --port 8004
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from auth import TokenStore  # noqa: E402
from common.redis_queue import QueueUnavailableError, RedisLogQueue, build_queue  # noqa: E402
from common.spool import Spool, SpoolFull  # noqa: E402
from common.wire import (  # noqa: E402
    SCHEMA_VERSION,
    SUPPORTED_MAJOR,
    BackpressureResponse,
    IngestRequest,
    IngestResponse,
    RecordProblem,
)
from config import Settings  # noqa: E402

logger = logging.getLogger("ingest")

_state: dict[str, object] = {"spool": None, "tokens": None, "settings": None, "error": None}

# auto_error=False so a missing header produces our own 401 with a usable
# message rather than FastAPI's bare "Not authenticated".
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    _state["settings"] = settings

    spool = build_queue(
        settings.queue_backend,
        spool_dir=settings.spool_dir,
        redis_url=settings.redis_url,
        redis_queue_key=settings.redis_queue_key,
        max_records=settings.spool_max_records,
    )
    if not spool.is_ready():
        msg = f"Cannot reach log queue backend ({settings.queue_backend})"
        _state["error"] = msg
        logger.error(msg)
        raise RuntimeError(msg)

    swept = spool.sweep_temp()
    if swept:
        logger.warning("Swept %d partial spool files left by an unclean shutdown", swept)
    _state["spool"] = spool

    tokens = TokenStore(settings.dsn, ttl_s=settings.token_cache_ttl_s)
    try:
        tokens.ensure_schema()
    except Exception as exc:  # noqa: BLE001
        # Refuse to start. Booting without an auth store would mean every
        # request denied anyway (the store fails closed), so a service that
        # looks healthy while rejecting all traffic is the worse outcome.
        _state["error"] = f"Cannot reach the agent store: {exc}"
        logger.error("Agent store unavailable at startup: %s", exc)
        raise
    _state["tokens"] = tokens

    target = settings.redis_url if settings.queue_backend == "redis" else str(settings.spool_dir)
    logger.info(
        "ingest-service ready. Queue %s (%s), schema %s",
        settings.queue_backend,
        target,
        SCHEMA_VERSION,
    )
    yield
    _state["spool"] = None


app = FastAPI(title="ingest-service", version="0.1.0", lifespan=lifespan)


def require_agent(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    tokens: TokenStore | None = _state.get("tokens")  # type: ignore[assignment]
    if tokens is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Agent store not ready.")
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Missing agent token. Send 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    agent_id = tokens.resolve(credentials.credentials)
    if agent_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or revoked agent token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return agent_id


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Name the offending records.

    An agent that is told only that its batch of 1000 was 'invalid' cannot drop
    the bad line, so it retries the same batch forever and stops shipping
    everything behind it. Same reasoning as the detector's handler.
    """
    problems: list[RecordProblem] = []
    version_mismatch = False
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", [])]
        record_index = None
        if "records" in location:
            position = location.index("records")
            if position + 1 < len(location) and location[position + 1].isdigit():
                record_index = int(location[position + 1])
        message = str(error.get("msg"))
        if "schema_version" in location:
            version_mismatch = True
        problems.append(
            RecordProblem(record_index=record_index, field=location[-1] if location else None, message=message)
        )

    detail = (
        f"Unsupported schema version in batch. This service speaks major version "
        f"{SUPPORTED_MAJOR} (current: {SCHEMA_VERSION}). Upgrade the agent."
        if version_mismatch
        else "Invalid records in batch."
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": detail,
            "supported_schema_version": SCHEMA_VERSION,
            "problems": [problem.model_dump() for problem in problems],
        },
    )


@app.post("/ingest", response_model=IngestResponse)
def ingest(request: IngestRequest, response: Response, agent_id: str = Depends(require_agent)) -> IngestResponse:
    spool: Spool | RedisLogQueue | None = _state.get("spool")  # type: ignore[assignment]
    settings: Settings = _state["settings"]  # type: ignore[assignment]
    if spool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Queue not ready.")

    try:
        depth = spool.stats().records
    except QueueUnavailableError as exc:
        logger.warning("Queue unavailable during depth check: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Log queue temporarily unavailable. Retain and retry.",
        ) from exc

    if depth >= settings.backpressure_records:
        # Not an error. The pipeline is telling the agent to keep holding its
        # records, which is where a backlog belongs — on the machine that
        # produced it, not on the box also running the model.
        body = BackpressureResponse(
            detail="Pipeline is behind. Retain and retry.",
            retry_after_s=settings.retry_after_s,
            queue_depth=depth,
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=body.model_dump(),
            headers={"Retry-After": str(settings.retry_after_s)},
        )

    payload = [record.model_dump() for record in request.records]
    for record in payload:
        # Stamped here, not trusted from the wire: it is the pipeline's own
        # record of which credential the batch arrived under.
        record["agent_id"] = agent_id

    try:
        spool.append(payload)
    except SpoolFull as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(settings.retry_after_s)},
        ) from exc
    except QueueUnavailableError as exc:
        logger.warning("Queue unavailable during enqueue: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Log queue temporarily unavailable. Retain and retry.",
        ) from exc

    # Only now. The batch is durably queued, so the agent is safe to drop it.
    tokens: TokenStore = _state["tokens"]  # type: ignore[assignment]
    tokens.touch(agent_id)
    response.status_code = status.HTTP_202_ACCEPTED
    return IngestResponse(accepted=len(payload))


@app.get("/healthz")
def healthz() -> dict:
    """Liveness. Says nothing about whether the pipeline is keeping up."""
    settings: Settings | None = _state.get("settings")  # type: ignore[assignment]
    return {
        "status": "ok",
        "spool_ready": _state.get("spool") is not None,
        "queue_backend": settings.queue_backend if settings else None,
    }


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness. Not ready while the queue or the agent store is unavailable."""
    spool: Spool | RedisLogQueue | None = _state.get("spool")  # type: ignore[assignment]
    spool_ready = spool is not None and spool.is_ready()
    ready = spool_ready and _state.get("tokens") is not None
    detail = None
    if not ready:
        detail = (
            "Queue backend unreachable."
            if spool is not None and not spool_ready
            else (_state.get("error") or "Starting.")
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ready" if ready else "not-ready",
            "detail": detail,
        },
    )


@app.get("/status")
def status_report() -> dict:
    """Backlog depth, for operators and for the collector's own tuning."""
    spool: Spool | RedisLogQueue | None = _state.get("spool")  # type: ignore[assignment]
    settings: Settings = _state["settings"]  # type: ignore[assignment]
    if spool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Queue not ready.")
    try:
        stats = spool.stats()
    except QueueUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "spool": {"files": stats.files, "records": stats.records, "bytes": stats.bytes},
        "queue": {
            "backend": settings.queue_backend,
            "pending_records": stats.pending_records,
            "inflight_records": stats.inflight_records,
            "records": stats.records,
            "bytes": stats.bytes,
        },
        "backpressure_at_records": settings.backpressure_records,
        "backpressure_active": stats.records >= settings.backpressure_records,
    }
