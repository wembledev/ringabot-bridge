import os
import stat
from pathlib import Path

from ringabot.envfile import set_value

ROOT = Path(__file__).resolve().parents[1]


def test_set_value_replaces_and_keeps_0600(tmp_path):
    p = tmp_path / "x.env"
    p.write_text("A=1\nXAI_WEBHOOK_SECRET=\n")
    set_value(str(p), "XAI_WEBHOOK_SECRET", "new")
    assert p.read_text() == "A=1\nXAI_WEBHOOK_SECRET=new\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_service_binds_nothing_public_and_has_no_audio():
    unit = (ROOT / "deploy/ringabot.service").read_text()
    assert "snd-aloop" not in unit and "audio" not in unit.lower().split("description")[0]
    assert "MemoryMax=" in unit


def test_no_baresip_left():
    for path in (ROOT / "src").rglob("*.py"):
        assert "baresip" not in path.read_text().lower()
