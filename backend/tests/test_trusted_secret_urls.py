import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import regex

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor, secret_client  # noqa: E402


BASE = "https://www.natfrp.com"
OPENID = "https://openid.13a.com"
LOGIN_URL = f"{OPENID}/oauth2/authorize?client_id=natfrp&redirect_uri=https%3A%2F%2Fwww.natfrp.com"
LOGIN_PATTERN = (
    r"https://openid\.13a\.com/oauth2/authorize\?client_id=natfrp"
    r"&redirect_uri=https%3A%2F%2Fwww\.natfrp\.com"
)
FEATURES = ["auth.form_login", "challenge.2fa"]
FAKE_SECRET = {"username": "test-user", "password": "test-password", "code": "123456"}


@pytest.mark.parametrize("current,base", [
    (f"{BASE}/login?next=/account#form", BASE),
    ("https://WWW.NATFRP.COM:443/login", f"{BASE}/account"),
    ("http://www.natfrp.com:80/login", "http://www.natfrp.com"),
    ("https://www.natfrp.com:8443/login", "https://www.natfrp.com:8443"),
])
@pytest.mark.parametrize("trusted_urls", [None, []])
def test_same_origin_remains_allowed(current, base, trusted_urls):
    secret_client.require_site_origin(current, base, trusted_urls)


def test_legacy_two_argument_same_origin_call():
    secret_client.require_site_origin(f"{BASE}/login", BASE)


@pytest.mark.parametrize("pattern", [regex.escape(LOGIN_URL), LOGIN_PATTERN])
def test_natfrp_to_openid_exact_url_and_query_pattern_allowed(pattern):
    secret_client.require_site_origin(LOGIN_URL, BASE, [pattern])


def test_regex_path_and_query_variation_allowed():
    pattern = r"https://openid\.13a\.com/(?:login|oauth2/authorize)\?client_id=natfrp&state=[a-z0-9]+"
    for path in ("login", "oauth2/authorize"):
        secret_client.require_site_origin(
            f"{OPENID}/{path}?client_id=natfrp&state=abc123", BASE, [pattern],
        )


@pytest.mark.parametrize("pattern", [
    r"https://openid\.13a\.com", r"https://openid\.13a\.com/oauth2/authorize",
    r"openid\.13a\.com", r"client_id=natfrp",
])
def test_regex_prefix_or_substring_is_not_enough(pattern):
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(LOGIN_URL, BASE, [pattern])


@pytest.mark.parametrize("current", [
    f"{OPENID}.evil.test/oauth2/authorize?client_id=natfrp",
    LOGIN_URL.replace("openid.13a.com", "evilopenid.13a.com"),
    LOGIN_URL.replace("openid.13a.com", "openidX13aXcom"),
    LOGIN_URL.replace("openid.13a.com", "evil.test"),
    LOGIN_URL.replace("/oauth2/authorize", "/oauth2/authorize/extra"),
    f"{LOGIN_URL}&next=https://evil.test",
    f"{LOGIN_URL}&client_id=evil",
    f"{LOGIN_URL}#https://evil.test",
    f"https://evil.test/?next={LOGIN_URL}",
    f"https://evil.test/{LOGIN_URL}",
    LOGIN_URL.replace("https://", "http://", 1),
    LOGIN_URL.replace("openid.13a.com", "openid.13a.com:444"),
    "http://www.natfrp.com/login",
    "https://www.natfrp.com:444/login",
])
def test_untrusted_domains_paths_queries_and_scheme_changes_denied(current):
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(current, BASE, [LOGIN_PATTERN])


@pytest.mark.parametrize("current", [
    None, 42, b"https://openid.13a.com/login", "", "/login", "//openid.13a.com/login",
    "https:///login", "https://", "https://[broken/login",
    f"https://user@openid.13a.com/login",
    "https://user:password@openid.13a.com/login",
    "https://@openid.13a.com/login",
    "https://openid.13a.com@evil.test/login",
    "https://openid.13a.com\\@evil.test/login",
    "https://openid.13a.com/login\\next",
    "https://openid.13a.com:invalid/login",
    "https://openid.13a.com:-1/login",
    "https://openid.13a.com:65536/login",
    "ftp://openid.13a.com/login", "file:///tmp/login", "about:blank",
    "javascript:alert(1)", "data:text/html,login",
    f"{OPENID}/" + "a" * 8192,
    *[f"{OPENID}/login{char}next" for char in ("\x00", "\x01", "\t", "\n", "\r", " ", "\x1f", "\x7f")],
    f" {OPENID}/login",
])
def test_malformed_or_non_http_urls_denied_even_with_broad_allowlist(current):
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(current, BASE, [r"(?s).*"])


