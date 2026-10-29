from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: int


class CryptoError(RuntimeError):
    pass


class CryptoService:
    """AES-256-GCM for integration config values.

    AAD binds the config key and key version, so a ciphertext cannot be moved
    to another integration field or key version without failing decryption.
    """

    def __init__(self, key: bytes, key_version: int = 1) -> None:
        if len(key) != 32:
            raise CryptoError("master key must be exactly 32 bytes")
        self._key = key
        self.key_version = key_version

    @staticmethod
    def aad(config_key: str, key_version: int) -> bytes:
        return f"{config_key}:{key_version}".encode("utf-8")

    def encrypt(self, config_key: str, plaintext: str) -> EncryptedValue:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            self.aad(config_key, self.key_version),
        )
        return EncryptedValue(
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=self.key_version,
        )

    def decrypt(
        self,
        config_key: str,
        ciphertext: bytes,
        nonce: bytes,
        key_version: int,
    ) -> str:
        if key_version != self.key_version:
            raise CryptoError(
                f"unsupported key version {key_version}; current={self.key_version}"
            )
        try:
            plaintext = AESGCM(self._key).decrypt(
                nonce,
                ciphertext,
                self.aad(config_key, key_version),
            )
        except Exception as exc:
            raise CryptoError("ciphertext decrypt failed") from exc
        return plaintext.decode("utf-8")

    def fingerprint(self, plaintext: str) -> str:
        digest = hmac.new(self._key, plaintext.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:8]
