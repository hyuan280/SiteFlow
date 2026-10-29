from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import CryptoError, CryptoService
from app.models.workflow import Site, SiteCredential, utcnow
from app.schemas.credential import SiteCredentialView


class CredentialError(ValueError):
    pass


class CredentialNotFoundError(CredentialError):
    pass


class CredentialNotConfiguredError(CredentialError):
    pass


class CredentialDecryptError(CredentialError):
    pass


@dataclass(frozen=True)
class SiteSecret:
    username: str
    password: str


def _aad(site_id: int, field: str) -> str:
    return f"site_credential:{site_id}:{field}"


def _mask_username(value: str) -> str:
    if "@" in value:
        local, domain = value.split("@", 1)
        visible = local[:1]
        return f"{visible}***@{domain}"
    if len(value) <= 2:
        return "***"
    return f"{value[0]}***{value[-1]}"


def _same_site(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }.get(text)


def _normalize_cookie(item: dict, base_url: str) -> dict | None:
    name = str(item.get("name") or "").strip()
    if not name or name.startswith("$"):
        return None
    cookie: dict = {"name": name, "value": str(item.get("value") or "")}
    domain = str(item.get("domain") or "").strip()
    if domain:
        cookie["domain"] = domain
        cookie["path"] = str(item.get("path") or "/")
    else:
        cookie["url"] = str(item.get("url") or base_url)
    if "secure" in item:
        cookie["secure"] = bool(item["secure"])
    if "httpOnly" in item:
        cookie["httpOnly"] = bool(item["httpOnly"])
    same_site = _same_site(item.get("sameSite"))
    if same_site:
        cookie["sameSite"] = same_site
    expires = item.get("expires", item.get("expirationDate"))
    if expires is not None:
        try:
            value = float(expires)
            if value > 0:
                cookie["expires"] = value
        except (TypeError, ValueError):
            pass
    return cookie