@pytest.mark.parametrize("base", [
    None, "about:blank", "https://user@www.natfrp.com", "https://www.natfrp.com:65536",
    "https://www.natfrp.com/\n", "https://www.natfrp.com\\login",
])
def test_invalid_base_cannot_be_overridden_by_allowlist(base):
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(LOGIN_URL, base, [r".*"])


@pytest.mark.parametrize("trusted_urls", [
    None, [], LOGIN_PATTERN, {}, (LOGIN_PATTERN,), 1, True,
    [None], [1], [True], [{}], [[]], [b".*"], [""], [" \t\n"],
    ["["], ["("], [r"\q"], ["(?V1).*"], ["(?V1)(?V0).*"],
    ["a" * 1025], [r".*"] * 21,
    ["[", r".*"], [None, r".*"], ["a" * 1025, r".*"],
])
def test_invalid_allowlist_fails_closed(trusted_urls):
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(LOGIN_URL, BASE, trusted_urls)


@pytest.mark.parametrize("invalid", [None, "[", "", "a" * 1025], ids=[
    "wrong-type", "invalid-regex", "empty-pattern", "overlong-pattern",
])
def test_matching_entry_does_not_hide_later_invalid_entry(invalid):
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(LOGIN_URL, BASE, [r".*", invalid])


def test_allowlist_count_pattern_length_and_url_length_boundaries():
    # Regex comments pad the pattern without changing its match.
    prefix = r"https://openid\.13a\.com/.*"
    pattern = prefix + "(?#" + "a" * (1024 - len(prefix) - 4) + ")"
    current = f"{OPENID}/" + "a" * (8192 - len(OPENID) - 1)
    assert len(pattern) == 1024
    assert len(current) == 8192
    secret_client.require_site_origin(current, BASE, ["never"] * 19 + [pattern])


def test_regex_fullmatch_uses_version0_and_bounded_timeout(monkeypatch):
    original = regex.fullmatch
    calls = []

    def fullmatch(pattern, current, *, flags, timeout):
        calls.append((pattern, current, flags, timeout))
        assert flags == regex.VERSION0
        assert timeout == 0.025
        return original(pattern, current, flags=flags, timeout=timeout)

    monkeypatch.setattr(secret_client.regex, "fullmatch", fullmatch)
    secret_client.require_site_origin(LOGIN_URL, BASE, [LOGIN_PATTERN])
    assert calls == [(LOGIN_PATTERN, LOGIN_URL, regex.VERSION0, 0.025)]


def test_pathological_regex_timeout_fails_closed_without_fallback(monkeypatch):
    pattern = r"https://openid\.13a\.com/(a+)+$"
    current = f"{OPENID}/" + "a" * 7000 + "!"
    calls = []

    def timeout_fullmatch(actual_pattern, actual_current, *, flags, timeout):
        calls.append(actual_pattern)
        assert actual_pattern == pattern
        assert actual_current == current
        assert flags == regex.VERSION0
        assert timeout == 0.025
        raise TimeoutError("regex timed out")

    monkeypatch.setattr(secret_client.regex, "fullmatch", timeout_fullmatch)
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        secret_client.require_site_origin(current, BASE, [pattern, r".*"])
    assert calls == [pattern]


