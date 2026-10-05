"""Smoke test for `ingest-service` — the contract, without a database.

Verifies the four properties the service exists to provide:

  1. an unauthenticated batch is rejected whole, and nothing is spooled
  2. an unknown schema major is rejected, naming both versions
  3. an accepted batch is on disk before the response is sent
  4. a saturated spool returns 429 with Retry-After instead of accepting

The agent store is stubbed because this machine has no container runtime, so
Postgres cannot be started. That stub is the *only* thing faked: the spool,
the wire contract and the HTTP surface are the real ones.

Usage:
    uv run python tools/smoke_ingest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.path.insert(0, str(REPO_ROOT / "services" / "ingest"))

from common.spool import Spool  # noqa: E402
from common.wire import SCHEMA_VERSION  # noqa: E402

VALID_TOKEN = "awa_test_token"


def record(message: str, **overrides) -> dict:
    base = {
        "schema_version": SCHEMA_VERSION,
        "host": "web1",
        "source": "nginx.access",
        "message": message,
        "level": "INFO",
    }
    base.update(overrides)
    return base


def build_client(spool_dir: Path, backpressure_records: int = 80_000):
    """Wire the real app against a temp spool and a stub agent store."""
    from fastapi.testclient import TestClient

    import app as ingest_app
    from config import Settings

    settings = Settings(
        spool_dir=spool_dir,
        spool_max_records=backpressure_records + 100,
        backpressure_records=backpressure_records,
        retry_after_s=30,
    )

    class StubTokens:
        def resolve(self, token: str) -> str | None:
            return "web1-stub" if token == VALID_TOKEN else None

        def touch(self, agent_id: str) -> None:
            pass

    spool = Spool(settings.spool_dir, max_records=settings.spool_max_records)
    ingest_app._state.update({"spool": spool, "tokens": StubTokens(), "settings": settings, "error": None})

    # The app's own lifespan would try to reach Postgres. State is injected
    # above instead, so the routes under test are exactly the deployed ones.
    client = TestClient(ingest_app.app, raise_server_exceptions=True)
    client.app.router.lifespan_context = _noop_lifespan
    return client, spool


def _noop_lifespan(app):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop(_app):
        yield

    return noop(app)


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        client, spool = build_client(spool_dir)
        auth = {"Authorization": f"Bearer {VALID_TOKEN}"}

        print("\n1. authentication")
        response = client.post("/ingest", json={"records": [record("GET / 200")]})
        failures += not check("no token rejected", response.status_code == 401, str(response.status_code))
        response = client.post(
            "/ingest", json={"records": [record("GET / 200")]}, headers={"Authorization": "Bearer wrong"}
        )
        failures += not check("bad token rejected", response.status_code == 401, str(response.status_code))
        failures += not check("nothing spooled by rejected requests", spool.stats().records == 0)

        print("\n2. schema version policy")
        response = client.post("/ingest", json={"records": [record("x", schema_version="2.0")]}, headers=auth)
        body = response.json()
        failures += not check("unknown major rejected", response.status_code == 400, str(response.status_code))
        failures += not check(
            "response names the supported version",
            body.get("supported_schema_version") == SCHEMA_VERSION,
            str(body.get("supported_schema_version")),
        )
        response = client.post(
            "/ingest", json={"records": [record("x", some_future_field=1)]}, headers=auth
        )
        failures += not check("unknown field accepted", response.status_code == 202, str(response.status_code))

        print("\n3. durable acknowledgement")
        response = client.post(
            "/ingest", json={"records": [record(f"line {i}") for i in range(50)]}, headers=auth
        )
        failures += not check("batch accepted", response.status_code == 202, str(response.status_code))
        failures += not check("accepted count reported", response.json().get("accepted") == 50)
        pending = spool.pending()
        failures += not check("batch is on disk at ack time", len(pending) == 2, f"{len(pending)} files")
        contents = spool.read(pending[-1])
        failures += not check("records readable from spool", len(contents) == 50)
        failures += not check(
            "agent id stamped by the service", contents[0].get("agent_id") == "web1-stub"
        )
        failures += not check("no partial .tmp files left", not list(spool_dir.glob("*.tmp")))

        print("\n4. validation detail")
        response = client.post(
            "/ingest",
            json={"records": [record("ok"), {"host": "web1", "source": "s"}]},
            headers=auth,
        )
        problems = response.json().get("problems", [])
        failures += not check("invalid batch rejected", response.status_code == 400)
        failures += not check(
            "offending record index named",
            any(problem.get("record_index") == 1 for problem in problems),
            str(problems[:2]),
        )

    with tempfile.TemporaryDirectory() as tmp:
        print("\n5. backpressure")
        spool_dir = Path(tmp) / "spool"
        client, spool = build_client(spool_dir, backpressure_records=10)
        auth = {"Authorization": f"Bearer {VALID_TOKEN}"}
        client.post("/ingest", json={"records": [record(f"l{i}") for i in range(12)]}, headers=auth)
        response = client.post("/ingest", json={"records": [record("one more")]}, headers=auth)
        failures += not check("saturated spool returns 429", response.status_code == 429, str(response.status_code))
        failures += not check("Retry-After header present", response.headers.get("Retry-After") == "30")
        status = client.get("/status").json()
        failures += not check("status reports backpressure active", status["backpressure_active"] is True)

    print()
    if failures:
        print(f"FAIL: {failures} check(s) failed")
        return 1
    print("PASS: ingest contract holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
