"""Persona text and the mandatory AI disclosure spoken on every call."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DISCLOSURE = "I am an AI assistant, not a human. I cannot contact emergency services."
RECORDING_CLAUSE = " This call is being recorded."
GOODBYE = "Goodbye."


@dataclass(frozen=True)
class Persona:
    name: str
    instructions: str
    greeting: str
    voice: str


def load_persona_file(path: str) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.is_file():
        return {}
    raw = file.read_text(encoding="utf-8")
    if len(raw) > 65536:
        raise ValueError("persona file is too large")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("persona file must be a mapping")
    return {str(key): str(value) for key, value in data.items() if value is not None}


def resolve_persona(
    *,
    name: str,
    instructions: str,
    greeting: str,
    voice: str,
    file_path: str,
) -> Persona:
    file_data = load_persona_file(file_path)
    return Persona(
        name=name or file_data.get("name") or "Assistant",
        instructions=instructions or file_data.get("instructions") or "",
        greeting=greeting or file_data.get("greeting") or "",
        voice=voice or file_data.get("voice") or "aurora",
    )


def disclosure_text(recordings: bool) -> str:
    if recordings:
        return DISCLOSURE + RECORDING_CLAUSE
    return DISCLOSURE


def opening_line(recordings: bool, greeting: str) -> str:
    spoken = disclosure_text(recordings)
    extra = greeting.strip()
    if extra:
        return f"{spoken} {extra}"
    return spoken


def session_instructions(
    *,
    persona: Persona,
    briefing: str,
    questions: list[str],
    recordings: bool,
    inbound: bool = False,
) -> str:
    question_lines = "\n".join(f"- {item}" for item in questions) or "- (none)"
    recorded = (
        "If recordings are on, the opening already said so. Do not deny it."
        if recordings
        else "Do not claim the call is being recorded."
    )
    situation = "This is an incoming call to the user's number. " if inbound else ""
    return (
        f"You are {persona.name}, an AI voice assistant on a live phone call. "
        f"{situation}{DISCLOSURE} Never claim to be human. Never place or forward emergency calls. "
        "If the person needs emergency help, tell them to hang up and dial their local "
        f"emergency number themselves. {recorded}\n"
        f"Additional persona notes:\n{persona.instructions}\n"
        f"Purpose of this call:\n{briefing}\n"
        "Ask these questions, one at a time, and wait for an answer:\n"
        f"{question_lines}\n"
        "Keep turns short. When the task is done, say goodbye."
    )


def summary_prompt(questions: list[str]) -> str:
    listed = "; ".join(questions) if questions else "(none)"
    return (
        "The phone call has ended. Reply with JSON only, no markdown, shaped as "
        '{"recap": string, "answers": [{"question": string, "answer": string}]}. '
        f"Questions that were requested: {listed}."
    )