@pytest.fixture
def sensitive_step(monkeypatch):
    page = SimpleNamespace(url=LOGIN_URL)
    locators = {}
    for name in ("username", "password", "code", "submit", "success"):
        locators[name] = SimpleNamespace(
            fill=AsyncMock(), click=AsyncMock(),
            input_value=AsyncMock(return_value=FAKE_SECRET.get(name, "")),
        )

    async def locate(page_arg, selectors, **kwargs):
        assert page_arg is page
        return locators[selectors[0]]

    resolver = AsyncMock(return_value=dict(FAKE_SECRET))
    finder = AsyncMock(side_effect=locate)
    otp = AsyncMock(return_value=(FAKE_SECRET["code"], "success", "test OTP"))
    monkeypatch.setattr(secret_client, "resolve_secret", resolver)
    monkeypatch.setattr(executor, "_find_locator", finder)
    monkeypatch.setattr(executor, "_fetch_otp", otp)
    payload = {"base_url": BASE, "trusted_urls": [LOGIN_PATTERN]}

    def run(feature, *, legacy=False):
        config = {
            "username_selector": ["username"], "password_selector": ["password"],
            "code_selector": ["code"], "submit_selector": ["submit"],
            "success_selector": ["success"],
        }
        return asyncio.run(executor._run_step(
            page, {"step_key": "test-sensitive", "feature_type": feature, "config": config},
            dict(FAKE_SECRET) if legacy else None, None, None, None, None, "general",
            secret_payload=None if legacy else payload,
        ))

    return SimpleNamespace(
        page=page, locators=locators, resolver=resolver, finder=finder, otp=otp,
        payload=payload, run=run,
    )


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize("pattern", [regex.escape(LOGIN_URL), LOGIN_PATTERN])
def test_sensitive_step_allows_trusted_cross_origin(sensitive_step, feature, pattern):
    h = sensitive_step
    h.payload["trusted_urls"] = [pattern]
    assert h.run(feature)[0] == "success"
    h.resolver.assert_awaited_once()
    assert h.resolver.call_args.args[0] is h.payload
    assert h.resolver.call_args.args[1]["feature_type"] == feature
    fields = ("username", "password") if feature == "auth.form_login" else ("code",)
    for name in fields:
        h.locators[name].fill.assert_awaited_once_with(
            FAKE_SECRET[name], timeout=executor.PAGE_TIMEOUT_MS,
        )
    h.locators["submit"].click.assert_awaited_once()
    h.otp.assert_not_awaited()


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize("allowlist", ["missing", None, []])
def test_sensitive_step_denies_untrusted_origin_before_secret_read(sensitive_step, feature, allowlist):
    h = sensitive_step
    if allowlist == "missing":
        h.payload.pop("trusted_urls")
    else:
        h.payload["trusted_urls"] = allowlist
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        h.run(feature)
    h.resolver.assert_not_awaited()
    h.finder.assert_not_awaited()
    h.otp.assert_not_awaited()


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize("redirect_at", ["secret_resolution", "locator_lookup"])
def test_redirect_before_fill_is_rechecked(sensitive_step, feature, redirect_at):
    h = sensitive_step
    if redirect_at == "secret_resolution":
        async def resolve(*args):
            h.page.url = "https://evil.test/login"
            return dict(FAKE_SECRET)
        h.resolver.side_effect = resolve
    else:
        original = h.finder.side_effect

        async def locate(*args, **kwargs):
            locator = await original(*args, **kwargs)
            h.page.url = "https://evil.test/login"
            return locator
        h.finder.side_effect = locate
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        h.run(feature)
    h.resolver.assert_awaited_once()
    for locator in h.locators.values():
        locator.fill.assert_not_awaited()
        locator.click.assert_not_awaited()


def test_username_fill_redirect_denies_password(sensitive_step):
    h = sensitive_step

    async def redirect(*args, **kwargs):
        h.page.url = "https://evil.test/login"

    h.locators["username"].fill.side_effect = redirect
    with pytest.raises(RuntimeError, match="Sensitive login fill failed") as error:
        h.run("auth.form_login")
    h.locators["username"].fill.assert_awaited_once()
    h.locators["password"].fill.assert_not_awaited()
    h.locators["submit"].click.assert_not_awaited()
    assert FAKE_SECRET["password"] not in str(error.value)


@pytest.mark.parametrize("feature", FEATURES)
def test_redirect_after_sensitive_fill_denies_submit(sensitive_step, feature):
    h = sensitive_step
    field = "password" if feature == "auth.form_login" else "code"

    async def redirect(*args, **kwargs):
        h.page.url = "https://evil.test/login"

    h.locators[field].fill.side_effect = redirect
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        h.run(feature)
    h.locators[field].fill.assert_awaited_once()
    h.locators["submit"].click.assert_not_awaited()


def test_totp_redirect_during_submit_lookup_denies_click(sensitive_step):
    h = sensitive_step
    original = h.finder.side_effect

    async def locate(page, selectors, **kwargs):
        locator = await original(page, selectors, **kwargs)
        if selectors == ["submit"]:
            h.page.url = "https://evil.test/login"
        return locator

    h.finder.side_effect = locate
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        h.run("challenge.2fa")
    h.locators["code"].fill.assert_awaited_once()
    h.locators["submit"].click.assert_not_awaited()


@pytest.mark.parametrize("feature", FEATURES)
def test_sensitive_step_same_origin_needs_no_allowlist(sensitive_step, feature):
    h = sensitive_step
    h.page.url = f"{BASE}/login"
    h.payload.pop("trusted_urls")
    assert h.run(feature)[0] == "success"
    h.resolver.assert_awaited_once()
    h.locators["submit"].click.assert_awaited_once()


@pytest.mark.parametrize("feature", FEATURES)
def test_legacy_sensitive_steps_without_secret_payload_preserved(sensitive_step, feature):
    h = sensitive_step
    assert h.run(feature, legacy=True)[0] == "success"
    h.resolver.assert_not_awaited()
    h.locators["submit"].click.assert_awaited_once()
    if feature == "challenge.2fa":
        h.otp.assert_awaited_once()
    else:
        h.otp.assert_not_awaited()