def parse_cookie_input(text: str, base_url: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw:
        raise CredentialError("Cookie 不能为空")
    parsed_url = urlparse(base_url)
    host = parsed_url.hostname or ""
    scheme = parsed_url.scheme or "https"

    items: list[dict] = []
    if raw[:1] in {"[", "{"}:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialError(f"Cookie JSON 解析失败: {exc}") from exc
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            if "name" in data and "value" in data:
                items = [data]
            else:
                items = [
                    {"name": str(name), "value": str(value)}
                    for name, value in data.items()
                ]
        else:
            raise CredentialError("Cookie JSON 必须是对象或数组")
    else:
        if raw.lower().startswith("cookie:"):
            raw = raw.split(":", 1)[1].strip()
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if not name:
                continue
            items.append(
                {
                    "name": name,
                    "value": value.strip(),
                    "domain": host,
                    "path": "/",
                    "secure": scheme == "https",
                }
            )

    cookies: dict[tuple[str, str, str], dict] = {}
    for item in items:
        cookie = _normalize_cookie(item, base_url)
        if cookie is None:
            continue
        domain = str(cookie.get("domain") or "")
        url = str(cookie.get("url") or "")
        path = str(cookie.get("path") or "")
        key = (cookie["name"], domain or url, path)
        cookies[key] = cookie
    result = list(cookies.values())
    if not result:
        raise CredentialError("没有解析到有效 Cookie")
    if len(result) > 300:
        raise CredentialError("Cookie 数量不能超过 300 条")
    return result


class CredentialService:
    def __init__(self, session: Session, crypto: CryptoService) -> None:
        self.session = session
        self.crypto = crypto
        self._rows_by_site: dict[int, SiteCredential] | None = None

    def preload(self) -> None:
        self._rows_by_site = {
            row.site_id: row
            for row in self.session.scalars(select(SiteCredential)).all()
        }

    def _site(self, site_id: int) -> Site:
        site = self.session.get(Site, site_id)
        if site is None:
            raise CredentialNotFoundError(f"站点 {site_id} 不存在")
        return site

    def _row(self, site_id: int) -> SiteCredential | None:
        if self._rows_by_site is not None:
            return self._rows_by_site.get(site_id)
        return self.session.get(SiteCredential, site_id)

    def _new_row(self, site_id: int) -> SiteCredential:
        row = SiteCredential(
            site_id=site_id,
            username_ciphertext=b"",
            username_nonce=b"",
            password_ciphertext=b"",
            password_nonce=b"",
            key_version=self.crypto.key_version,
            updated_at=utcnow(),
        )
        self.session.add(row)
        return row

    def _decrypt(
        self,
        site_id: int,
        field: str,
        ciphertext: bytes,
        nonce: bytes,
        version: int,
    ) -> str:
        try:
            return self.crypto.decrypt(_aad(site_id, field), ciphertext, nonce, version)
        except CryptoError as exc:
            raise CredentialDecryptError(f"{field} 解密失败: {exc}") from exc

    def view(self, site_id: int) -> SiteCredentialView:
        self._site(site_id)
        row = self._row(site_id)
        if row is None:
            return SiteCredentialView(configured=False)
        configured = bool(row.username_ciphertext and row.password_ciphertext)
        username_masked = None
        error = None
        if configured:
            try:
                username = self._decrypt(
                    site_id,
                    "username",
                    row.username_ciphertext,
                    row.username_nonce,
                    row.key_version,
                )
                username_masked = _mask_username(username)
            except CredentialDecryptError as exc:
                error = str(exc)
        cookies_configured = bool(row.cookies_ciphertext)
        return SiteCredentialView(
            configured=configured and error is None,
            username_masked=username_masked,
            cookies_configured=cookies_configured,
            cookie_count=row.cookie_count if cookies_configured else 0,
            updated_at=row.updated_at,
            cookies_updated_at=row.cookies_updated_at,
            last_verified_at=row.last_verified_at,
            last_error=row.last_error,
            error=error,
        )

    def resolve(self, site_id: int) -> SiteSecret:
        self._site(site_id)
        row = self._row(site_id)
        if row is None or not row.username_ciphertext or not row.password_ciphertext:
            raise CredentialNotConfiguredError(f"站点 {site_id} 未配置登录凭据")
        return SiteSecret(
            username=self._decrypt(
                site_id,
                "username",
                row.username_ciphertext,
                row.username_nonce,
                row.key_version,
            ),
            password=self._decrypt(
                site_id,
                "password",
                row.password_ciphertext,
                row.password_nonce,
                row.key_version,
            ),
        )

    def resolve_cookies(self, site_id: int) -> list[dict] | None:
        self._site(site_id)
        row = self._row(site_id)
        if row is None or not row.cookies_ciphertext or not row.cookies_nonce:
            return None
        try:
            text = self.crypto.decrypt(
                _aad(site_id, "cookies"),
                row.cookies_ciphertext,
                row.cookies_nonce,
                row.key_version,
            )
            value = json.loads(text)
        except (CryptoError, json.JSONDecodeError) as exc:
            raise CredentialDecryptError(f"Cookie 解密失败: {exc}") from exc
        if not isinstance(value, list):
            raise CredentialDecryptError("Cookie 数据格式无效")
        return [item for item in value if isinstance(item, dict)]

    def update(
        self,
        site_id: int,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> SiteCredentialView:
        self._site(site_id)
        row = self._row(site_id)
        if username is None and password is None:
            raise CredentialError("请至少填写用户名或密码")
        if row is None or not row.username_ciphertext or not row.password_ciphertext:
            if username is None or password is None:
                raise CredentialError("首次配置需要同时填写用户名和密码")
            if row is None:
                row = self._new_row(site_id)
        if username is not None:
            username = username.strip()
            if not username:
                raise CredentialError("用户名不能为空")
            encrypted = self.crypto.encrypt(_aad(site_id, "username"), username)
            row.username_ciphertext = encrypted.ciphertext
            row.username_nonce = encrypted.nonce
        if password is not None:
            if not password:
                raise CredentialError("密码不能为空")
            encrypted = self.crypto.encrypt(_aad(site_id, "password"), password)
            row.password_ciphertext = encrypted.ciphertext
            row.password_nonce = encrypted.nonce
        row.last_error = None
        row.updated_at = utcnow()
        self.session.commit()
        return self.view(site_id)

    def _store_cookies(self, site_id: int, cookies: list[dict]) -> SiteCredentialView:
        row = self._row(site_id) or self._new_row(site_id)
        encrypted = self.crypto.encrypt(
            _aad(site_id, "cookies"),
            json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
        )
        row.cookies_ciphertext = encrypted.ciphertext
        row.cookies_nonce = encrypted.nonce
        row.cookie_count = len(cookies)
        row.cookies_updated_at = utcnow()
        self.session.commit()
        return self.view(site_id)

    def update_cookies(self, site_id: int, text: str) -> SiteCredentialView:
        site = self._site(site_id)
        cookies = parse_cookie_input(text, site.base_url)
        return self._store_cookies(site_id, cookies)

    def store_exported_cookies(self, site_id: int, cookies: list[dict]) -> SiteCredentialView:
        site = self._site(site_id)
        if not cookies:
            self.delete_cookies(site_id)
            return self.view(site_id)
        normalized = parse_cookie_input(json.dumps(cookies), site.base_url)
        return self._store_cookies(site_id, normalized)

    def delete(self, site_id: int) -> None:
        self._site(site_id)
        row = self._row(site_id)
        if row is None:
            return
        row.username_ciphertext = b""
        row.username_nonce = b""
        row.password_ciphertext = b""
        row.password_nonce = b""
        row.last_error = None
        row.updated_at = utcnow()
        if not row.cookies_ciphertext:
            self.session.delete(row)
        self.session.commit()

    def delete_cookies(self, site_id: int) -> None:
        self._site(site_id)
        row = self._row(site_id)
        if row is None:
            return
        row.cookies_ciphertext = None
        row.cookies_nonce = None
        row.cookie_count = 0
        row.cookies_updated_at = None
        if not row.username_ciphertext:
            self.session.delete(row)
        self.session.commit()
