from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth as auth_api
from app.api.v1 import (
    analytics,
    artifacts as artifacts_api,
    features,
    health,
    integrations,
    lane_settings,
    lanes,
    notifications,
    picker,
    plugin_runtime,
    runs,
    scheduler as scheduler_api,
    sites,
    secrets,
    templates,
)
from app.core.auth import LoginRateLimiter
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.core.db import Base, create_db_engine, create_session_factory
from app.features.plugin_registry import clear_plugin_registry, refresh_plugin_registry
from app.services.artifacts import ArtifactCleanupRunner
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from app.services.scheduler import Scheduler
from app.services.scheduler_settings import reset_next_run_for_timezone
from app.models.auth import AuthSession, User  # noqa: F401  (register tables)
from app.models.execution import JobStep, Run, SiteJob  # noqa: F401  (register tables)
from app.models.integration import IntegrationConfig  # noqa: F401  (register table)
from app.models.plugin import PluginRegistration  # noqa: F401  (register table)
from app.models.setting import AppSetting  # noqa: F401  (register table)
from app.models.workflow import (  # noqa: F401  (register tables)
    Site,
    SiteCredential,
    SiteResignRecord,
    Template,
    WorkflowGroup,
    WorkflowSignal,
    WorkflowStep,
)
from app.services.auth import bootstrap_admin


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    crypto = CryptoService(settings.master_key_bytes())
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    bootstrap = bootstrap_admin(session_factory, settings)
    clear_plugin_registry()
    with session_factory() as session:
        refresh_plugin_registry(session)

    executor = ExecutionManager(session_factory, settings, crypto)
    scheduler = Scheduler(session_factory, settings, crypto, executor)
    artifact_cleanup = ArtifactCleanupRunner(session_factory, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        with session_factory() as session:
            reset_next_run_for_timezone(session, settings.scheduler_timezone)
            ExecutionService(session, settings, crypto).recover_interrupted()
        await scheduler.start()
        await artifact_cleanup.start()
        yield
        await artifact_cleanup.stop()
        await scheduler.stop()
        await executor.shutdown()

    app = FastAPI(
        title="SiteFlow API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.crypto = crypto
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.bootstrap = bootstrap
    app.state.rate_limiter = LoginRateLimiter()
    app.state.executor = executor
    app.state.scheduler = scheduler
    app.state.artifact_cleanup = artifact_cleanup

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(secrets.router)
    app.include_router(auth_api.router)
    app.include_router(integrations.router)
    app.include_router(notifications.router)
    app.include_router(plugin_runtime.router)
    app.include_router(lanes.router)
    app.include_router(lane_settings.router)
    app.include_router(features.router)
    app.include_router(templates.router)
    app.include_router(sites.router)
    app.include_router(runs.router)
    app.include_router(runs.stream_router)
    app.include_router(analytics.router)
    app.include_router(artifacts_api.router)
    app.include_router(runs.artifact_router)
    app.include_router(picker.router)
    app.include_router(scheduler_api.router)
    return app
