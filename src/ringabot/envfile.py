"""Quote values for a systemd EnvironmentFile."""

from __future__ import annotations


def quote_env(value: str) -> str:
    """Return a double-quoted EnvironmentFile value.

    systemd expands unquoted ``$`` and treats an unquoted space as the end of
    the value, so ``PERSONA_NAME=Example Assistant`` would load as ``Example``.
    Quotes and newlines are rejected so the file stays one assignment per line.
    """
    if any(ch in value for ch in '\r\n"'):
        raise ValueError("unsafe_env_value")
    return f'"{value}"'
