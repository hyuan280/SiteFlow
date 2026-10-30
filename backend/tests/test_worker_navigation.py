from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.executor import (  # noqa: E402
    _captcha_format_error,
    _cf_page_state,
    _cf_progress_reason,
    _detect_page_error,
    _evaluate_signal_condition,
    _selector_exists,
    _export_site_cookies,
    _fetch_bitwarden_credentials,
    _goto_if_needed,
    _NavigationPageError,
    _run_flow_jump,
    _run_step,
    _same_navigation_url,
    _wait_unit3d_success,
    execute_job,
)


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.calls: list[str] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.calls.append(url)
        self.url = url


class FlakyPage(FakePage):
    def __init__(self, url: str, failures: int) -> None:
        super().__init__(url)
        self.failures = failures
        self.attempts = 0

    async def goto(self, url: str, **kwargs) -> None:
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
        await super().goto(url, **kwargs)


def test_navigation_url_matching() -> None:
    assert _same_navigation_url("https://zmpt.cc/", "https://zmpt.cc")
    assert _same_navigation_url(
        "https://zmpt.cc/index.php?b=2&a=1",
        "https://zmpt.cc/index.php?a=1&b=2",
    )
    assert _same_navigation_url("https://zmpt.cc:443/sign", "https://zmpt.cc/sign")
    assert not _same_navigation_url("https://zmpt.cc/sign", "https://zmpt.cc/")
    assert not _same_navigation_url("http://zmpt.cc/sign", "https://zmpt.cc/sign")


def test_goto_skips_same_url() -> None:
    async def run() -> None:
        page = FakePage("https://zmpt.cc/")
        messages: list[tuple[str, str]] = []
        navigated = await _goto_if_needed(
            page,
            "https://zmpt.cc",
            "domcontentloaded",
            lambda level, message: messages.append((level, message)),
            "页面",
        )
        assert navigated is False
        assert page.calls == []
        assert any("跳过" in message for _, message in messages)

        navigated = await _goto_if_needed(
            page,
            "https://zmpt.cc/sign",
            "domcontentloaded",
            lambda level, message: messages.append((level, message)),
            "页面",
        )
        assert navigated is True
        assert page.calls == ["https://zmpt.cc/sign"]

    asyncio.run(run())


def test_export_cookies_uses_site_and_seed_domains() -> None:
    class Context:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def cookies(self, urls):  # type: ignore[no-untyped-def]
            self.urls = list(urls)
            return [{"name": "uid", "value": "1"}]

    async def run() -> None:
        context = Context()
        cookies = await _export_site_cookies(
            context,
            "https://pt.example.com/attendance.php",
            [
                {"name": "sso", "value": "x", "domain": ".sso.example.com"},
                {"name": "direct", "value": "y", "url": "https://other.example.com/"},
            ],
            lambda _level, _text: None,
        )
        assert cookies == [{"name": "uid", "value": "1"}]
        assert "https://pt.example.com/attendance.php" in context.urls
        assert "https://sso.example.com/" in context.urls
        assert "https://other.example.com/" in context.urls

    asyncio.run(run())


def test_goto_retries_transient_network_errors() -> None:
    async def run() -> None:
        page = FlakyPage("https://zmpt.cc/", 2)
        messages: list[tuple[str, str]] = []
        navigated = await _goto_if_needed(
            page,
            "https://zmpt.cc/sign",
            "domcontentloaded",
            lambda level, message: messages.append((level, message)),
            "页面",
            retry_count=2,
            retry_interval_seconds=0,
        )
        assert navigated is True
        assert page.attempts == 3
        assert page.calls == ["https://zmpt.cc/sign"]
        assert sum(1 for level, _ in messages if level == "WARN") == 2

    asyncio.run(run())


def test_captcha_format_validation() -> None:
    assert _captcha_format_error("a1B2", "alnum", 4) is None
    assert _captcha_format_error("a1B2", "digits", 4) == "字符集不符合 digits"
    assert _captcha_format_error("1234", "digits", 4) is None
    assert _captcha_format_error("abcd", "lower", 4) is None
    assert _captcha_format_error("ABCD", "upper", 4) is None
    assert _captcha_format_error("ab12", "letters", 4) == "字符集不符合 letters"
    assert _captcha_format_error("ab12", "digits_lower", 4) is None
    assert _captcha_format_error("AB12", "digits_upper", 4) is None
    assert _captcha_format_error("abc", "lower", 4) == "长度 3 不符合期望 4"
    assert _captcha_format_error("中文", "any", 0) is None
    assert _captcha_format_error("", "alnum", 0) == "识别结果为空"


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count
        self.first = self

    async def count(self) -> int:
        return self._count


