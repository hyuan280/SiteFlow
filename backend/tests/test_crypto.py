from __future__ import annotations

import base64
import secrets

import pytest

from app.core.crypto import CryptoError, CryptoService


def test_roundtrip_and_aad_binding() -> None:
    crypto = CryptoService(secrets.token_bytes(32))
    encrypted = crypto.encrypt("proxy.url", "socks5://user:pass@host:1080")
    assert crypto.decrypt("proxy.url", encrypted.ciphertext, encrypted.nonce, encrypted.key_version) == (
        "socks5://user:pass@host:1080"
    )
    with pytest.raises(CryptoError):
        crypto.decrypt("twofauth.token", encrypted.ciphertext, encrypted.nonce, encrypted.key_version)


def test_fingerprint_is_stable_and_short() -> None:
    key = base64.b64decode(base64.b64encode(secrets.token_bytes(32)))
    crypto = CryptoService(key)
    first = crypto.fingerprint("secret")
    second = crypto.fingerprint("secret")
    assert first == second
    assert len(first) == 8
    assert crypto.fingerprint("other") != first
