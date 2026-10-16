import base64

import pytest

from ringabot.auth import StaticBearerAuth, ct_eq
from ringabot.crypto import decrypt, encrypt
from ringabot.limits import LimitError, normalize_destination


def test_constant_time_compare():
    assert ct_eq(b"placeholder-token-value", b"placeholder-token-value")
    assert not ct_eq(b"placeholder-token-value", b"placeholder-token-valu!")
    assert not ct_eq(b"short", b"placeholder-token-value")


def test_bearer_auth_rejects_near_miss():
    auth = StaticBearerAuth("placeholder-token-value")
    assert auth.authenticate("Bearer placeholder-token-value") == "local"
    assert auth.authenticate("Bearer placeholder-token-valu!") is None
    assert auth.authenticate("placeholder-token-value") is None
    assert auth.authenticate(None) is None
    assert StaticBearerAuth("").authenticate("Bearer placeholder-token-value") is None


def test_allowlist_and_emergency():
    ok = normalize_destination("+1 555-555-0100", ("+1",))
    assert ok.e164 == "+15555550100"
    with pytest.raises(LimitError) as denied:
        normalize_destination("+442075550100", ("+1",))
    assert denied.value.code == "destination_not_allowed"
    with pytest.raises(LimitError) as emergency:
        normalize_destination("+1911", ("+1",))
    assert emergency.value.code == "emergency_not_allowed"
    with pytest.raises(LimitError):
        normalize_destination("5555550100", ("+1",))


def test_secret_box_roundtrip_and_binding():
    key = b"0" * 32
    nonce, ciphertext = encrypt(key, b"placeholder-sip-pass", b"local:sip_password")
    assert b"placeholder-sip-pass" not in ciphertext
    assert decrypt(key, nonce, ciphertext, b"local:sip_password") == b"placeholder-sip-pass"
    with pytest.raises(Exception):
        decrypt(key, nonce, ciphertext, b"other:sip_password")
    assert len(base64.b64encode(key)) > 0
