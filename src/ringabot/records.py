"""Transcript and recording retention."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ringabot.store import Store, utc_now


def local_summary(questions: list[str], lines: list[dict]) -> str:
    spoken = [item["text"] for item in lines if item["role"] == "assistant"]
    recap = " ".join(spoken)[-800:] or "Call ended before a recap was captured."
    answers = [{"question": question, "answer": ""} for question in questions]
    return json.dumps({"recap": recap, "answers": answers})


def parse_summary(text: str, questions: list[str], lines: list[dict]) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return local_summary(questions, lines)
    if not isinstance(data, dict) or "recap" not in data:
        return local_summary(questions, lines)
    return json.dumps(data)


def enforce_retention(store: Store, retention_days: int) -> list[str]:
    removed: list[str] = []
    if retention_days < 0:
        return removed
    now = utc_now()
    for row in store.expired_recordings(retention_days, now):
        path = Path(row["path"])
        if path.is_file():
            path.unlink()
            removed.append(str(path))
        store.delete_recording_row(row["call_id"])
    store.delete_transcripts_before(now - timedelta(days=retention_days))
    return removed
