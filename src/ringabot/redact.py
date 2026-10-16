"""Log redaction. Secrets added at runtime are scrubbed, plus common token shapes."""

from __future__ import annotations

import logging
import re

_TOKEN_SHAPES = (
    re.compile(r"xai-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(auth_pass=)[^;\s]+"),
)


class Redactor:
    def __init__(self) -> None:
        self._secrets: list[str] = []

    def add(self, value: str | None) -> None:
        if not value:
            return
        text = value.strip()
        if len(text) < 6 or text in self._secrets:
            return
        self._secrets.append(text)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[redacted]")
        for pattern in _TOKEN_SHAPES:
            text = pattern.sub(
                lambda match: match.group(1) + "[redacted]" if match.lastindex else "[redacted]",
                text,
            )
        return text


REDACTOR = Redactor()


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = REDACTOR.scrub(record.msg)
        if record.args:
            record.args = tuple(
                REDACTOR.scrub(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def install_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    root = logging.getLogger()
    if not any(isinstance(item, RedactFilter) for item in root.filters):
        root.addFilter(RedactFilter())
