"""Authenticated, fixed-purpose AI/OCR proxy. WSGI application; no external packages."""
import base64
import binascii
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

MAX_BODY = 5 * 1024 * 1024
MAX_IMAGE = 3 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
WINDOW = 60
LIMIT = 12
ROLES = {
    "You are a problem creator.",
    "You only check the users answers is correct or uncorrect. Do not say correct answer",
    "You give a solution",
}


class Failure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def https_url(value):
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise Failure(503, "configuration_required")
    return value


def setting(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise Failure(503, "configuration_required")
    return value


def upstream(url, headers, payload=None, auth=False):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(https_url(url), data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
            raw = r.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise Failure(502, "upstream_response_too_large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("invalid upstream shape")
        return result
    except urllib.error.HTTPError as exc:
        if auth and exc.code in (400, 401, 403):
            raise Failure(401, "invalid_session") from None
        if exc.code == 429:
            raise Failure(429, "upstream_rate_limit") from None
        raise Failure(502, "upstream_failure") from None
    except (OSError, ValueError):
        raise Failure(502, "upstream_failure") from None


def authenticate(token):
    base = https_url(setting("SUPABASE_URL")).rstrip("/")
    if urlsplit(base).query:
        raise Failure(503, "configuration_required")
    user = upstream(base + "/auth/v1/user", {
        "apikey": setting("SUPABASE_ANON_KEY"),
        "Authorization": "Bearer " + token,
    }, auth=True)
    uid = user.get("id")
    if not isinstance(uid, str) or not uid or len(uid) > 128:
        raise Failure(401, "invalid_session")
    return uid


class Limiter:
    """Single-process fixed-window limiter; use shared storage in production."""
    def __init__(self):
        self.lock = threading.Lock()
        self.window = -1
        self.counts = {}

    def take(self, uid):
        with self.lock:
            now = int(time.monotonic() // WINDOW)
            if now != self.window:
                self.window = now
                self.counts.clear()
            if len(self.counts) >= 4096 and uid not in self.counts:
                raise Failure(429, "rate_limit")
            count = self.counts.get(uid, 0)
            if count >= LIMIT:
                raise Failure(429, "rate_limit")
            self.counts[uid] = count + 1


def chat(payload, call=upstream):
    if payload.get("model") != "gpt-4o":
        raise Failure(400, "unsupported_model")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise Failure(400, "invalid_messages")
    a, b = messages
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise Failure(400, "invalid_messages")
    if a.get("role") != "system" or not isinstance(a.get("content"), str) or a["content"] not in ROLES:
        raise Failure(400, "invalid_messages")
    user = b.get("content")
    if b.get("role") != "user" or not isinstance(user, str) or not user.strip() or len(user) > 12000:
        raise Failure(400, "invalid_messages")
    result = call("https://api.openai.com/v1/chat/completions", {
        "Authorization": "Bearer " + setting("OPENAI_API_KEY"),
        "Content-Type": "application/json",
    }, {"model": "gpt-4o", "messages": [
        {"role": "system", "content": a["content"]},
        {"role": "user", "content": user},
    ], "max_completion_tokens": 1024})
    try:
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise Failure(502, "invalid_upstream_response") from None
    return {"choices": [{"message": {"content": content}}]}


def ocr(payload, call=upstream):
    images = payload.get("images")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise Failure(400, "invalid_image")
    image = images[0]
    if image.get("url") is not None or image.get("format") not in ("jpg", "jpeg", "png"):
        raise Failure(400, "invalid_image")
    encoded = image.get("data")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE + 2) // 3):
        raise Failure(413, "image_too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise Failure(400, "invalid_image") from None
    if len(raw) > MAX_IMAGE:
        raise Failure(413, "image_too_large")
    if raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"):
        fmt = "jpg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        fmt = "png"
    else:
        raise Failure(400, "invalid_image")
    result = call(setting("CLOVA_OCR_URL"), {
        "X-OCR-SECRET": setting("CLOVA_OCR_SECRET"),
        "Content-Type": "application/json",
    }, {"version": "V1", "requestId": uuid.uuid4().hex,
        "timestamp": int(time.time() * 1000), "lang": "ko",
        "images": [{"format": fmt, "name": "image", "data": encoded}]})
    try:
        fields = result["images"][0]["fields"]
        if not isinstance(fields, list):
            raise ValueError()
        cleaned = [{"inferText": f["inferText"]} for f in fields
                   if isinstance(f, dict) and isinstance(f.get("inferText"), str)]
    except (KeyError, IndexError, TypeError, ValueError):
        raise Failure(502, "invalid_upstream_response") from None
    return {"images": [{"fields": cleaned}]}


def make_app(verify=authenticate, call=upstream, limiter=None):
    limiter = limiter if limiter is not None else Limiter()

    def app(env, start_response):
        try:
            route = env.get("PATH_INFO", "")
            if route == "/health" and env.get("REQUEST_METHOD") == "GET":
                status, result = 200, {"ok": True}
            else:
                if route not in ("/chat", "/ocr"):
                    raise Failure(404, "not_found")
                if env.get("REQUEST_METHOD") != "POST":
                    raise Failure(405, "method_not_allowed")
                if env.get("CONTENT_TYPE", "").split(";")[0].strip().lower() != "application/json":
                    raise Failure(415, "unsupported_media_type")
                try:
                    size = int(env.get("CONTENT_LENGTH", ""))
                except ValueError:
                    raise Failure(411, "length_required") from None
                if size <= 0 or size > MAX_BODY:
                    raise Failure(413, "invalid_body_size")
                match = re.fullmatch(r"Bearer ([A-Za-z0-9._~-]{1,4096})", env.get("HTTP_AUTHORIZATION", ""))
                if not match:
                    raise Failure(401, "authentication_required")
                uid = verify(match.group(1))
                limiter.take(uid)
                try:
                    raw = env["wsgi.input"].read(size)
                    if len(raw) != size:
                        raise ValueError()
                    payload = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise Failure(400, "invalid_json") from None
                if not isinstance(payload, dict):
                    raise Failure(400, "invalid_json")
                result = (chat if route == "/chat" else ocr)(payload, call)
                status = 200
        except Failure as exc:
            status, result = exc.status, {"error": exc.code}
        except Exception:
            status, result = 500, {"error": "internal_error"}
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        from http import HTTPStatus
        start_response(f"{status} {HTTPStatus(status).phrase}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])
        return [body]
    return app


application = make_app()

if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    with make_server("127.0.0.1", 8080, application) as server:
        server.serve_forever()
