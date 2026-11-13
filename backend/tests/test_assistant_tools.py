from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest

from app.services.assistant_prompt import tool_definitions
from app.services.assistant_tools import AssistantTools
from tests.test_assistant_api import (  # noqa: F401
    assistant_client, create, provider, target,
)


@pytest.mark.parametrize("field,length,accepted", [
    ("url", 2048, True), ("url", 2049, True),
    ("url", 4096, True), ("url", 4097, False),
    ("path", 2048, True), ("path", 2049, False),
])
def test_navigation_length_boundaries_match_schema(assistant_client, field, length, accepted):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    confirmed = target(assistant_client, identifier)["target"]
    manager.browser_tool = AsyncMock(return_value={"ok": True})
    tools = AssistantTools(manager, "admin", identifier, confirmed)
    prefix = "https://example.com/" if field == "url" else "/"
    value = prefix + "a" * (length - len(prefix))
    schema = next(item["function"]["parameters"] for item in tool_definitions()
                  if item["function"]["name"] == "browser_navigate")
    assert Draft202012Validator(schema).is_valid({field: value, "title": "Navigate"}) is accepted
    result = asyncio.run(tools.dispatch("browser_navigate", {field: value}))
    if accepted:
        assert result == {"ok": True}
        expected = value if field == "url" else "https://example.com" + value
        manager.browser_tool.assert_awaited_once_with(
            "admin", identifier, confirmed, "browser_navigate", expected,
        )
    else:
        assert result == {"ok": False, "code": "invalid_navigation"}
        manager.browser_tool.assert_not_awaited()
