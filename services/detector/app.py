"""detector-service — HTTP anomaly scoring over log records.

Everything downstream (the replay harness today, the Kafka consumer later) talks
to this contract instead of importing a model. That is what makes a model swap a
config change.

Run:
    uv run uvicorn services.detector.app:app --port 8003
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ml"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Settings  # noqa: E402
from registry import get_pin  # noqa: E402
from schemas import (  # noqa: E402
    HealthResponse,
    ModelInfo,
    ScoreRequest,
    ScoreResponse,
    ScoreResult,
)
from scorer import LogScorer  # noqa: E402

logger = logging.getLogger("detector")

# Module-level so /readyz can report load state without the model being an
# import-time side effect.
_state: dict[str, object] = {"scorer": None, "settings": None, "error": None}


def _load_eval_metrics(settings: Settings, model_key: str) -> dict | None:
    """Best-effort read of the harness output for the active model."""
    path = settings.results_dir / f"{model_key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("metrics")
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read eval metrics from %s", path)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    _state["settings"] = settings

    pin = get_pin(settings.model_key)
    if settings.revision_override:
        pin = replace(pin, revision=settings.revision_override)

    try:
        _state["scorer"] = LogScorer(
            pin,
            cache_dir=settings.cache_dir,
            threshold=settings.threshold,
            max_length=settings.max_length,
            num_threads=settings.num_threads,
            anomaly_index_override=settings.anomaly_index_override,
            offline=settings.offline,
        )
        meta = _state["scorer"].metadata()
        logger.info("Loaded %s @ %s (anomaly index %s)", meta["repo_id"], meta["revision"], meta["anomaly_index"])
    except Exception as exc:  # noqa: BLE001
        # Startup failure must be loud and must name the model. A detector that
        # boots without a model is worse than one that refuses to boot.
        _state["error"] = str(exc)
        logger.error("Model load failed for %s @ %s: %s", pin.repo_id, pin.revision, exc)
        raise

    yield
    _state["scorer"] = None


app = FastAPI(title="detector-service", version="0.1.0", lifespan=lifespan)


def _require_scorer() -> LogScorer:
    scorer = _state.get("scorer")
    if scorer is None:
        # Fail fast rather than block: a caller waiting on a model that may never
        # load is a caller that cannot shed load.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_state.get("error") or "Model is not loaded yet.",
        )
    return scorer


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Report which record failed, not just that the batch did.

    A 500-record batch rejected with 'validation error' is unactionable.
    """
    problems = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", [])]
        record_index = None
        if "records" in location:
            position = location.index("records")
            if position + 1 < len(location) and location[position + 1].isdigit():
                record_index = int(location[position + 1])
        problems.append(
            {
                "record_index": record_index,
                "field": ".".join(location[-1:]) or None,
                "message": error.get("msg"),
            }
        )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Invalid records in request.", "problems": problems},
    )


@app.post("/score", response_model=ScoreResponse)
def score(request: ScoreRequest) -> ScoreResponse:
    scorer = _require_scorer()
    settings: Settings = _state["settings"]  # type: ignore[assignment]

    if len(request.records) > settings.max_batch_size:
        # Reject rather than degrade: one oversized batch monopolising the CPU
        # would blow the latency budget for every other caller.
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Batch of {len(request.records)} exceeds max_batch_size="
                f"{settings.max_batch_size}. Split the batch."
            ),
        )

    started = time.perf_counter()
    scored = scorer.score([record.message for record in request.records])
    elapsed_ms = (time.perf_counter() - started) * 1000

    # zip(strict=True) enforces the order-preserving, one-result-per-record contract.
    results = [
        ScoreResult(score=result.score, is_anomaly=result.is_anomaly, template_id=record.template_id)
        for record, result in zip(request.records, scored, strict=True)
    ]
    meta = scorer.metadata()
    return ScoreResponse(
        results=results,
        model_key=str(meta["key"]),
        model_revision=str(meta["revision"]),
        threshold=float(meta["threshold"]),
        elapsed_ms=round(elapsed_ms, 3),
    )


@app.get("/model", response_model=ModelInfo)
def model_info() -> ModelInfo:
    scorer = _require_scorer()
    settings: Settings = _state["settings"]  # type: ignore[assignment]
    meta = scorer.metadata()
    return ModelInfo(
        key=str(meta["key"]),
        repo_id=str(meta["repo_id"]),
        revision=str(meta["revision"]),
        license=str(meta["license"]),
        role=str(meta["role"]),
        threshold=float(meta["threshold"]),
        max_batch_size=settings.max_batch_size,
        max_length=int(meta["max_length"]),
        anomaly_index=int(meta["anomaly_index"]),
        anomaly_index_reason=str(meta["anomaly_index_reason"]),
        eval_metrics=_load_eval_metrics(settings, str(meta["key"])),
    )


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Liveness — the process is up. Says nothing about the model."""
    return HealthResponse(status="ok", model_loaded=_state.get("scorer") is not None)


@app.get("/readyz", response_model=HealthResponse)
def readyz() -> JSONResponse:
    """Readiness — safe to route traffic. False while the model is still loading."""
    loaded = _state.get("scorer") is not None
    body = HealthResponse(
        status="ready" if loaded else "not-ready",
        model_loaded=loaded,
        detail=None if loaded else (_state.get("error") or "Model still loading."),  # type: ignore[arg-type]
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if loaded else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(),
    )
