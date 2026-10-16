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


def test_voice_url_blocks_metadata_literals():
    from ringabot.outbound import voice_url_allowed

    assert voice_url_allowed("ws://127.0.0.1:9/v1/realtime")
    assert voice_url_allowed("wss://api.x.ai/v1/realtime")
    assert not voice_url_allowed("ws://169.254.169.254/latest")
    assert not voice_url_allowed("wss://10.1.1.1/v1/realtime")
    assert not voice_url_allowed("http://api.x.ai/v1/realtime")


def test_silence_does_not_count_as_speech():
    from ringabot.call import audible

    assert not audible(b"")
    assert not audible(b"\x00\x00" * 160)
    assert not audible(b"\x01\x00" * 80)
    assert audible(b"\x00\x10" * 4)


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
