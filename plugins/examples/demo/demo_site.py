"""Local authentication fixtures with public demo factors; not a production service."""
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from nexusphp_gd5.generator import NexusPhpGD5Generator


def captcha(seed=5, scaled=False):
    image, text = NexusPhpGD5Generator(seed=seed).render()
    if scaled:
        image = image.resize((151, 41), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), text


class Handler(BaseHTTPRequestHandler):
    sessions = set()
    lock = threading.Lock()
    login_results = []  # Booleans only: test evidence, never submitted factors.
    fault_mutations = 0

    def authenticated(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        token = cookie.get("demo_session")
        with self.lock:
            return bool(token and token.value in self.sessions)

    def respond(self, body, content_type="text/html; charset=utf-8", status=200, cookie=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/health":
            return self.respond('{"ok":true}', "application/json")
        if url.path == "/fault":
            return self.respond('''<p>Disposable fault fixture</p><script>
setTimeout(() => {
  const button = document.createElement('button');
  button.id = 'delayed-action'; button.textContent = 'Mutate fixture';
  button.onclick = () => fetch('/fault-mutation');
  document.body.appendChild(button);
}, 4000);
</script>''')
        if url.path == "/fault-mutation":
            with self.lock:
                type(self).fault_mutations += 1
            return self.respond('{"ok":true}', "application/json")
        if url.path in {"/", "/login"}:
            return self.respond('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width"><title>Demo login</title><h1>Demo login</h1><form method="post" action="/login"><label>Username <input name="username" required></label><label>Password <input name="password" type="password" required></label><label>2FA <input name="code" required></label><button type="submit">Log in</button></form></html>')
        if not self.authenticated():
            return self.respond('<p id="error">Login required</p>', status=401)
        if url.path == "/captcha.png":
            return self.respond(captcha(scaled=query.get("scaled") == ["1"])[0], "image/png")
        if url.path not in {"/", "/checkin", "/form", "/popup", "/iframe", "/frame", "/captcha"}:
            return self.respond("Not found", status=404)
        content = '<button id="sign" onclick="document.querySelector(\'#success\').textContent=\'Demo checkin successful\'">Check in</button><p id="success"></p>'
        if url.path == "/form":
            content = '<form method="post" action="/form"><label>Demo message <input name="message" id="message" required maxlength="100"></label><button id="sign">Submit</button></form>'
        elif url.path == "/popup":
            content = '<a id="popup" href="/checkin" target="_blank">Open same-origin checkin popup</a>'
        elif url.path == "/iframe":
            content = '<iframe title="Demo frame" src="/frame"></iframe><p>Automation alternative: <a href="/frame">open frame as a top-level page</a></p>'
        elif url.path == "/captcha":
            scaled = "1" if query.get("scaled") == ["1"] else "0"
            content = f'<form method="post" action="/verify"><img id="captcha" src="/captcha.png?scaled={scaled}" alt="Demo captcha"><input id="answer" name="answer" required><button id="sign">Verify</button></form>'
        self.respond('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>SiteFlow Demo</title><style>body{font:18px system-ui;max-width:800px;margin:40px auto;padding:20px}a{margin-right:15px}input,button{padding:10px}iframe{width:100%;height:180px}</style><h1>SiteFlow Demo</h1><nav><a href="/checkin">Checkin</a><a href="/form">Form</a><a href="/popup">Popup</a><a href="/iframe">Iframe</a><a href="/captcha">Captcha</a><a href="/captcha?scaled=1">Scaled captcha</a></nav><hr>' + content + '</html>')

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 4096:
            return self.respond("Invalid body size", status=400)
        values = parse_qs(self.rfile.read(size).decode())
        if self.path == "/login":
            ok = all(values.get(key) == [expected] for key, expected in
                     (("username", "admin"), ("password", "password"), ("code", "123456")))
            with self.lock:
                self.login_results.append(ok)
                token = secrets.token_urlsafe(32) if ok else None
                if token:
                    self.sessions.add(token)
            if not ok:
                return self.respond('<p id="error">Invalid login factors</p>', status=401)
            return self.respond('<p id="login-success">Demo login verified: credentials and 2FA</p><button id="login-confirm" type="button">Confirm result</button>',
                                cookie=f"demo_session={token}; HttpOnly; SameSite=Strict; Path=/")
        if not self.authenticated():
            return self.respond('<p id="error">Login required</p>', status=401)
        if self.path == "/verify":
            ok = values.get("answer", [""])[0] == captcha()[1]
            return self.respond('<p id="success">Captcha verified</p>' if ok else '<p id="error">Incorrect captcha</p>', status=200 if ok else 422)
        if self.path == "/form":
            return self.respond('<p id="success">Demo form submitted</p>')
        self.respond("Not found", status=404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
