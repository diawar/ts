"""Verify temporary public access without transmitting real credentials."""
from pathlib import Path
import re
import time
import os

import httpx


url = os.environ.get('TUNNEL_URL') or (Path(__file__).resolve().parents[1] / "data/tunnel-url.txt").read_text(encoding="ascii").strip()
if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", url):
    raise ValueError("Unexpected tunnel hostname")
with httpx.Client(base_url=url, timeout=20, follow_redirects=False, trust_env=False) as client:
    for attempt in range(5):
        try:
            login = client.get("/login/")
            if login.status_code == 200:
                break
        except httpx.TransportError:
            if attempt == 4:
                raise
        time.sleep(2)
    else:
        raise AssertionError("Public login is unavailable")
    assert 'id="id_username"' in login.text and 'type="password"' in login.text
    home = client.get('/')
    assert home.status_code == 200
    assert 'href="/login/"' in home.text and 'href="/register/"' in home.text
    assert 'В приложение' not in home.text
    for path in ('/main/', '/search/', '/workspace/', '/settings/', '/administration/'):
        response = client.get(path)
        assert response.status_code == 302 and response.headers['location'].startswith('/login/'), path
    assert client.get("/admin/").status_code == 403
    assert client.get("/admin/auth/user/").status_code == 403
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^\"]+)"', login.text).group(1)
    assert any(cookie.name == "csrftoken" and cookie.secure for cookie in client.cookies.jar)
    data = {"csrfmiddlewaretoken": csrf, "username": "tunnel-connectivity-check", "password": "invalid-check-password"}
    response = client.post("/login/", data=data, headers={"Origin": url})
    assert response.status_code == 200 and "Не удалось войти" in response.text
    assert client.post("/login/", data=data, headers={"Origin": "https://untrusted.example"}).status_code == 403
with httpx.Client(timeout=10, trust_env=False) as client:
    assert client.get(os.environ.get('LOCAL_APP_URL', 'http://127.0.0.1:8001')+'/login/').status_code == 200
print("HTTPS tunnel OK: protected routes, secure cookie, CSRF origin checks; localhost unchanged")
print(url)
