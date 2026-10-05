"""Render a FluentBit config for one host from the shared template.

Configs are generated, never hand-edited on the machine. A fleet whose configs
were each tweaked during an install is a fleet where nobody can say what any
given host is actually shipping — and the first time that matters is while
debugging why one host went quiet.

Usage:
    python tools/render_agent_config.py --host web1 --ingest-host watch.internal \\
        --token awa_... --out /tmp/web1-fluent-bit.conf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "deploy" / "fluentbit" / "fluent-bit.conf.template"

PLACEHOLDERS = ("@@HOST@@", "@@AGENT_TOKEN@@", "@@INGEST_HOST@@", "@@CA_FILE@@")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="value for the `host` field on every record")
    parser.add_argument("--ingest-host", required=True, help="hostname of the Nginx TLS endpoint")
    parser.add_argument("--token", required=True, help="from tools/agent_token.py issue")
    parser.add_argument("--ca-file", default="/etc/fluent-bit/anomaly-watch-ca.crt")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if not TEMPLATE.exists():
        print(f"Template missing: {TEMPLATE}", file=sys.stderr)
        return 1

    rendered = TEMPLATE.read_text(encoding="utf-8")
    for placeholder, value in zip(
        PLACEHOLDERS, (args.host, args.token, args.ingest_host, args.ca_file), strict=True
    ):
        rendered = rendered.replace(placeholder, value)

    missing = [placeholder for placeholder in PLACEHOLDERS if placeholder in rendered]
    if missing:
        # Shipping a config with an unsubstituted placeholder means an agent
        # that authenticates as the literal string "@@AGENT_TOKEN@@" and fails
        # in a way nobody reads until the host has been silent for a week.
        print(f"Unsubstituted placeholders remain: {', '.join(missing)}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    # 0600: the file contains a bearer token for the ingest endpoint.
    args.out.chmod(0o600)
    print(f"wrote {args.out} (mode 600 — it holds the agent token)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
