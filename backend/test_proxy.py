"""Offline WSGI contract tests. No network, no real credentials."""
import base64
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from proxy import make_app, Failure, LIMIT, MAX_BODY, authenticate, upstream, NoRedirect

CHAT = {"model": "gpt-4o", "messages": [
    {"role": "system", "content": "You are a problem creator."},
    {"role": "user", "content": "test question"},
]}
IMAGE = {"images": [{"format": "jpg", "url": None,
                     "data": base64.b64encode(b"\xff\xd8\xfftest\xff\xd9").decode()}]}


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "CLOVA_OCR_URL": "https://example.invalid/ocr", "CLOVA_OCR_SECRET": "fake"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.call = Mock(return_value={"choices": [{"message": {"content": "answer"}}]})
        self.verify = Mock(return_value="user-a")
        self.app = make_app(self.verify, self.call)

    def request(self, path="/chat", payload=CHAT, token="fake-token", method="POST", length=None, content_type="application/json"):
        raw = json.dumps(payload).encode()
        env = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_TYPE": content_type,
               "CONTENT_LENGTH": str(len(raw) if length is None else length), "wsgi.input": io.BytesIO(raw)}
        if token is not None:
            env["HTTP_AUTHORIZATION"] = "Bearer " + token
        start = Mock()
        body = json.loads(b"".join(self.app(env, start)))
        return int(start.call_args.args[0].split()[0]), body

    def test_chat_contract(self):
        self.assertEqual(self.request(), (200, {"choices": [{"message": {"content": "answer"}}]}))
        self.verify.assert_called_once_with("fake-token")
        self.assertEqual(self.call.call_args.args[2]["max_completion_tokens"], 1024)

    def test_missing_token(self):
        self.assertEqual(self.request(token=None)[0], 401)
        self.call.assert_not_called()

    def test_invalid_session(self):
        self.verify.side_effect = Failure(401, "invalid_session")
        self.assertEqual(self.request()[0], 401)
        self.call.assert_not_called()

    def test_model_allowlist(self):
        self.assertEqual(self.request(payload={**CHAT, "model": "other"})[0], 400)
        self.call.assert_not_called()

    def test_invalid_system_role(self):
        self.assertEqual(self.request(payload={**CHAT, "messages": [{"role": "system", "content": "other"}, CHAT["messages"][1]]})[0], 400)

    def test_body_limit(self):
        self.assertEqual(self.request(length=MAX_BODY+1)[0], 413)
        self.verify.assert_not_called()

    def test_content_type(self):
        self.assertEqual(self.request(content_type="text/plain")[0], 415)

    def test_ocr_contract(self):
        self.call.return_value = {"images": [{"fields": [{"inferText": "test"}]}]}
        self.assertEqual(self.request("/ocr", IMAGE), (200, {"images": [{"fields": [{"inferText": "test"}]}]}))

    def test_ocr_reject_remote_url(self):
        self.assertEqual(self.request("/ocr", {"images": [{**IMAGE["images"][0], "url": "https://example.invalid"}]})[0], 400)
        self.call.assert_not_called()

    def test_ocr_invalid_data(self):
        self.assertEqual(self.request("/ocr", {"images": [{**IMAGE["images"][0], "data": "not-base64"}]})[0], 400)

    def test_quota(self):
        for _ in range(LIMIT):
            self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(self.call.call_count, LIMIT)
        self.verify.return_value = "user-b"
        self.assertEqual(self.request()[0], 200)

    def test_upstream_error_is_sanitized(self):
        self.call.side_effect = Failure(502, "upstream_failure")
        self.assertEqual(self.request(), (502, {"error": "upstream_failure"}))

    def test_bad_upstream_shape(self):
        self.call.return_value = {"unexpected": "response"}
        self.assertEqual(self.request()[0], 502)

    def test_unknown_route(self):
        self.assertEqual(self.request("/forward")[0], 404)

    def test_health(self):
        self.assertEqual(self.request("/health", method="GET"), (200, {"ok": True}))
        self.verify.assert_not_called()

    def test_auth_calls_supabase_user_endpoint(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://example.invalid", "SUPABASE_ANON_KEY": "fake"}), patch("proxy.upstream", return_value={"id": "user-a"}) as remote:
            self.assertEqual(authenticate("fake-token"), "user-a")
            self.assertEqual(remote.call_args.args[0], "https://example.invalid/auth/v1/user")
            self.assertTrue(remote.call_args.kwargs["auth"])

    def test_redirect_disabled(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))


if __name__ == "__main__":
    unittest.main()