class _FakeFrame:
    def __init__(self, selectors: set[str], text: str = "") -> None:
        self.selectors = selectors
        self.text = text

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(1 if selector in self.selectors else 0)

    async def inner_text(self, _selector: str) -> str:
        return self.text


class _FakePage:
    def __init__(self, text: str = "", selectors: set[str] | None = None) -> None:
        self.text = text
        self.frames = [_FakeFrame(selectors or set(), text)]

    async def title(self) -> str:
        return "Title"

    async def inner_text(self, _selector: str) -> str:
        return self.text


def test_flow_jump_matches_text_selector_and_restart() -> None:
    matched = asyncio.run(
        _run_flow_jump(
            _FakePage("页面出现 验证失败", set()),
            {
                "texts": ["验证失败"],
                "selectors": [],
                "action": "jump",
                "target_step_key": "login",
            },
        )
    )
    assert matched[0] == "success"
    assert matched[2]["matched"] is True
    assert matched[2]["target_step_key"] == "login"

    selector = asyncio.run(
        _run_flow_jump(
            _FakePage("", {".ant-modal button:has(span:text-is('確認'))"}),
            {
                "selectors": [".ant-modal button:has(span:text-is('確認'))"],
                "action": "restart",
            },
        )
    )
    assert selector[0] == "restart"
    assert selector[2]["action"] == "restart"

    missed = asyncio.run(
        _run_flow_jump(
            _FakePage("normal"),
            {"texts": ["验证失败"], "action": "jump", "target_step_key": "login"},
        )
    )
    assert missed[0] == "success"
    assert missed[2]["matched"] is False


class _JumpPage(_FakePage):
    def on(self, *_args):
        pass

    def remove_listener(self, *_args):
        pass

    def __init__(self) -> None:
        super().__init__("hit", set())
        self.url = "https://example.com"

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _JumpContext:
    def __init__(self, page: _JumpPage) -> None:
        self.page = page

    async def new_page(self) -> _JumpPage:
        return self.page


class _JumpLane:
    def __init__(self, page: _JumpPage) -> None:
        self.context = _JumpContext(page)
        self.execution_semaphore = asyncio.Semaphore(1)
        self.task_pages: dict[str, object] = {}
        self.display = ":120"
        self.page = None


class _JumpManager:
    def __init__(self, lane: _JumpLane) -> None:
        self.lane = lane
        self.released = False

    async def lease_lane(self, _lane_key: str):
        return "lease-1", {"display": ":120", "browser": "fake", "profile": "/tmp"}

    def get(self, _lane_key: str) -> _JumpLane:
        return self.lane

    async def release_lane(self, _lane_key: str, _lease_id: str) -> None:
        self.released = True


def test_signal_evaluation_timeout_returns_unknown(monkeypatch) -> None:
    import worker.executor as executor_module

    async def slow_signal(*_args, **_kwargs):
        await asyncio.sleep(5)
        return "true", "late"

    monkeypatch.setattr(executor_module, "_evaluate_signal", slow_signal)
    monkeypatch.setattr(executor_module, "SIGNAL_EVAL_TIMEOUT_SECONDS", 0.05)
    result = asyncio.run(
        _evaluate_signal_condition(
            object(),
            "slow",
            True,
            {
                "slow": {
                    "signal_key": "slow",
                    "enabled": True,
                    "label": "慢信号",
                    "include_texts": ["x"],
                }
            },
        )
    )
    assert result == "unknown"


def test_selector_exists_has_per_frame_timeout(monkeypatch) -> None:
    import worker.executor as executor_module

    class SlowLocator:
        @property
        def first(self):
            return self

        async def count(self):
            await asyncio.sleep(5)
            return 1

    class SlowFrame:
        def locator(self, _selector):
            return SlowLocator()

    class SlowPage:
        frames = [SlowFrame()]

    monkeypatch.setattr(executor_module, "FRAME_OP_TIMEOUT_SECONDS", 0.05)
    assert asyncio.run(_selector_exists(SlowPage(), "#x")) is False


