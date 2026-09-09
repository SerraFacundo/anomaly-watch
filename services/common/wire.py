"""The wire contract between a log agent and the pipeline.

This is the one definition. `ingest-service` validates against it and
`log-collector` consumes it; neither owns a private copy, because two copies of
a wire format is how a fleet of agents starts failing one host at a time.

Frozen before the first agent ships. Agents live on machines the customer
updates rarely, so a record written by an agent installed today must still be
readable a year from now. That is what `schema_version` is for, and why it is
here from the first record rather than added once it hurts.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# major.minor.
#   major — breaking. Field removed, renamed, or its meaning changed. Rejected
#           by a service that does not know it.
#   minor — additive. New optional field. Older services ignore it.
SCHEMA_VERSION = "1.0"
SUPPORTED_MAJOR = 1

# Ingest is a network endpoint reachable by every monitored host. An unbounded
# batch is a memory exhaustion primitive handed to whoever holds an agent token.
MAX_BATCH_RECORDS = 1000

# Long enough for a Java stack trace line, short enough that a runaway process
# writing megabyte lines cannot fill the pipeline. Truncation happens at the
# agent; the service rejects, so the loss is visible rather than silent.
MAX_MESSAGE_CHARS = 16_384


class SchemaVersionError(ValueError):
    """Raised when a record's schema major version is not supported.

    Carries both versions: an operator debugging a stale agent needs to know
    what arrived and what was expected, not that something was 'invalid'.
    """

    def __init__(self, received: str, supported: int = SUPPORTED_MAJOR) -> None:
        self.received = received
        self.supported = supported
        super().__init__(
            f"Unsupported schema_version {received!r}: this service supports major "
            f"version {supported} (e.g. '{supported}.0'). Upgrade the agent on the "
            f"sending host, or deploy a service that speaks {received!r}."
        )


def parse_major(schema_version: str) -> int:
    head = schema_version.split(".", 1)[0].strip()
    if not head.isdigit():
        raise SchemaVersionError(schema_version)
    return int(head)


def check_major(schema_version: str) -> None:
    """Reject unknown majors. Unknown minors are deliberately fine."""
    if parse_major(schema_version) != SUPPORTED_MAJOR:
        raise SchemaVersionError(schema_version)


class WireRecord(BaseModel):
    """One log line as it crosses the network.

    Deliberately *not* `LogRecord` from the detector contract. This is what the
    agent knows before anything has parsed it: no template, no cluster id, no
    score. Drain3 runs in `log-collector`, on the central box, so the agent
    stays a dumb forwarder that never needs updating when parsing changes.
    """

    # extra="ignore" is the minor-version policy in one line: an agent one
    # version ahead sends a field we have never heard of and the record is still
    # accepted, because refusing it would take a host offline over an addition.
    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default=SCHEMA_VERSION, description="major.minor of this envelope")
    host: str = Field(min_length=1, description="Machine the line came from")
    source: str = Field(min_length=1, description="Where on it, e.g. 'nginx.access', 'systemd.journal'")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS, description="The raw log line")
    # Kept as the string the log actually contained. Parsing it into a real
    # datetime means guessing a format and a timezone per source, and a wrong
    # guess silently shifts an incident by hours.
    log_timestamp: str | None = Field(default=None, description="Timestamp as written in the log")
    level: str | None = Field(default=None, description="Severity as written, e.g. 'ERROR'. Drives the drop policy")
    # Agent-side receipt time. Distinct from log_timestamp on purpose: the gap
    # between them is how long a record sat in the agent's disk buffer, which is
    # the only direct evidence that the buffer did its job during an outage.
    agent_ts: str | None = Field(default=None, description="ISO-8601 UTC time the agent read the line")

    @field_validator("schema_version")
    @classmethod
    def _known_major(cls, value: str) -> str:
        check_major(value)
        return value


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    records: list[WireRecord] = Field(min_length=1, max_length=MAX_BATCH_RECORDS)


class RecordProblem(BaseModel):
    """Which record failed and why.

    A 1000-record batch rejected as 'validation error' is unactionable — the
    agent cannot tell which line to drop, so it retries the whole batch forever.
    Same reasoning as the detector's handler in `services/detector/app.py`.
    """

    record_index: int | None = None
    field: str | None = None
    message: str


class IngestResponse(BaseModel):
    """Returned only once the batch is durably handed off.

    Acknowledging on receipt would mean an agent deletes its buffered copy of
    records this process is still holding in memory, and a restart loses them.
    """

    accepted: int
    rejected: int = 0
    problems: list[RecordProblem] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


class BackpressureResponse(BaseModel):
    """Body of a 429. Accompanied by a `Retry-After` header.

    Backpressure is not an error: it is the pipeline telling the agent to keep
    holding records on its own disk, which is where a backlog belongs.
    """

    detail: str
    retry_after_s: int
    queue_depth: int
