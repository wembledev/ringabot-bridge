import base64

import pytest

from ringabot.config import Settings

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()


class FakeXai:
    def __init__(self):
        self.hangups: list[str] = []
        self.refers: list[tuple[str, str]] = []

    async def hangup(self, call_id):
        self.hangups.append(call_id)
        return 200

    async def refer(self, call_id, uri):
        self.refers.append((call_id, uri))
        return 200, "{}"


@pytest.fixture
def settings():
    return Settings.from_env(
        {
            "XAI_API_KEY": "test-key-placeholder",
            "XAI_WEBHOOK_SECRET": SECRET,
            "API_TOKEN": "admin-placeholder",
            "DID": "6045550100",
            "TRANSFER_TARGETS": "desk=tel:+16045550111",
        }
    )
