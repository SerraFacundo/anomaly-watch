"""Drain3 parsing — turns a raw log line into the record detector-service expects.

Drain3 is unchanged from the original architecture and needs no training, which
is why it survived the model refactor untouched. Its output is metadata that
travels *alongside* the raw message; the message itself is what gets scored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

# Common leading timestamp shapes. Stripping them keeps Drain3 from minting a new
# template per second, and keeps high-cardinality noise out of the scored text.
_TIMESTAMP_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+"),
    re.compile(r"^\d{6}\s+\d{6}\s+"),  # HDFS: 081109 203615
    re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+"),  # syslog: Aug 24 10:12:45
]


@dataclass
class ParsedRecord:
    message: str
    template_id: str | None = None
    template: str | None = None
    params: list[str] | None = None
    host: str | None = None
    timestamp: str | None = None

    def to_payload(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}


def split_timestamp(line: str) -> tuple[str | None, str]:
    for pattern in _TIMESTAMP_PATTERNS:
        match = pattern.match(line)
        if match:
            return match.group(0).strip(), line[match.end() :]
    return None, line


class LogParser:
    """Thin wrapper over Drain3's TemplateMiner.

    Stateful by design: Drain3 learns templates as it goes, so the same instance
    must see the whole stream.
    """

    def __init__(self, similarity_threshold: float = 0.4, persistence_path: Path | None = None) -> None:
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig

        config = TemplateMinerConfig()
        config.drain_sim_th = similarity_threshold

        persistence = None
        if persistence_path:
            from drain3.file_persistence import FilePersistence

            persistence = FilePersistence(str(persistence_path))

        self._miner = TemplateMiner(persistence_handler=persistence, config=config)

    def parse(self, line: str, host: str | None = None) -> ParsedRecord | None:
        line = line.rstrip("\n")
        if not line.strip():
            return None

        timestamp, remainder = split_timestamp(line)
        message = remainder.strip() or line.strip()

        result = self._miner.add_log_message(message)
        template = result.get("template_mined")

        params: list[str] | None = None
        if template:
            extracted = self._miner.extract_parameters(template, message)
            params = [item.value for item in extracted] if extracted else None

        cluster_id = result.get("cluster_id")
        return ParsedRecord(
            message=message,
            template_id=str(cluster_id) if cluster_id is not None else None,
            template=template,
            params=params,
            host=host,
            timestamp=timestamp,
        )

    @property
    def template_count(self) -> int:
        return len(self._miner.drain.clusters)
