"""Request/response contract for the detector.

This contract is the stable surface every downstream consumer depends on. The
model behind it is expected to change; this is not.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LogRecord(BaseModel):
    """One parsed log record.

    `message` is the raw line and is the only required field — it is what the
    model actually scores. The Drain3 fields travel alongside for grouping,
    classical-baseline features and RAG context, but are never the model input.
    """

    message: str = Field(min_length=1, description="Raw log line. Scored by the model.")
    template_id: str | None = Field(default=None, description="Drain3 cluster id")
    template: str | None = Field(default=None, description="Parameterized template, e.g. 'User <*> logged in'")
    params: list[str] | None = Field(default=None, description="Values extracted by Drain3")
    host: str | None = Field(default=None, description="Source machine")
    timestamp: str | None = Field(default=None, description="Original log timestamp, as written")


class ScoreRequest(BaseModel):
    records: list[LogRecord] = Field(min_length=1)


class ScoreResult(BaseModel):
    # Always present regardless of the threshold, so consumers can re-threshold
    # historical results without paying to re-score them.
    score: float = Field(ge=0.0, le=1.0)
    is_anomaly: bool
    template_id: str | None = None


class ScoreResponse(BaseModel):
    results: list[ScoreResult]
    model_key: str
    model_revision: str
    threshold: float
    elapsed_ms: float


class ModelInfo(BaseModel):
    key: str
    repo_id: str
    revision: str
    license: str
    role: str
    threshold: float
    max_batch_size: int
    max_length: int
    anomaly_index: int
    anomaly_index_reason: str
    eval_metrics: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    detail: str | None = None
