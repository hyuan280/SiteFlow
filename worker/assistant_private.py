"""In-process capabilities for single-step private execution, never model arguments."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from worker.assistant_step import secret_in_url
from worker.secret_client import require_site_origin


TARGET_VALID = r"""(el, {original, originalForm, origin, kind}) => {
    if (el.ownerDocument.location.origin !== origin ||
        el !== original || !el.isConnected || el.matches(':disabled') ||
        el.closest('[inert], [aria-disabled="true"]')) return false;
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (!rect.width || !rect.height || style.display === 'none' ||
        style.visibility !== 'visible') return false;
    if (el.target && el.target !== '_self') return false;
    if (el.hasAttribute('href')) {
        const u = new URL(el.href, location.href);
        if (!['http:', 'https:'].includes(u.protocol) || u.origin !== origin ||
            u.username || u.password) return false;
    }
    const form = el.form || null;
    if (form !== originalForm) return false;
    if (form) {
        if (form.target && form.target !== '_self') return false;
        const u = new URL(el.hasAttribute('formaction') ? el.formAction : form.action, location.href);
        if (!['http:', 'https:'].includes(u.protocol) || u.origin !== origin ||
            u.username || u.password || el.formTarget && el.formTarget !== '_self') return false;
    }
    if (kind === 'submit') return ['BUTTON', 'INPUT'].includes(el.tagName) &&
        ['submit', 'button', 'radio', 'checkbox'].includes(el.type);
    if (kind === 'code') return !!form && el.tagName === 'INPUT' && !el.readOnly &&
        ['text', 'tel', 'number'].includes(el.type);
    if (kind === 'fill') {
        if (!(el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' &&
            ['text','email','tel','number','search','url'].includes(el.type)) || el.readOnly) return false;
        const classification = [el.id, el.name, el.autocomplete, el.placeholder,
            el.getAttribute('aria-label')].join(' ');
        if (/password|passwd|secret|token|cookie|credential|authorization|otp|2fa|captcha|verification|one.time|\u9a8c\u8bc1\u7801|\u5bc6[\u7801\u9470\u94a5]/i.test(classification)) return false;
    }
    return true;
}"""

# Focus runs page handlers. Recheck the pinned nodes and forms in the same JS
# task that inserts the value, without another implicit Playwright focus step.
INSERT_VALUE = r"""(el, args) => {
    const valid = (""" + TARGET_VALID + r""");
    if (!valid(el, args) || el.ownerDocument.activeElement !== el) return false;
    for (const peer of args.peers) {
        if (!valid(peer.original, peer)) return false;
        if (['code', 'submit'].includes(args.kind) && ['code', 'submit'].includes(peer.kind) &&
            el.form !== peer.original.form) return false;
    }
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, args.value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return el.value === args.value;
}"""

CHALLENGE_CANDIDATES = r"""() => Array.from(document.querySelectorAll('iframe[src]')).filter(el => {
    const u = new URL(el.src, location.href), r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return u.protocol === 'https:' && u.hostname === 'challenges.cloudflare.com' &&
        (!u.port || u.port === '443') && !u.username && !u.password &&
        u.pathname.startsWith('/cdn-cgi/challenge-platform/') && el.isConnected &&
        r.width >= 20 && r.height >= 20 && r.width <= 650 && r.height <= 650 &&
        s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0;
})"""


@dataclass
class PrivateContext:
    page: object = field(repr=False)
    origin: str
    register_secret: Callable = field(repr=False)
    known: set = field(repr=False)
    provider_fingerprints: frozenset = field(default_factory=frozenset, repr=False)
    approved_origins: frozenset = field(default_factory=frozenset, repr=False)
    action_started: bool = False
    targets: list = field(default_factory=list, repr=False)
    iframe: object = field(default=None, repr=False)
    frame: object = field(default=None, repr=False)
    iframe_selector: str | None = None
    challenge: object = field(default=None, repr=False)
    challenge_frame: object = field(default=None, repr=False)
    challenge_document: object = field(default=None, repr=False)
    challenge_source: str | None = field(default=None, repr=False)

    def reject_provider_secret(self, value):
        if isinstance(value, str) and secret_in_url(value, self.provider_fingerprints):
            raise RuntimeError('Private provider output denied')

    async def validate_origin(self):
        require_site_origin(self.page.url, self.origin, [])
        if self.frame is not None:
            require_site_origin(self.frame.url, self.origin, [])
            locator = self.page.locator(self.iframe_selector)
            if (await locator.count() != 1 or not await self.iframe.is_visible()
                    or not await locator.evaluate('(el, original) => el === original', self.iframe)
                    or await self.iframe.content_frame() != self.frame):
                raise RuntimeError("Private frame denied")

    async def target(self, scope, selectors, kind):
        await self.validate_origin()
        for selector in selectors:
            locator = scope.locator(selector)
            count = await locator.count()
            if not count or not await locator.first.is_visible():
                continue
            if count != 1:
                raise RuntimeError("Private target denied")
            handle = await locator.element_handle()
            form = await handle.evaluate_handle('el => el.form || null')
            self.targets.append((locator, handle, form, kind))
            await self.validate_target(handle)
            return handle
        raise RuntimeError("Private target denied")

    async def validate_target(self, target):
        await self.validate_origin()
        locator, handle, form, kind = next(row for row in self.targets if row[1] is target)
        if await locator.count() != 1 or not await handle.is_visible():
            raise RuntimeError("Private target denied")
        valid = await locator.evaluate(TARGET_VALID,
            {"original": handle, "originalForm": form, "origin": self.origin, "kind": kind})
        if valid is not True:
            raise RuntimeError("Private target denied")
        if kind in {'code', 'submit'}:
            for _, other, _, other_kind in self.targets:
                if other_kind in {'code', 'submit'} and other_kind != kind:
                    if not await handle.evaluate('(el, other) => el.form === other.form', other):
                        raise RuntimeError('Private form denied')

    async def mutate(self, target, *, value=None):
        from worker.browser_input import hold_page_input

        async with hold_page_input(self.page):
            self.reject_provider_secret(value)
            await self.validate_target(target)
            for _, other, _, kind in self.targets:
                if other is not target and kind in {'code', 'submit', 'image'}:
                    await self.validate_target(other)
            self.action_started = True
            if value is None:
                for text in await target.evaluate("el => [el.value, el.getAttribute('href'), el.getAttribute('formaction')]"):
                    self.reject_provider_secret(text)
                await target.click(timeout=10000)
            else:
                await target.focus()
                await self.validate_target(target)
                for _, other, _, other_kind in self.targets:
                    if other is not target and other_kind in {'code', 'submit', 'image'}:
                        await self.validate_target(other)
                locator, handle, form, kind = next(row for row in self.targets if row[1] is target)
                peers = [{"original": other, "originalForm": other_form, "kind": other_kind, "origin": self.origin}
                    for _, other, other_form, other_kind in self.targets
                    if other is not target and other_kind in {'code', 'submit', 'image'}]
                if not await locator.evaluate(INSERT_VALUE, {"original": handle, "originalForm": form,
                    "origin": self.origin, "kind": kind, "value": value, "peers": peers}):
                    raise RuntimeError('Private input verification failed')

    async def pin_challenge(self):
        await self.validate_origin()
        if 'https://challenges.cloudflare.com' not in self.approved_origins:
            raise RuntimeError('Private challenge denied')
        if self.challenge is not None:
            await self.challenge.dispose()
            self.challenge = None
        if self.challenge_document is not None:
            await self.challenge_document.dispose()
            self.challenge_document = None
        self.challenge = await self.page.evaluate_handle(r"""() => {
            const candidates = (""" + CHALLENGE_CANDIDATES + r""")();
            if (candidates.length !== 1) return null;
            const target = candidates[0], r = target.getBoundingClientRect();
            return {target, document, source: target.src,
                box: {x:r.x, y:r.y, width:r.width, height:r.height}};
        }""")
        if not await self.challenge.evaluate('state => !!state'):
            raise RuntimeError('Private challenge denied')
        target = await self.challenge.evaluate_handle('state => state.target')
        try:
            self.challenge_frame = await target.as_element().content_frame()
            self.challenge_source = await self.challenge.evaluate('state => state.source')
            if self.challenge_frame is None or self.challenge_frame.url != self.challenge_source:
                raise RuntimeError('Private challenge denied')
            self.challenge_document = await self.challenge_frame.evaluate_handle('document')
        finally:
            await target.dispose()

    async def click_challenge(self, x, y):
        from worker.browser_input import hold_page_input

        async with hold_page_input(self.page):
            await self.validate_origin()
            if (self.challenge is None or self.challenge_frame is None or
                    self.challenge_frame.url != self.challenge_source):
                raise RuntimeError('Private challenge denied')
            if self.challenge_document is None or not await self.challenge_document.evaluate('original => original === document'):
                raise RuntimeError('Private challenge denied')
            position = await self.challenge.evaluate(r"""(state, {x, y}) => {
                const candidates = (""" + CHALLENGE_CANDIDATES + r""")();
                if (!state || candidates.length !== 1 || candidates[0] !== state.target ||
                    state.document !== document || state.target.src !== state.source) return null;
                const r = state.target.getBoundingClientRect(), b = state.box;
                if (r.x !== b.x || r.y !== b.y || r.width !== b.width || r.height !== b.height ||
                    !Number.isFinite(x) || !Number.isFinite(y) || x <= r.left || x >= r.right ||
                    y <= r.top || y >= r.bottom || document.elementFromPoint(x, y) !== state.target) return null;
                return {x: x - r.left, y: y - r.top};
            }""", {"x": x, "y": y})
            if position is None:
                raise RuntimeError('Private challenge denied')
            target = await self.challenge.evaluate_handle('state => state.target')
            try:
                self.action_started = True
                # A pinned iframe handle also gives Playwright's last-moment hit
                # test; never dispatch unbounded provider coordinates to the page.
                await target.as_element().click(position=position, timeout=3000)
            finally:
                await target.dispose()

    async def close(self):
        for handle in (self.challenge, self.challenge_document):
            if handle is not None:
                try:
                    await handle.dispose()
                except Exception:
                    pass
        for _, handle, form, _ in self.targets:
            for value in (handle, form):
                try:
                    await value.dispose()
                except Exception:
                    pass
        if self.iframe is not None:
            try:
                await self.iframe.dispose()
            except Exception:
                pass


def private_context(payload):
    context = (payload or {}).get('_assistant_private_context')
    if context is not None and type(context) is not PrivateContext:
        raise RuntimeError('Private capability denied')
    return context


async def run_private_step(context, step, vision, cfbox, lane_key, plugin_runtimes=None):
    from worker import executor

    feature, config = step['feature_type'], step['config']
    note = lambda *_: None
    await context.validate_origin()
    if feature == 'action.fill':
        value = config.get('value', '')
        if (secret_in_url(value, context.known) or re.search(
            r'\{[^}]*?(?:password|secret|token|otp|cookie)[^}]*\}|\bBearer\s+\S+|'
            r'(?:password|passwd|api[_-]?key|cookie|authorization)\s*[:=]', value, re.I
        ) or re.search(
            r'password|passwd|secret|token|cookie|credential|authorization|otp|2fa|captcha|one.time|\u9a8c\u8bc1\u7801',
            ' '.join(config['selector']), re.I
        )):
            raise RuntimeError('Public fill denied')
        target = await context.target(context.page, config['selector'], 'fill')
        # Also reject overwriting an existing known secret with public input.
        if secret_in_url(await target.input_value(), context.known):
            raise RuntimeError('Public fill denied')
        if config.get('clear', True):
            await context.mutate(target, value='')
        await context.mutate(target, value=value)
        return 'success', '', {}
    if feature in {'challenge.image_captcha', 'sign.iframe.image_captcha'}:
        scope = context.page
        if feature.startswith('sign.'):
            for selector in config['iframe_selector']:
                locator = context.page.locator(selector)
                if not await locator.count() or not await locator.first.is_visible():
                    continue
                if await locator.count() != 1:
                    raise RuntimeError('Private frame denied')
                context.iframe = await locator.element_handle()
                context.iframe_selector = selector
                context.frame = await context.iframe.content_frame()
                if context.frame is None:
                    raise RuntimeError('Private frame denied')
                break
            if context.frame is None:
                raise RuntimeError('Private frame denied')
            await context.validate_origin()
            scope = context.frame
        image = await context.target(scope, config['image_selector'], 'image')
        target = await context.target(scope, config['input_selector'], 'code')
        submit = (await context.target(scope, config['submit_selector'], 'submit')
                  if config.get('submit_selector') else None)
        status, text, _, _ = await executor._read_captcha(scope, image, config, note, vision,
            plugin_runtimes=plugin_runtimes, private=context)
        context.register_secret(text)
        if status != 'ok':
            return 'fail', '', {}
        await context.mutate(target, value=text)
        if submit is not None:
            await context.mutate(submit)
        return 'success', '', {'submitted': submit is not None}
    if feature == 'sign.image_quiz':
        return await executor._run_image_quiz(context.page, config, note, vision, private=context)
    if feature == 'challenge.grid_captcha':
        return await _run_grid(context, config, vision)
    if feature == 'challenge.turnstile':
        # Never delegate an assistant session to desktop/X display authority.
        return await executor._run_turnstile(context.page, config, cfbox, None, None, lane_key, note, private=context)
    raise RuntimeError('Private feature denied')


async def _run_grid(context, config, vision):
    from worker import grid_captcha as grid
    from worker.vision import answer_grid_captcha, GridCaptchaUnclear, ModelCallError

    if not vision or vision.get('supports_vision') is not True:
        return 'fail', '', {}
    state = None
    try:
        for attempt in range(config.get('max_refresh', 3) + 1):
            await context.validate_origin()
            if state is None:
                state = await grid._wait_snapshot(context.page, config, lambda *_: None)
            if state is None or not await grid._act(state, []):
                return 'fail', '', {}
            require_site_origin(await state.evaluate('state => state.root.ownerDocument.location.href'), context.origin, [])
            root = await state.evaluate_handle('state => state.root')
            try:
                image = await root.as_element().screenshot(type='png', timeout=5000)
            finally:
                await root.dispose()
            try:
                answer = await answer_grid_captcha(image, vision,
                    (vision.get('execution_settings') or {}).get('llm_prompt') or None)
            except GridCaptchaUnclear:
                if attempt == config.get('max_refresh', 3):
                    return 'fail', '', {}
                refresh = await context.target(context.page,
                    config.get('refresh_selector', ['.geetest_refresh']), 'click')
                await context.validate_target(refresh)
                context.action_started = True
                if not await grid._act(state, [], button=refresh):
                    return 'fail', '', {}
                fresh = await grid._wait_changed(context.page, config, state)
                await state.dispose()
                state = fresh
                if state is None:
                    return 'fail', '', {}
                continue
            except ModelCallError:
                return 'fail', '', {}
            if (type(answer) is not list or not answer or len(set(answer)) != len(answer)
                    or any(type(index) is not int or not 1 <= index <= 9 for index in answer)):
                return 'fail', '', {}
            context.register_secret(json.dumps(answer, separators=(',', ':')))
            selected = []
            for index in sorted(answer):
                await context.validate_origin()
                context.action_started = True
                if not await grid._act(state, selected, index=index):
                    return 'unknown', '', {}
                selected.append(index)
            submit = await context.target(context.page,
                config.get('submit_selector', ['.geetest_commit']), 'click')
            await context.validate_target(submit)
            if not await grid._act(state, selected, button=submit):
                return 'unknown', '', {}
            success = await grid._wait_success(context.page, config, state)
            if isinstance(success, str):
                return 'success', '', {'submitted': True, 'verified': True}
            if success is None:
                return 'unknown', '', {'submitted': True}
            await state.dispose()
            state = success
        return 'unknown', '', {}
    finally:
        if state is not None:
            await state.dispose()
