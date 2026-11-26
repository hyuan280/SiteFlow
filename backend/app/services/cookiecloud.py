from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import CryptoService
from app.models.workflow import Site, SiteCredential, utcnow
from app.services.credentials import CredentialService

# Keep each IN clause below the bind-variable limit of older SQLite builds.
SITE_KEY_BATCH_SIZE = 500


def _client_cookie(cookie: dict, base_url: str, now: float) -> dict | None:
    explicit_domain = cookie.get("domain")
    url = urlsplit(cookie.get("url") or base_url)
    domain = explicit_domain or url.hostname
    if not domain or not cookie.get("name"):
        return None
    expires = cookie.get("expires")
    persistent = isinstance(expires, (float, int)) and math.isfinite(expires) and expires > 0
    if persistent and expires <= now:
        return None
    path = cookie.get("path")
    if not path and not explicit_domain:
        # RFC 6265 default path for URL-based cookies.
        path = url.path.rpartition("/")[0] or "/"
    result = {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": domain,
        "path": path or "/",
        "secure": cookie.get("secure", url.scheme == "https" and not explicit_domain),
        "httpOnly": cookie.get("httpOnly", False),
        "sameSite": {"Lax": "lax", "Strict": "strict", "None": "no_restriction"}.get(
            cookie.get("sameSite"), "unspecified"
        ),
        "hostOnly": not domain.startswith("."),
        "session": not persistent,
    }
    if persistent:
        result["expirationDate"] = expires
    return result


def cookie_payload(session: Session, crypto: CryptoService, site_keys: list[str] | None) -> dict:
    now = utcnow()
    statement = select(Site.base_url, SiteCredential).join(
        SiteCredential, SiteCredential.site_id == Site.id,
    ).where(SiteCredential.cookies_ciphertext.is_not(None))
    rows = []
    if site_keys is None:
        rows.extend(session.execute(statement))
    else:
        for offset in range(0, len(site_keys), SITE_KEY_BATCH_SIZE):
            batch = site_keys[offset:offset + SITE_KEY_BATCH_SIZE]
            rows.extend(session.execute(statement.where(Site.history_key.in_(batch))))
    credentials = CredentialService(session, crypto)
    grouped: dict[str, list[dict]] = {}
    for base_url, row in sorted(rows, key=lambda result: result[1].site_id):
        for stored in credentials.cookies_from_row(row) or []:
            try:
                cookie = _client_cookie(stored, base_url, now.timestamp())
            except ValueError:  # A malformed URL in an older manually imported cookie.
                continue
            if cookie is not None:
                grouped.setdefault(cookie["domain"], []).append(cookie)
    return {"cookie_data": grouped, "local_storage_data": {}, "update_time": now.isoformat()}


def encrypt_payload(uuid: str, password: str, payload: dict, crypto_type: str) -> str:
    # Match CookieCloud's CryptoJS wire formats; local storage uses CryptoService instead.
    passphrase = hashlib.md5(f"{uuid}-{password}".encode(), usedforsecurity=False)
    key = passphrase.hexdigest()[:16].encode("ascii")
    iv = bytes(16)
    prefix = b""
    if crypto_type == "legacy":
        salt = secrets.token_bytes(8)
        prefix = b"Salted__" + salt
        # OpenSSL EVP_BytesToKey (MD5, one round), as used by CryptoJS passphrase mode.
        material = b""
        block = b""
        for _ in range(3):
            block = hashlib.md5(block + key + salt, usedforsecurity=False).digest()
            material += block
        key, iv = material[:32], material[32:]
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padder.update(plaintext) + padder.finalize())
    return base64.b64encode(prefix + ciphertext + encryptor.finalize()).decode("ascii")
