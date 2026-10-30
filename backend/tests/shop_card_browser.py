"""Isolated regression: worker + dedicated remote backend, never production profiles."""
import asyncio
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs
from unittest.mock import patch

from worker.browser_backend import ManagerBackend
from worker.executor import _extract_card_info, _purchase_cards


async def main():
    fixture = Path(__file__).with_name('shop_card_fixture.html').read_text()
    captured = []
    tokens = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
                return
            token = f'token-{len(tokens)}'
            tokens.append(token)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(fixture.replace('TOKEN', token).encode())

        def do_POST(self):
            captured.append((self.path, parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    backend = ManagerBackend(Path('/tmp/shop-browser-smoke'))
    session = await backend.start('shop-smoke', None)
    try:
        page = await session.context.new_page()
        url = f'http://127.0.0.1:{server.server_port}/mybonus.php'
        await page.goto(url)
        log = lambda *_args: None
        info = await _extract_card_info(page, {}, log)
        assert info['price'] == 3000 and info['option'] == '0', info
        assert info['fields'] == {'option': '0', 'csrf': 'token-0', 'redeem': '交换'}, info
        assert info['action'] == url + '?action=exchange', info
        async def no_sleep(*_args):
            pass
        with patch('worker.executor.asyncio.sleep', no_sleep):
            result = await _purchase_cards(page, {'max_price': 2000}, url, 1, log)
            assert result['status'] == 'fail' and not captured, result
            await page.locator('p').evaluate("el => el.textContent = '补签卡: 0 魔力值: 500'")
            result = await _purchase_cards(page, {}, url, 1, log)
            assert result['bought'] == 0 and not captured, result
            await page.goto(url)
            result = await _purchase_cards(page, {'purchase_interval_seconds': 0}, url, 2, log)
            assert result['bought'] == 2, result
            assert [item[1]['csrf'] for item in captured] == [[tokens[1]], [tokens[2]]], captured
            assert all(path == '/mybonus.php?action=exchange' and fields['redeem'] == ['交换'] for path, fields in captured)
            await page.locator('form.magic-card').last.evaluate("el => el.action = 'https://invalid.example/exchange'")
            result = await _purchase_cards(page, {}, url, 1, log)
            assert result['status'] == 'fail' and len(captured) == 2, result
        await page.evaluate('html => document.body.innerHTML = html', fixture + fixture)
        assert await _extract_card_info(page, {}, log) is None
        await page.evaluate('html => document.body.innerHTML = html', '''<table><tr><td><h2>购买补签卡</h2><input name="option" value="9"></td><td>999999</td><td>1,200</td></tr></table>''')
        info = await _extract_card_info(page, {}, log)
        assert info['price'] == 1200 and info['option'] == '9', info
        await page.evaluate('html => document.body.innerHTML = html', fixture.replace('购买补签卡', '改名卡'))
        assert await _extract_card_info(page, {}, log) is None
        print('PASS: browser DOM, legacy table, scoped coin price, zero option, hidden fields, button text, limits, refreshed CSRF, local POST, cross-origin rejection, ambiguity')
    finally:
        await backend.stop(session)
        await backend.close()
        server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