def test_fetch_bitwarden_credentials_defers_lookup(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"username": "vault-user", "password": "vault-pass"}

        @property
        def text(self):
            return ""

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("worker.executor.httpx.AsyncClient", FakeClient)
    messages: list[tuple[str, str]] = []
    credentials = asyncio.run(
        _fetch_bitwarden_credentials(
            {
                "type": "bitwarden",
                "url": "http://bitwarden:8090",
                "token": "test-token",
                "mode": "domain",
                "host": "pt.example.com",
                "timeout": 12,
            },
            lambda level, message: messages.append((level, message)),
        )
    )
    assert credentials == {"username": "vault-user", "password": "vault-pass"}
    assert captured["url"] == "http://bitwarden:8090/lookup"
    assert captured["json"] == {"mode": "domain", "host": "pt.example.com"}
    assert captured["headers"] == {"Authorization": "Bearer test-token"}
    assert any("获取完成" in message for _, message in messages)


def test_flow_jump_loop_limit() -> None:
    page = _JumpPage()
    lane = _JumpLane(page)
    manager = _JumpManager(lane)
    result = asyncio.run(
        execute_job(
            manager,
            {
                "job_id": 1,
                "run_id": 1,
                "lane_key": "general",
                "site_name": "jump-site",
                "steps": [
                    {
                        "step_key": "jump",
                        "feature_type": "flow.jump",
                        "enabled": True,
                        "config": {
                            "texts": ["hit"],
                            "selectors": [],
                            "action": "jump",
                            "target_step_key": "jump",
                            "max_jumps": 2,
                        },
                    }
                ],
            },
        )
    )
    assert result["status"] == "fail"
    assert "上限" in result["message"]
    assert manager.released is True


def test_cf_progress_reason_resets_for_new_challenge_only() -> None:
    initial = "https://www.hitpt.com/login.php?__cf_chl_rt_tk=abc"
    assert (
        _cf_progress_reason(initial, initial, 10, 0, clicked=True)
        == "Turnstile token 已更新"
    )
    assert (
        _cf_progress_reason(initial, "https://www.hitpt.com/login.php", 0, 0, clicked=True)
        == "页面已跳转"
    )
    assert _cf_progress_reason(initial, initial, 10, 10, clicked=True) is None
    assert _cf_progress_reason(initial, initial, 0, 0, clicked=False) is None
    assert (
        _cf_progress_reason(
            initial,
            "https://www.hitpt.com/login.php?__cf_chl_rt_tk=next",
            0,
            0,
            clicked=True,
        )
        is None
    )


class _CfStatePage:
    def __init__(
        self,
        *,
        marker: bool = False,
        token_length: int = 0,
        url: str = "https://kamept.com/",
    ) -> None:
        self.marker = marker
        self.token_length = token_length
        self.url = url

    async def evaluate(self, script: str):
        if "CF page body unavailable" in script:
            return "欢迎回来"
        if "cf-turnstile-response" in script:
            return self.token_length
        return self.marker

    async def title(self) -> str:
        return "KamePT"

    async def inner_text(self, _selector: str) -> str:
        return "欢迎回来"

    @property
    def frames(self):
        return []


def test_cf_state_ignores_hidden_response_input_and_frames() -> None:
    async def run() -> None:
        state = await _cf_page_state(_CfStatePage(marker=False, token_length=0))
        assert state["marker"] is False
        assert state["token_length"] == 0
        assert state["challenge_text"] is False
        assert state["url_challenge"] is False

        challenge = await _cf_page_state(
            _CfStatePage(
                marker=True,
                token_length=0,
                url="https://kamept.com/login.php?__cf_chl_rt_tk=abc",
            )
        )
        assert challenge["marker"] is True
        assert challenge["url_challenge"] is True

    asyncio.run(run())


