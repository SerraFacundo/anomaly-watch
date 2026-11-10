"""Redis Streams publisher and consumer for anomaly alerts.

Decouples the log-collector worker from downstream business consumers
(business API, RAG engine, notification service, web dashboard) using
an asynchronous Redis Stream.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("alert_stream")


class AlertStreamPublisher:
    """Publishes anomaly alert events to a capped Redis Stream.

    Guarantees:
      - Fire-and-forget: Failures to publish are logged as warnings and never
        raise exceptions, ensuring the collector's log processing and PostgreSQL
        writes are never disrupted.
      - Bounded memory: Uses `maxlen` with `approximate=True` so Redis memory
        remains strictly capped even if no consumer reads from the stream.
    """

    def __init__(
        self,
        redis_url: str = "redis://redis:6379/0",
        stream_key: str = "anomaly_watch:alerts",
        maxlen: int = 10_000,
        enabled: bool = True,
        client: Any | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.stream_key = stream_key
        self.maxlen = maxlen
        self.enabled = enabled
        self._client = client
        self.published_total: int = 0

    def _get_client(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self.redis_url,
                socket_timeout=3.0,
                decode_responses=True,
            )
        return self._client

    def publish_batch(self, anomalies: list[dict]) -> int:
        """Publish a batch of anomaly records to the stream."""
        if not self.enabled or not anomalies:
            return 0

        client = None
        try:
            client = self._get_client()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AlertStreamPublisher could not obtain Redis client: %s", exc)
            return 0

        published = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        for anomaly in anomalies:
            try:
                # Key searchable fields flattened for fast filtering,
                # plus full JSON payload for rich client rendering / RAG context.
                fields = {
                    "host": str(anomaly.get("host") or ""),
                    "agent_id": str(anomaly.get("agent_id") or ""),
                    "source": str(anomaly.get("source") or ""),
                    "score": str(round(float(anomaly.get("score") or 0.0), 4)),
                    "threshold": str(round(float(anomaly.get("threshold") or 0.5), 4)),
                    "model_key": str(anomaly.get("model_key") or ""),
                    "model_revision": str(anomaly.get("model_revision") or ""),
                    "template_id": str(anomaly.get("template_id") or ""),
                    "message": str(anomaly.get("message") or "")[:2048],
                    "occurrences": str(anomaly.get("occurrences", 1)),
                    "detected_at": str(anomaly.get("detected_at") or now_iso),
                    "payload": json.dumps(anomaly, default=str),
                }

                client.xadd(
                    self.stream_key,
                    fields,
                    maxlen=self.maxlen,
                    approximate=True,
                )
                published += 1
                self.published_total += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AlertStreamPublisher failed to publish anomaly event to '%s': %s",
                    self.stream_key,
                    exc,
                )

        return published


class AlertStreamConsumer:
    """Consumer helper for the business API or notification engine.

    Can be imported by the business API on VM2 to listen to alerts reactively
    with consumer groups (or direct stream reading).
    """

    def __init__(
        self,
        redis_url: str = "redis://172.16.0.4:6379/0",
        stream_key: str = "anomaly_watch:alerts",
        client: Any | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.stream_key = stream_key
        self._client = client

    def _get_client(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self.redis_url,
                socket_timeout=10.0,
                decode_responses=True,
            )
        return self._client

    def ensure_group(self, group_name: str, start_id: str = "$") -> bool:
        """Create consumer group if it does not already exist."""
        client = self._get_client()
        try:
            client.xgroup_create(self.stream_key, group_name, id=start_id, mkstream=True)
            return True
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                return True
            logger.warning("Could not create consumer group '%s': %s", group_name, exc)
            return False

    def read_alerts(
        self,
        group_name: str | None = None,
        consumer_name: str | None = None,
        count: int = 10,
        block_ms: int = 5000,
        last_id: str = "$",
    ) -> list[tuple[str, dict]]:
        """Read alerts from the stream.

        If `group_name` and `consumer_name` are provided, uses `xreadgroup`.
        Otherwise uses direct `xread`.
        Returns a list of (message_id, data_dict).
        """
        client = self._get_client()
        results: list[tuple[str, dict]] = []

        try:
            if group_name and consumer_name:
                raw = client.xreadgroup(
                    group_name,
                    consumer_name,
                    {self.stream_key: ">"},
                    count=count,
                    block=block_ms,
                )
            else:
                raw = client.xread(
                    {self.stream_key: last_id},
                    count=count,
                    block=block_ms,
                )

            if raw:
                for _stream, messages in raw:
                    for msg_id, fields in messages:
                        if "payload" in fields and isinstance(fields["payload"], str):
                            try:
                                fields["payload"] = json.loads(fields["payload"])
                            except Exception:
                                pass
                        results.append((msg_id, fields))
        except Exception as exc:
            logger.warning("AlertStreamConsumer error reading from '%s': %s", self.stream_key, exc)

        return results

    def ack(self, group_name: str, message_ids: list[str]) -> int:
        """Acknowledge processed messages in consumer group."""
        if not message_ids:
            return 0
        try:
            return self._get_client().xack(self.stream_key, group_name, *message_ids)
        except Exception as exc:
            logger.warning("AlertStreamConsumer error acknowledging messages: %s", exc)
            return 0
