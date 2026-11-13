"""Worker fixtures use the same resolved, host-built plans as assistant runs."""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.core.crypto import CryptoService
from app.core.db import Base, create_db_engine, create_session_factory
from app.models.execution import Run, SiteJob
from app.models.workflow import Site
from app.schemas.workflow import SiteWrite
from app.services.assistant_consumption import CONSUMING_FEATURES, build_plan
from app.services.resign import ResignService
from app.services.workflows import WorkflowService


@pytest.fixture
def approved_consumption_payload(settings):
    engine = create_db_engine("sqlite://")
    factory = create_session_factory(engine)
    initialized = False
    crypto = CryptoService(b"c" * 32)

    def approve(payload):
        nonlocal initialized
        payload = deepcopy(payload)
        consuming = [step for step in payload["steps"] if step.get("enabled", True)
                     and step["feature_type"] in CONSUMING_FEATURES]
        if payload.get("assistant_run") is not True or not consuming:
            return payload
        if not initialized:
            Base.metadata.create_all(engine)
            initialized = True
        with factory() as db:
            service = WorkflowService(db, settings, crypto)
            steps = []
            for step in consuming:
                config = deepcopy(step.get("config") or {})
                if step["feature_type"] == "post.buy_card":
                    config.setdefault("max_price", 10)
                steps.append({"step_key": step["step_key"], "feature_type": step["feature_type"],
                              "config": config})
            saved = service.create_site(SiteWrite(
                name="Approved worker fixture", base_url=payload.get("base_url", "https://site.test/"),
                support_resign=True, resign_max_days=365, steps=steps))
            site = db.get(Site, saved.id)
            resign = ResignService(db, settings)
            yesterday = datetime.now(resign.timezone).date() - timedelta(days=1)
            history = Run(trigger="manual", created_by="fixture", status="fail")
            db.add(history)
            db.flush()
            db.add(SiteJob(run_id=history.id, site_id=site.id, site_name=site.name,
                           lane="general", status="fail", finished_at=resign._start_utc(yesterday)))
            db.flush()
            plan = build_plan(db, service, site)
            resolved = {step.step_key: step for step in service.resolve_site(site.id).steps}
            for step in payload["steps"]:
                if step["step_key"] in resolved:
                    step["config"] = resolved[step["step_key"]].config
            payload.update(assistant_consumption_plan=plan, attempt=1, site_id=site.id,
                           resign_pending_dates=list(plan["review"]["dates"]),
                           resign_today=plan["review"]["today"], resign_unresolved_dates=[],
                           resign_max_days=site.resign_max_days)
        return payload

    yield approve
    engine.dispose()