def test_fallback_interaction_status_propagates(monkeypatch, tmp_path) -> None:
    import worker.executor as executor_module

    monkeypatch.setattr(executor_module, "ARTIFACT_ROOT", tmp_path)

    async def fake_capture(self, _page, _step_key, _label, reason="失败"):
        self.screenshots.append({"kind": "screenshot"})

    async def fake_run_step(*_args, **_kwargs):
        return "fallback_interaction", "Playwright 点击未生效，转交互车道重试", {}

    monkeypatch.setattr(executor_module.JobArtifacts, "capture_failure", fake_capture)
    monkeypatch.setattr(executor_module, "_run_step", fake_run_step)
    manager = _JumpManager(_JumpLane(_JumpPage()))
    result = asyncio.run(
        execute_job(
            manager,
            {
                "job_id": 1,
                "run_id": 1,
                "lane_key": "general",
                "site_name": "fallback-site",
                "steps": [
                    {
                        "step_key": "cf",
                        "feature_type": "challenge.turnstile",
                        "enabled": True,
                        "condition": "always",
                        "on_error": "stop",
                        "config": {},
                    }
                ],
            },
        )
    )
    assert result["status"] == "fallback_interaction"
    assert "转交互车道" in result["message"]


def test_continue_policy_does_not_fail_job(monkeypatch, tmp_path) -> None:
    import worker.executor as executor_module

    monkeypatch.setattr(executor_module, "ARTIFACT_ROOT", tmp_path)

    async def fake_run_step(_page, step, *_args, **_kwargs):
        if step["step_key"] == "optional":
            return "fail", "可选步骤失败", {}
        return "success", "签到完成", {}

    monkeypatch.setattr(executor_module, "_run_step", fake_run_step)
    manager = _JumpManager(_JumpLane(_JumpPage()))
    result = asyncio.run(
        execute_job(
            manager,
            {
                "job_id": 1,
                "run_id": 1,
                "lane_key": "general",
                "site_name": "continue-site",
                "steps": [
                    {
                        "step_key": "optional",
                        "feature_type": "action.click",
                        "enabled": True,
                        "condition": "always",
                        "on_error": "continue",
                        "config": {},
                    },
                    {
                        "step_key": "sign",
                        "feature_type": "sign.button",
                        "enabled": True,
                        "condition": "always",
                        "config": {},
                    },
                ],
            },
        )
    )
    assert result["status"] == "success"
    assert [step["status"] for step in result["steps"]] == ["fail", "success"]


def test_continue_policy_with_only_failed_step_keeps_job_success(
    monkeypatch, tmp_path
) -> None:
    import worker.executor as executor_module

    monkeypatch.setattr(executor_module, "ARTIFACT_ROOT", tmp_path)

    async def fake_run_step(*_args, **_kwargs):
        return "fail", "可选步骤失败", {}

    monkeypatch.setattr(executor_module, "_run_step", fake_run_step)
    manager = _JumpManager(_JumpLane(_JumpPage()))
    result = asyncio.run(
        execute_job(
            manager,
            {
                "job_id": 1,
                "run_id": 1,
                "lane_key": "general",
                "site_name": "continue-only-site",
                "steps": [
                    {
                        "step_key": "optional",
                        "feature_type": "action.click",
                        "enabled": True,
                        "condition": "always",
                        "on_error": "continue",
                        "config": {},
                    }
                ],
            },
        )
    )
    assert result["status"] == "success"
    assert result["steps"][0]["status"] == "fail"
    assert result["message"] == "执行完成"


def test_stop_policy_still_fails_job(monkeypatch, tmp_path) -> None:
    import worker.executor as executor_module

    monkeypatch.setattr(executor_module, "ARTIFACT_ROOT", tmp_path)

    async def fake_run_step(*_args, **_kwargs):
        return "fail", "步骤失败", {}

    monkeypatch.setattr(executor_module, "_run_step", fake_run_step)
    manager = _JumpManager(_JumpLane(_JumpPage()))
    result = asyncio.run(
        execute_job(
            manager,
            {
                "job_id": 1,
                "run_id": 1,
                "lane_key": "general",
                "site_name": "stop-site",
                "steps": [
                    {
                        "step_key": "required",
                        "feature_type": "action.click",
                        "enabled": True,
                        "condition": "always",
                        "config": {},
                    }
                ],
            },
        )
    )
    assert result["status"] == "fail"


class _ErrorPage:
    def __init__(self, title: str, text: str) -> None:
        self.title_text = title
        self.text = text
        self.url = "about:blank"

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def title(self) -> str:
        return self.title_text

    async def inner_text(self, _selector: str) -> str:
        return self.text


