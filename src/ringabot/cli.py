"""`ringabot serve` and `ringabot doctor`."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import uvicorn

from ringabot.config import load_env_file, load_settings
from ringabot.doctor import failed, format_report, run_doctor
from ringabot.store import Store

HELP = """ringabot-bridge

  ringabot serve          start the HTTP and MCP server on 127.0.0.1
  ringabot doctor         check the token, SIP REGISTER, xAI key, and TLS
  ringabot doctor --offline
                          check local files only

Configuration is environment variables. See .env.example.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _load_env()
    if not args or args[0] in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if args[0] == "doctor":
        return _doctor("--offline" in args)
    if args[0] == "serve":
        return _serve()
    print(HELP, file=sys.stderr)
    return 2


def _load_env() -> None:
    import os

    named = os.environ.get("RINGABOT_ENV_FILE", "").strip()
    candidates = [Path(named)] if named else [Path("/etc/ringabot/ringabot.env"), Path(".env")]
    for path in candidates:
        if path.is_file():
            load_env_file(path)
            os.environ.setdefault("RINGABOT_ENV_FILE", str(path))
            return


def _doctor(offline: bool) -> int:
    try:
        settings = load_settings()
    except SystemExit as exc:
        print(f"fail     config           {exc}")
        return 1
    store = Store(settings.data_dir / "ringabot.db", settings.master_key)
    try:
        checks = asyncio.run(run_doctor(settings, store, offline=offline))
    finally:
        store.close()
    print(format_report(checks))
    return 1 if failed(checks) else 0


def _serve() -> int:
    try:
        settings = load_settings()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    if len(settings.api_token) < 16:
        print("API_TOKEN must be at least 16 characters", file=sys.stderr)
        return 1
    uvicorn.run(
        "ringabot.asgi:build",
        host=settings.host,
        port=settings.port,
        factory=True,
        log_level=settings.log_level.lower(),
    )
    return 0
