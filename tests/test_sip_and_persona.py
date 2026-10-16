from pathlib import Path

import pytest

from ringabot.persona import DISCLOSURE, opening_line, resolve_persona, session_instructions
from ringabot.sip import SipError, account_line, write_config


def test_account_line_rejects_injection():
    line = account_line(
        user="example-user",
        password="placeholder-sip-pass",
        server="sip.example.com",
        did="+15555550100",
        regint=0,
    )
    assert "auth_pass=placeholder-sip-pass" in line
    assert "regint=0" in line
    with pytest.raises(SipError):
        account_line(
            user="example-user",
            password="bad;pass",
            server="sip.example.com",
            did="+15555550100",
            regint=0,
        )


def test_config_file_is_private(tmp_path: Path):
    write_config(
        tmp_path / "cfg",
        user="example-user",
        password="placeholder-sip-pass",
        server="sip.example.com",
        did="+15555550100",
        ctrl_port=4444,
        play_device="play",
        mic_device="mic",
        regint=0,
    )
    accounts = tmp_path / "cfg" / "accounts"
    assert accounts.stat().st_mode & 0o777 == 0o600
    assert "placeholder-sip-pass" in accounts.read_text(encoding="utf-8")


def test_disclosure_cannot_be_removed(tmp_path: Path):
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(
        "name: Example Assistant\ninstructions: Be brief.\ngreeting: Hello.\nvoice: aurora\n",
        encoding="utf-8",
    )
    persona = resolve_persona(
        name="",
        instructions="",
        greeting="",
        voice="",
        file_path=str(persona_path),
    )
    spoken = opening_line(False, persona.greeting)
    assert spoken.startswith(DISCLOSURE)
    assert "Hello." in spoken
    recorded = opening_line(True, "")
    assert "being recorded" in recorded
    text = session_instructions(
        persona=persona,
        briefing="Confirm the picnic.",
        questions=["When?"],
        recordings=False,
    )
    assert DISCLOSURE in text
    assert "Confirm the picnic." in text
    assert "placeholder-sip-pass" not in text
