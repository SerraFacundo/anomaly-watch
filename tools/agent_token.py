"""Issue, list and revoke agent tokens.

The token is printed exactly once, at issue time, because only its hash is
stored. There is no recovery path and that is the point: a token that can be
read back out of the database later is a token a database dump hands over.

Rotation on machines the operator visits rarely is the open question in
`design.md`. The answer implemented here is deliberately the boring one:
`issue` for the same host mints a second credential without revoking the first,
so an operator can install the new token, confirm the agent's `last_seen` is
moving, and only then revoke the old one. Overlap instead of a cutover, because
a cutover on a host nobody can reach is a host that silently stops shipping.

Usage:
    python tools/agent_token.py issue --host web1
    python tools/agent_token.py list
    python tools/agent_token.py revoke --agent-id web1-a3f2c1
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "ingest"))

from auth import SCHEMA, generate_token, hash_token  # noqa: E402

DEFAULT_DSN = "postgresql://anomaly:anomaly@localhost:5432/anomaly_watch"


def connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn)


def cmd_issue(args) -> int:
    agent_id = args.agent_id or f"{args.host}-{secrets.token_hex(3)}"
    token = generate_token()
    with connect(args.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(
                "INSERT INTO agents (agent_id, host, token_hash) VALUES (%s, %s, %s)",
                (agent_id, args.host, hash_token(token)),
            )
        connection.commit()

    print(f"agent_id : {agent_id}")
    print(f"host     : {args.host}")
    print(f"token    : {token}")
    print()
    print("Store this now — only its hash is kept, so it cannot be shown again.")
    print("Put it in the agent's config as: Authorization: Bearer <token>")
    return 0


def cmd_list(args) -> int:
    with connect(args.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(
                "SELECT agent_id, host, created_at, last_seen, revoked_at "
                "FROM agents ORDER BY revoked_at NULLS FIRST, host, created_at"
            )
            rows = cursor.fetchall()

    if not rows:
        print("No agents registered. Issue one with: agent_token.py issue --host <name>")
        return 0

    print(f"{'agent_id':<24} {'host':<20} {'last seen':<22} state")
    for agent_id, host, _created, last_seen, revoked in rows:
        seen = last_seen.strftime("%Y-%m-%d %H:%M:%SZ") if last_seen else "never"
        state = "revoked" if revoked else ("active" if last_seen else "issued, never used")
        print(f"{agent_id:<24} {host:<20} {seen:<22} {state}")
    return 0


def cmd_revoke(args) -> int:
    with connect(args.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(
                "UPDATE agents SET revoked_at = now() WHERE agent_id = %s AND revoked_at IS NULL",
                (args.agent_id,),
            )
            affected = cursor.rowcount
        connection.commit()

    if not affected:
        print(f"No active agent with id {args.agent_id!r}.", file=sys.stderr)
        return 1
    # The ingest token cache is why this is not instant.
    print(f"Revoked {args.agent_id}. Takes effect within the ingest token cache TTL (default 60s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="mint a token for a host")
    issue.add_argument("--host", required=True)
    issue.add_argument("--agent-id", default=None, help="defaults to <host>-<random>")
    issue.set_defaults(func=cmd_issue)

    listing = sub.add_parser("list", help="show agents and when each was last seen")
    listing.set_defaults(func=cmd_list)

    revoke = sub.add_parser("revoke", help="revoke a token")
    revoke.add_argument("--agent-id", required=True)
    revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
