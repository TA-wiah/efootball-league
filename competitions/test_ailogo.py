"""AI logo images: off until a super admin sets a key; managers only; real clubs and brands refused; a daily limit."""
import io
import json
from unittest import mock

from django.test import TestCase

from league.models import Admin
from orgs.tests import Helpers
from superadmin import store


class AiLogoTest(Helpers, TestCase):
    def setUp(self):
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)

    def fake(self, *a, **k):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps({"data": [{"b64_json": "iVBORw0KGgo="}]}).encode()
        self.sent = json.loads(a[0].data)
        self.auth = a[0].headers.get("Authorization")
        return resp

    def test_ai_logo(self):
        self.assertFalse(self.boss.get("/api/ai-logo").json()["enabled"], "off by default")
        self.assertEqual(self.boss.call("post", "/api/ai-logo", {"prompt": "a blue lion"}).status_code, 400)
        store.update("ai", {"enabled": True, "api_key": "sk-test", "daily_limit": 2})
        self.assertNotIn("sk-test", json.dumps(store.masked("ai")), "the key never goes to browsers")
        self.assertTrue(self.boss.get("/api/ai-logo").json()["enabled"])
        fan = self.signup("fan")
        self.invite_and_join(self.boss, self.slug, fan, "viewer")
        self.assertEqual(fan.call("post", "/api/ai-logo", {"prompt": "a blue lion"}).status_code, 403, "managers only")
        r = self.boss.call("post", "/api/ai-logo", {"prompt": "the Real Madrid badge in blue"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("belongs to someone else", r.json()["error"])
        with mock.patch("urllib.request.urlopen", side_effect=self.fake):
            r = self.boss.call("post", "/api/ai-logo", {"prompt": "a blue lion with a gold crown", "name": "Accra Lions"})
            self.assertEqual(r.status_code, 200, r.json())
            self.assertTrue(r.json()["image"].startswith("data:image/png;base64,"))
            self.assertIn("Accra Lions", self.sent["prompt"])
            self.assertIn("does not resemble any real football club", self.sent["prompt"])
            self.assertEqual(self.auth, "Bearer sk-test")
            self.boss.call("post", "/api/ai-logo", {"prompt": "a green eagle"})
            self.assertEqual(self.boss.call("post", "/api/ai-logo", {"prompt": "a red bull"}).status_code, 429, "daily limit")
        self.assertEqual(self.boss.get("/api/ai-logo").json()["left"], 0)