def test_navigation_detects_http_error_and_maintenance_pages() -> None:
    assert asyncio.run(
        _detect_page_error(_ErrorPage("502 Bad Gateway", "nginx"))
    ) == "bad gateway"
    assert asyncio.run(
        _detect_page_error(
            _ErrorPage("百川PT", "网站正在维护，请稍后再试")
        )
    ) == "网站正在维护"
    assert asyncio.run(
        _detect_page_error(_ErrorPage("站点公告", "当前站点关闭维护"))
    ) == "关闭维护"
    assert asyncio.run(
        _detect_page_error(
            _ErrorPage("KamePT", "The website has been stopped")
        )
    ) == "the website has been stopped"
    assert asyncio.run(
        _detect_page_error(
            _ErrorPage("example.com | 522", "Connection timed out")
        )
    )
    assert asyncio.run(
        _detect_page_error(_ErrorPage("Just a moment...", "Checking your browser"))
    ) is None
    assert asyncio.run(
        _detect_page_error(_ErrorPage("正常站点", "欢迎回来，开始签到"))
    ) is None


def test_flow_open_returns_failure_for_error_page() -> None:
    page = _ErrorPage("502 Bad Gateway", "bad gateway")
    status, message, data = asyncio.run(
        _run_step(
            page,
            {
                "feature_type": "flow.open",
                "config": {"url": "https://pt.example.com", "wait_until": "domcontentloaded"},
            },
            None,
            None,
            None,
            None,
            None,
            "general",
        )
    )
    assert status == "fail"
    assert "bad gateway" in message
    assert data["error_page"] == "bad gateway"


class _CachedErrorPage(_ErrorPage):
    def __init__(self, outcomes, *, same_url=False):
        super().__init__("The website has been stopped", "")
        self.url = "https://pt.example.com/" if same_url else "about:blank"
        self.outcomes = iter(outcomes)
        self.visits = []
        self.commands = []
        self.detached = False
        self.context = self

    async def goto(self, url, **_kwargs):
        self.visits.append(url)
        self.url = url
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        self.title_text = outcome

    async def new_cdp_session(self, page):
        assert page is self
        return self

    async def send(self, method, params=None):
        self.commands.append((method, params))

    async def detach(self):
        self.detached = True


@pytest.mark.parametrize("same_url", [False, True])
def test_open_error_page_retries_same_target_without_cache(same_url):
    async def run():
        page = _CachedErrorPage(
            ["Welcome"] if same_url else ["The website has been stopped", "Welcome"],
            same_url=same_url,
        )
        assert await _goto_if_needed(
            page, "https://pt.example.com/", "domcontentloaded", lambda *_: None,
            "页面", retry_count=1, retry_interval_seconds=0, check_error_page=True,
        )
        assert len(page.visits) == (1 if same_url else 2)
        assert set(page.visits) == {"https://pt.example.com/"}
        assert page.commands == [
            ("Network.enable", None),
            ("Network.setCacheDisabled", {"cacheDisabled": True}),
            ("Network.setCacheDisabled", {"cacheDisabled": False}),
        ]
        assert page.detached

    asyncio.run(run())


@pytest.mark.parametrize("retry_count", [0, 2])
def test_open_error_page_exhausts_retry_budget_and_preserves_failure_data(monkeypatch, retry_count):
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr("worker.executor.asyncio.sleep", no_wait)
    page = _CachedErrorPage(["The website has been stopped"] * (retry_count + 1))
    status, message, data = asyncio.run(_run_step(
        page, {"feature_type": "flow.open", "config": {
            "url": "https://pt.example.com/", "retry_count": retry_count,
        }}, None, None, None, None, None, "general",
    ))
    assert status == "fail"
    assert "the website has been stopped" in message
    assert data == {"url": page.url, "error_page": "the website has been stopped"}
    assert len(page.visits) == retry_count + 1
    assert page.detached == bool(retry_count)
    if not retry_count:
        assert page.commands == []


def test_open_network_and_page_errors_share_one_retry_budget():
    async def run():
        page = _CachedErrorPage([
            RuntimeError("Page.goto: net::ERR_CONNECTION_RESET"),
            "The website has been stopped",
            "The website has been stopped",
        ])
        with pytest.raises(_NavigationPageError):
            await _goto_if_needed(
                page, "https://pt.example.com/", "domcontentloaded", lambda *_: None,
                "页面", retry_count=2, retry_interval_seconds=0, check_error_page=True,
            )
        assert len(page.visits) == 3
        assert page.detached

    asyncio.run(run())


