"""Allowlisted OpenAI chat usage, isolated per turn and per actual HTTP attempt.

Missing/invalid counters remain unknown. Cache hits are a subset of prompt tokens;
DeepSeek's hit counter is a fallback, never an additional charge. No estimates,
provider metadata, or tool-result usage are collected.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
from uuid import uuid4

from app.services.config_ai_generation import _has_secret, _strict_json


MAX_SAFE_INTEGER = 9007199254740991
COUNTERS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
            "reasoning_tokens")
_observer = ContextVar("assistant_usage_observer", default=None)
_request = ContextVar("assistant_usage_request", default=None)
_logger = logging.getLogger(__name__)


def _counter(value):
    return value if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER else None


def parse_usage(output) -> dict:
    """Parse only Chat Completions usage; do not infer totals or missing counters."""
    usage = output.get("usage") if isinstance(output, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    result = {key: _counter(usage.get(key)) for key in COUNTERS[:3]}
    prompt = usage.get("prompt_tokens_details")
    completion = usage.get("completion_tokens_details")
    cached = _counter(prompt.get("cached_tokens")) if isinstance(prompt, dict) else None
    result["cached_tokens"] = (cached if cached is not None else
                               _counter(usage.get("prompt_cache_hit_tokens")))
    result["reasoning_tokens"] = (_counter(completion.get("reasoning_tokens"))
                                  if isinstance(completion, dict) else None)
    return result


def contains_embedded_secret(output, key):
    """Inspect JSON carried inside chat fields without accepting its protocol.

    Only one embedded layer is part of the chat protocol. Invalid embedded JSON
    is left to answer validation; it does not erase otherwise safe known usage.
    """
    choices = output.get("choices") if isinstance(output, dict) else None
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        strings = [message.get("content")]
        calls = message.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                strings.append(function.get("arguments"))
        for value in strings:
            if not isinstance(value, str):
                continue
            try:
                decoded = _strict_json(value)
            except (ValueError, RecursionError):
                continue
            if _has_secret(decoded, key):
                return True
    return False


@contextmanager
def usage_observer(callback):
    """Install a synchronous, sanitized-event sink for this turn only."""
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def usage_request(purpose, *, context_budget_tokens=None, context_revision=None,
                  target_revision=None):
    """One attempted request, including cancellation/HTTP/JSON failures, exactly once."""
    state = {"id": str(uuid4()), "kind": purpose,
              "created_at": datetime.now(timezone.utc).isoformat(), "reported": False}
    if purpose == "conversation":
        state.update(context_budget_tokens=context_budget_tokens,
                     context_revision=context_revision, target_revision=target_revision)
    token = _request.set(state)
    try:
        yield
    finally:
        try:
            report_usage(None, purpose)
        finally:
            _request.reset(token)


def report_usage(output, purpose):
    """Call only AFTER the configured-key secret check, before protocol validation."""
    state = _request.get()
    callback = _observer.get()
    if not state or state["kind"] != purpose or state["reported"]:
        return
    state["reported"] = True
    if callback is not None:
        event = {key: state[key] for key in ("id", "kind", "created_at")}
        if purpose == "conversation":
            event.update({key: state[key] for key in (
                "context_budget_tokens", "context_revision", "target_revision")})
        event.update(parse_usage(output))
        try:
            callback(event)
        except Exception:
            # Telemetry must not discard an answer. Never log provider/DB exceptions.
            _logger.warning("Assistant usage could not be persisted")


def accumulate_usage(previous, event):
    """Keep 100 recent calls, but lifetime known-counter totals (saturating at JS max)."""
    previous = previous or {}
    totals = dict(previous.get("totals", {"request_count": 0, **dict.fromkeys(COUNTERS, 0)}))
    totals["request_count"] = min(MAX_SAFE_INTEGER, totals["request_count"] + 1)
    for key in COUNTERS:
        if event[key] is not None:
            totals[key] = min(MAX_SAFE_INTEGER, totals[key] + event[key])
    conversation = event["kind"] == "conversation"
    missing = any(event[key] is None for key in COUNTERS[:3])
    current = {key: previous.get(key) for key in (
        "last_context_tokens", "last_context_budget_tokens", "last_context_prompt_tokens",
        "last_context_completion_tokens", "last_context_cached_tokens",
        "last_context_revision", "last_context_target_revision")}
    current["last_context_usage_missing"] = previous.get("last_context_usage_missing", False)
    if conversation:
        prompt, completion = event["prompt_tokens"], event["completion_tokens"]
        budget = _counter(event.get("context_budget_tokens"))
        budget = budget if budget and 16000 <= budget <= 1000000 else None
        measured = (prompt + completion if prompt is not None and completion is not None
                    and prompt + completion <= MAX_SAFE_INTEGER and budget is not None else None)
        current["last_context_usage_missing"] = measured is None
        if measured is not None:
            current.update(last_context_tokens=measured, last_context_budget_tokens=budget,
                           last_context_prompt_tokens=prompt, last_context_completion_tokens=completion,
                           last_context_cached_tokens=event["cached_tokens"],
                           last_context_revision=_counter(event.get("context_revision")),
                           last_context_target_revision=_counter(event.get("target_revision")))
        # Incomplete calls still enter the ledger/totals, but cannot replace any
        # part of the last valid measurement. The flag labels it as historical.
    # Old ledgers counted prompt-only and have no denominator. Never reinterpret
    # those numbers as a current measurement or reuse an earlier known ratio.
    if current["last_context_budget_tokens"] is None:
        current["last_context_tokens"] = None
    return {
        "revision": previous.get("revision", 0) + 1,
        **current,
        "calls": [*previous.get("calls", []), event][-100:], "totals": totals,
        "unreported_usage_count": previous.get("unreported_usage_count", 0) + int(missing),
    }


def pending_context_usage(previous):
    """Invalidate only current measurement after context changes; retain billing totals."""
    return {**previous, "revision": previous.get("revision", 0) + 1,
            "last_context_usage_missing": False,
            **dict.fromkeys(("last_context_tokens", "last_context_budget_tokens",
                             "last_context_prompt_tokens", "last_context_completion_tokens",
                             "last_context_cached_tokens", "last_context_revision",
                             "last_context_target_revision"))}
