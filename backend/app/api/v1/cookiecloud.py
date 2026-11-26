from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_db, get_readonly_current_session, get_settings, require_csrf
from app.core.auth import safe_compare
from app.core.config import Settings
from app.core.crypto import CryptoError, CryptoService
from app.models.cookiecloud import CookieCloudServer
from app.models.workflow import Site
from app.services.cookiecloud import cookie_payload, encrypt_payload
from app.services.credentials import CredentialDecryptError

ID_PATTERN = r"^[a-z0-9_-]{1,64}$"
UUID_PATTERN = r"^[a-zA-Z0-9_-]{1,128}$"
NO_STORE = {"Cache-Control": "no-store"}
ServerId = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
ServerPath = Annotated[str, Path(pattern=ID_PATTERN)]
ClientUUID = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]


class CookieCloudAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = re.sub(r"^(/cookiecloud/[^/]+/get/).+", r"\1[redacted]", args[2])
            record.args = (*args[:2], path, *args[3:])
        return True


access_filter = CookieCloudAccessFilter()


class _SettingsRoute(APIRoute):
    def get_route_handler(self):  # type: ignore[no-untyped-def]
        handler = super().get_route_handler()

        async def safe_handler(request):  # type: ignore[no-untyped-def]
            try:
                response = await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422, "CookieCloud 配置无效，请检查输入。", headers=NO_STORE,
                ) from None
            except CryptoError:
                raise HTTPException(
                    503, "CookieCloud 连接凭据暂不可用。", headers=NO_STORE,
                ) from None
            response.headers.update(NO_STORE)
            return response

        return safe_handler


settings_router = APIRouter(
    prefix="/api/v1/cookiecloud/servers",
    tags=["cookiecloud"],
    dependencies=[Depends(get_readonly_current_session)],
    route_class=_SettingsRoute,
)
public_router = APIRouter(prefix="/cookiecloud", tags=["cookiecloud"])


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    site_keys: list[UUID]
    all_sites: bool = False
    uuid: ClientUUID
    password: Annotated[SecretStr, Field(min_length=1, max_length=256)] | None = None


class ServerCreate(ServerSettings):
    id: ServerId
    password: Annotated[SecretStr, Field(min_length=1, max_length=256)]


class ServerView(BaseModel):
    id: str
    name: str
    site_keys: list[str]
    all_sites: bool
    created_at: datetime


class ServerConnection(BaseModel):
    uuid: str
    password: str


def _view(row: CookieCloudServer, existing: set[str]) -> ServerView:
    return ServerView(
        id=row.id,
        name=row.name,
        site_keys=[key for key in row.site_keys if key in existing],
        all_sites=row.all_sites,
        created_at=row.created_at.replace(tzinfo=timezone.utc),
    )


def _row(session: Session, server_id: str) -> CookieCloudServer:
    row = session.get(CookieCloudServer, server_id)
    if row is None:
        raise HTTPException(404, "CookieCloud 配置不存在。", headers=NO_STORE)
    return row


def _connection(row: CookieCloudServer, crypto: CryptoService) -> dict[str, str]:
    plaintext = crypto.decrypt(f"cookiecloud:{row.id}", row.ciphertext, row.nonce, row.key_version)
    return json.loads(plaintext)


def _save(
    session: Session, crypto: CryptoService, row: CookieCloudServer, form: ServerSettings,
) -> ServerView:
    keys = [] if form.all_sites else list(dict.fromkeys(str(key) for key in form.site_keys))
    existing = set(session.scalars(select(Site.history_key))) if keys else set()
    if not existing.issuperset(keys):
        raise HTTPException(422, "部分站点已不存在，请刷新列表后重试。", headers=NO_STORE)
    if form.password is not None:
        password = form.password.get_secret_value()
    else:
        password = _connection(row, crypto)["password"]
    plaintext = json.dumps({"uuid": form.uuid, "password": password})
    encrypted = crypto.encrypt(f"cookiecloud:{row.id}", plaintext)
    row.name = form.name
    row.all_sites = form.all_sites
    row.site_keys = keys
    row.ciphertext = encrypted.ciphertext
    row.nonce = encrypted.nonce
    row.key_version = encrypted.key_version
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "该 CookieCloud ID 已存在。", headers=NO_STORE) from None
    return _view(row, existing)