def test_open_cache_setting_restored_on_cancellation():
    async def run():
        page = _CachedErrorPage(["The website has been stopped", asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await _goto_if_needed(
                page, "https://pt.example.com/", "domcontentloaded", lambda *_: None,
                "页面", retry_count=1, retry_interval_seconds=0, check_error_page=True,
            )
        assert page.commands[-1] == ("Network.setCacheDisabled", {"cacheDisabled": False})
        assert page.detached

    asyncio.run(run())


def test_non_success_step_statuses_capture_screenshot(monkeypatch, tmp_path) -> None:
    import worker.executor as executor_module

    monkeypatch.setattr(executor_module, "ARTIFACT_ROOT", tmp_path)
    captured: list[tuple[str | None, str]] = []

    async def fake_capture(self, _page, step_key, _label, reason="失败"):
        captured.append((step_key, reason))
        self.screenshots.append({"kind": "screenshot"})

    monkeypatch.setattr(executor_module.JobArtifacts, "capture_failure", fake_capture)

    for status in ("unknown", "waiting_human", "fail", "error", "canceled"):
        async def fake_run_step(
            _page, _step, *_args, status=status, **_kwargs
        ):
            return status, f"{status} message", {}

        monkeypatch.setattr(executor_module, "_run_step", fake_run_step)
        captured.clear()
        manager = _JumpManager(_JumpLane(_JumpPage()))
        asyncio.run(
            execute_job(
                manager,
                {
                    "job_id": 1,
                    "run_id": 1,
                    "lane_key": "general",
                    "site_name": "shoot-site",
                    "steps": [
                        {
                            "step_key": "probe",
                            "feature_type": "challenge.turnstile",
                            "enabled": True,
                            "condition": "always",
                            "config": {},
                        }
                    ],
                },
            )
        )
        expected = "失败" if status in {"fail", "error"} else "未成功"
        assert captured == [("probe", expected)]


def test_skipped_step_does_not_capture_screenshot(monkeypatch, tmp_path) -> None:
    import worker.executor as executor_module

    monkeypatch.setattr(executor_module, "ARTIFACT_ROOT", tmp_path)
    captured: list[str | None] = []

    async def fake_capture(self, _page, step_key, _label, reason="失败"):
        captured.append(step_key)
        self.screenshots.append({"kind": "screenshot"})

    async def fake_run_step(*_args, **_kwargs):
        return "skipped", "条件不满足", {}

    monkeypatch.setattr(executor_module.JobArtifacts, "capture_failure", fake_capture)
    monkeypatch.setattr(executor_module, "_run_step", fake_run_step)
    manager = _JumpManager(_JumpLane(_JumpPage()))
    result = asyncio.run(
        execute_job(
            manager,
            {
                "job_id": 1,
                "run_id": 1,
                "lane_key": "general",
                "site_name": "skip-site",
                "steps": [
                    {
                        "step_key": "probe",
                        "feature_type": "sign.button",
                        "enabled": True,
                        "condition": "always",
                        "config": {},
                    }
                ],
            },
        )
    )
    assert result["status"] == "success"
    assert captured == []


def test_wait_unit3d_success_conditions() -> None:
    ok, by = asyncio.run(
        _wait_unit3d_success(
            _FakePage("Successfully claimed"),
            {"success_signals": ["claimed"], "success_wait_seconds": 1},
            lambda _level, _message: None,
        )
    )
    assert ok is True
    assert by == "text"

    ok, by = asyncio.run(
        _wait_unit3d_success(
            _FakePage("", {".success"}),
            {"success_selector": [".success"], "success_wait_seconds": 1},
            lambda _level, _message: None,
        )
    )
    assert ok is True
    assert by == "selector:.success"

    ok, by = asyncio.run(
        _wait_unit3d_success(
            _FakePage("", set()),
            {"success_when_missing": ["button.claim"], "success_wait_seconds": 1},
            lambda _level, _message: None,
        )
    )
    assert ok is True
    assert by == "missing:button.claim"

    ok, _ = asyncio.run(
        _wait_unit3d_success(
            _FakePage("nothing"),
            {"success_signals": ["Claimed"], "success_wait_seconds": 1},
            lambda _level, _message: None,
        )
    )
    assert ok is False
