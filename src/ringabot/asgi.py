"""ASGI factory. uvicorn imports this and calls build() after the env is loaded."""

from __future__ import annotations

from ringabot.api import create_app
from ringabot.config import load_settings
from ringabot.store import Store


def build():
    settings = load_settings()
    store = Store(settings.data_dir / "ringabot.db", settings.master_key)
    return create_app(settings, store)
