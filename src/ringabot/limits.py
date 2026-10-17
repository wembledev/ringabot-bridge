"""Abuse guards: destination allowlist, emergency block, daily cap, concurrency."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIGITS = re.compile(r"\D+")
_EMERGENCY = {"911", "988", "112", "999", "933", "000", "1911", "1988", "1933"}


class LimitError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Destination:
    e164: str
    digits: str


def normalize_e164(number: str) -> str:
    """E.164 with a leading plus. Emergency short codes are rejected."""
    raw = number.strip().replace(" ", "").replace("-", "").replace(".", "")
    if not raw.startswith("+"):
        raise LimitError("invalid_number")
    digits = _DIGITS.sub("", raw)
    if digits != raw[1:]:
        raise LimitError("invalid_number")
    if digits in _EMERGENCY:
        raise LimitError("emergency_not_allowed")
    if not (8 <= len(digits) <= 15):
        raise LimitError("invalid_number")
    return "+" + digits


def normalize_destination(number: str, prefixes: tuple[str, ...]) -> Destination:
    e164 = normalize_e164(number)
    if not any(e164.startswith(prefix) for prefix in prefixes):
        raise LimitError("destination_not_allowed")
    return Destination(e164=e164, digits=e164[1:])