@settings_router.get("")
def list_servers(session: Session = Depends(get_db)) -> list[ServerView]:
    statement = select(CookieCloudServer).order_by(CookieCloudServer.created_at, CookieCloudServer.id)
    rows = list(session.scalars(statement))
    if not rows:
        return []
    existing = set(session.scalars(select(Site.history_key)))
    return [_view(row, existing) for row in rows]


@settings_router.get("/endpoint-base")
def endpoint_base(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"base_url": settings.siteflow_domain.strip().rstrip("/")}


@settings_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)])
def create_server(
    form: ServerCreate, session: Session = Depends(get_db), crypto: CryptoService = Depends(get_crypto),
) -> ServerView:
    return _save(session, crypto, CookieCloudServer(id=form.id), form)


@settings_router.get("/{server_id}/connection")
def server_connection(
    server_id: ServerPath, session: Session = Depends(get_db), crypto: CryptoService = Depends(get_crypto),
) -> ServerConnection:
    return ServerConnection(**_connection(_row(session, server_id), crypto))


@settings_router.put("/{server_id}", dependencies=[Depends(require_csrf)])
def update_server(
    server_id: ServerPath, form: ServerSettings,
    session: Session = Depends(get_db), crypto: CryptoService = Depends(get_crypto),
) -> ServerView:
    return _save(session, crypto, _row(session, server_id), form)


@settings_router.delete("/{server_id}", status_code=204, dependencies=[Depends(require_csrf)])
def delete_server(server_id: ServerPath, session: Session = Depends(get_db)) -> Response:
    session.delete(_row(session, server_id))
    session.commit()
    return Response(status_code=204)


@public_router.get("/{server_id}", include_in_schema=False)
@public_router.get("/{server_id}/", include_in_schema=False)
def check_connection(
    server_id: str, response: Response, session: Session = Depends(get_db),
) -> dict[str, str]:
    # PyCookieCloud requires a successful GET to the base URL before downloading.
    if not re.fullmatch(ID_PATTERN, server_id) or session.get(CookieCloudServer, server_id) is None:
        raise HTTPException(404, "Not Found", headers=NO_STORE)
    response.headers.update(NO_STORE)
    return {"status": "OK"}


@public_router.get("/{server_id}/get/{uuid}", include_in_schema=False)
@public_router.post("/{server_id}/get/{uuid}", include_in_schema=False)
def download(
    server_id: str, uuid: str, response: Response,
    crypto_type: Literal["legacy", "aes-128-cbc-fixed"] = "legacy",
    session: Session = Depends(get_db), crypto: CryptoService = Depends(get_crypto),
) -> dict[str, str]:
    not_found = HTTPException(404, "Not Found", headers=NO_STORE)
    if not re.fullmatch(ID_PATTERN, server_id) or not re.fullmatch(UUID_PATTERN, uuid):
        raise not_found
    row = session.get(CookieCloudServer, server_id)
    if row is None:
        raise not_found
    try:
        connection = _connection(row, crypto)
        if not safe_compare(uuid, connection["uuid"]):
            raise not_found
        payload = cookie_payload(session, crypto, None if row.all_sites else row.site_keys)
    except (CryptoError, CredentialDecryptError):
        raise HTTPException(503, "CookieCloud data unavailable", headers=NO_STORE) from None
    response.headers.update(NO_STORE)
    return {
        "encrypted": encrypt_payload(uuid, connection["password"], payload, crypto_type),
        "crypto_type": crypto_type,
    }
