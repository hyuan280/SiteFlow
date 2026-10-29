from sqlalchemy import delete, select

from app.models.plugin import PluginAuditEvent

EVENTS = {"remove", "restore", "metadata_change", "register", "approve", "revoke", "disable", "enable", "lease_issue", "lease_consume", "lease_reject", "provider_resolve"}
RESULTS = {"success", "denied", "failed"}
REASONS = {"ok", "scope_denied", "provider_failed", "metadata_changed", "admin"}


def record(session, event, result="success", reason="ok", **scope):
    if event not in EVENTS or result not in RESULTS or reason not in REASONS:
        raise ValueError("Invalid audit code")
    session.add(PluginAuditEvent(event=event, action=event, result=result, reason=reason, **scope))
    session.flush()
    cutoff = session.scalar(select(PluginAuditEvent.id).order_by(PluginAuditEvent.id.desc()).offset(9999).limit(1))
    if cutoff is not None:
        session.execute(delete(PluginAuditEvent).where(PluginAuditEvent.id < cutoff))
