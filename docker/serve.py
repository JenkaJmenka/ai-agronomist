#!/usr/bin/env python3
"""
Dual-stack uvicorn launcher for Railway deployment.

CPython unconditionally sets IPV6_V6ONLY=1 on AF_INET6 sockets created inside
asyncio (loop.create_server), so a plain `--host ::` uvicorn invocation is
IPv6-only — it refuses IPv4 connections.  This script pre-creates the socket
with dualstack_ipv6=True (which explicitly clears IPV6_V6ONLY) and hands the
open fd to uvicorn, bypassing asyncio's socket setup entirely.
"""
import os
import socket
import sys

# Running as `python docker/serve.py` puts the docker/ directory on sys.path[0].
# The src package lives at the repo root (the parent of docker/), so we insert
# it explicitly -- mirroring what `python -m uvicorn` achieves via the -m flag.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import uvicorn  # noqa: E402 -- must come after sys.path fix

PORT = int(os.environ.get("PORT", 8000))

if socket.has_dualstack_ipv6():
    sock = socket.create_server(
        ("", PORT),
        family=socket.AF_INET6,
        dualstack_ipv6=True,
        backlog=2048,
    )
else:
    # Platform does not support dual-stack; fall back to IPv4-only.
    sock = socket.create_server(("0.0.0.0", PORT), backlog=2048)

# Pass the already-bound, listening fd to uvicorn.  Do NOT pass host or port
# alongside fd — uvicorn ignores them when fd is set, but it avoids confusion.
uvicorn.run("src.api.main:app", fd=sock.fileno())
