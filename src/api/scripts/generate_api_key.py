"""Generate one API credential entry for ``API_CREDENTIALS_JSON``.

Run interactively, once per issued credential:

    ./venv/bin/python -m src.api.scripts.generate_api_key --caller-type web --name "Next.js BFF"
    ./venv/bin/python -m src.api.scripts.generate_api_key --caller-type integration \\
        --name "Acme SCADA push" --tenant acme_greenhouse_1

This tool writes nothing to the repository or to disk: the raw key is
printed once to stdout for the operator to copy into a secrets manager, and
the ``API_CREDENTIALS_JSON`` snippet (containing only the *hash*, never the
raw key) is printed alongside it for pasting into the deployment's env
vars. Nothing here is logged — hand both values over outside the repo and
outside chat, then discard this terminal's scrollback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys

_KEY_BYTES = 32  # 256 bits of entropy, URL-safe encoded.


def _generate_raw_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _build_key_id(caller_type: str) -> str:
    return f"{caller_type}_{secrets.token_hex(4)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caller-type", choices=("web", "integration"), required=True)
    parser.add_argument("--name", required=True, help="Human label for this credential")
    parser.add_argument(
        "--tenant",
        action="append",
        default=[],
        dest="tenants",
        help="Tenant id this credential may act as (repeatable). "
        "Required for --caller-type integration (exactly one); "
        "omit for --caller-type web to default to '*' (all tenants).",
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        dest="scopes",
        help="Optional scope string (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.caller_type == "integration":
        if len(args.tenants) != 1:
            parser.error("--caller-type integration requires exactly one --tenant")
        tenant_ids: list[str] | str = args.tenants
    else:
        tenant_ids = args.tenants if args.tenants else "*"

    raw_key = _generate_raw_key()
    key_id = _build_key_id(args.caller_type)
    credential = {
        "key_hash": _hash_key(raw_key),
        "caller_type": args.caller_type,
        "name": args.name,
        "tenant_ids": tenant_ids,
        "scopes": args.scopes,
    }

    print("Raw key (hand this to the caller, outside the repo, outside chat):")
    print(f"  {raw_key}")
    print()
    print("Merge this entry into the deployment's API_CREDENTIALS_JSON:")
    print(f"  {json.dumps({key_id: credential})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
