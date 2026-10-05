"""API tests. Run with:  python manage.py test"""
import json
import os
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase

from .models import Admin

PASS = "A long test password 1"
LEAGUE = {"g": {"A": ["a1", "a2", "a3", "a4"], "B": ["b1", "b2", "b3", "b4"]}, "r": {}, "k": {}}


class ApiTest(TestCase):
    def setUp(self):
        owner = Admin(username="owner", role=Admin.OWNER)
        owner.set_password(PASS)
        owner.save()

    # --- helpers: a browser-like client that sends the CSRF token like the page does ---
    def browser(self):
        c = Client(enforce_csrf_checks=True)
        c.csrf = c.get("/api/me").json()["csrf"]
        return c

    def post(self, c, url, data, **extra):
        return c.post(url, json.dumps(data), content_type="application/json", HTTP_X_CSRF=c.csrf, **extra)

    def login(self, user="owner", password=PASS):
        c = self.browser()
        r = self.post(c, "/api/login", {"user": user, "password": password})
        c.status = r.status_code
        c.csrf = c.get("/api/me").json()["csrf"]   # Django gives a new token after login
        return c

    def rev(self):
        return Client().get("/api/state").json()["rev"]

    def put(self, c, state, rev=None, **extra):
        return c.put("/api/state", json.dumps(state), content_type="application/json", HTTP_X_CSRF=c.csrf,
                     HTTP_X_REV=str(self.rev() if rev is None else rev), **extra)

    # --- tests ---
    def test_health_and_page(self):
        self.assertEqual(Client().get("/api/health").json(), {"ok": True})
        r = Client().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'nonce-", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertIn('<script nonce="', r.content.decode())
        self.assertEqual(Client().get("/api/nope").status_code, 404)

    def test_new_database_starts_from_seed(self):
        seed = json.loads(settings.SEED_FILE.read_text(encoding="utf-8"))
        s = Client().get("/api/state").json()
        self.assertEqual(s["g"], seed["g"])
        self.assertEqual(s["ui"]["t"], seed["ui"]["t"])
        self.assertEqual(Client().get("/api/seed").status_code, 401)
        self.assertEqual(self.login().get("/api/seed").json()["g"], seed["g"])

    def test_login(self):
        bad = self.login(password="nope")
        self.assertEqual(bad.status, 401)
        r1 = self.post(self.browser(), "/api/login", {"user": "owner", "password": "nope"}).json()
        r2 = self.post(self.browser(), "/api/login", {"user": "ghost", "password": "nope"}).json()
        self.assertEqual(r1, r2, "unknown users get the same answer")
        c = self.login()
        self.assertEqual(c.status, 200)
        self.assertEqual(c.get("/api/me").json()["user"]["role"], "owner")
        self.assertEqual(self.login(user="OWNER").status, 200, "usernames are not case-sensitive")

    def test_saving_needs_login_csrf_and_current_version(self):
        self.assertEqual(Client().put("/api/state", json.dumps(LEAGUE), content_type="application/json").status_code, 401)
        self.assertEqual(self.browser().put("/api/state", json.dumps(LEAGUE), content_type="application/json").status_code, 403, "no CSRF token")
        c = self.login()
        r = c.put("/api/state", json.dumps(LEAGUE), content_type="application/json", HTTP_X_CSRF="wrong", HTTP_X_REV=str(self.rev()))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.put(c, LEAGUE, HTTP_ORIGIN="http://evil.example").status_code, 403)
        r0 = self.rev()
        ok = self.put(c, LEAGUE, r0)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["rev"], r0 + 1)

    def test_two_editors_stale_save_refused(self):
        a, b = self.login(), self.login()
        base = self.rev()
        self.assertEqual(self.put(a, {**LEAGUE, "r": {"A0_0": [1, 0]}}, base).status_code, 200)
        stale = self.put(b, {**LEAGUE, "r": {"A0_0": [5, 5]}}, base)
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.json()["conflict"])
        self.assertEqual(Client().get("/api/state").json()["r"]["A0_0"], [1, 0])

    def test_rejects_unsafe_or_broken_data(self):
        c = self.login()
        for bad in ({"g": {"<img>": ["x", "y"]}}, {"g": {"A": ["x" * 41, "y"]}}, {**LEAGUE, "ev": {"r.A0_0": [{"s": 7}]}},
                    {**LEAGUE, "ev": {"r.A0_0": [{"s": True}]}}, {**LEAGUE, "ui": {"t": "x" * 41}}):
            self.assertEqual(self.put(c, bad).status_code, 400, bad)

    def test_group_draw_random_pots_and_current_only(self):
        c = self.login()
        players = [f"p{i}" for i in range(1, 9)]
        seen = set()
        for _ in range(12):
            r = self.post(c, "/api/draw/groups", {"players": players, "groups": 2, "pots": True})
            self.assertEqual(r.status_code, 200)
            g = r.json()["state"]["g"]
            self.assertEqual(len(g["A"]) + len(g["B"]), 8)
            for p in range(4):   # each pot of 2 is split across the groups
                pot = players[p * 2:p * 2 + 2]
                self.assertTrue(any(n in pot for n in g["A"]) and any(n in pot for n in g["B"]))
            seen.add(json.dumps(g))
        self.assertGreater(len(seen), 1, "repeated draws give different groups")
        self.assertEqual(self.post(c, "/api/draw/groups", {"players": ["x", "X", "y", "z"], "groups": 2}).status_code, 400)
        pub = Client().get("/api/draws").json()["draws"]
        self.assertEqual(len(pub), 1)
        self.assertEqual((pub[0]["kind"], pub[0]["role"]), ("groups", "owner"))

    def test_knockout_pairings_only_from_server_draw_once(self):
        c = self.login()
        self.assertEqual(self.put(c, {**LEAGUE, "kd": ["A1", "B1"]}).status_code, 409)
        pots = [[{"k": "A1", "n": "a1"}, {"k": "B1", "n": "b1"}], [{"k": "A2", "n": "a2"}, {"k": "B2", "n": "b2"}], []]
        self.post(c, "/api/draw/groups", {"players": ["a1", "a2", "b1", "b2"], "groups": 2})
        first = self.post(c, "/api/draw/knockout", {"pots": pots})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.json()["state"]["kd"]), 4)
        self.assertEqual(self.post(c, "/api/draw/knockout", {"pots": pots}).status_code, 409)
        self.assertEqual([d["kind"] for d in Client().get("/api/draws").json()["draws"]], ["knockout", "groups"])

    def test_invite_link_single_use(self):
        c = self.login()
        link = self.post(c, "/api/admins", {"username": "helper", "email": "helper@example.com"}).json()["link"]
        token = link.split("#invite=")[1]
        guest = self.browser()
        self.assertEqual(self.post(guest, "/api/token/check", {"token": token}).json(), {"kind": "invite", "username": "helper"})
        self.assertEqual(self.post(guest, "/api/token/use", {"token": token, "password": "short"}).status_code, 400)
        self.assertEqual(self.post(guest, "/api/token/use", {"token": token, "password": "Another fine password"}).status_code, 200)
        self.assertEqual(self.post(self.browser(), "/api/token/use", {"token": token, "password": "Another fine password"}).status_code, 400)
        self.assertEqual(self.login("helper", "Another fine password").status, 200)
        self.assertEqual(self.post(self.login("helper", "Another fine password"), "/api/admins/remove", {"id": 1}).status_code, 403)

    def test_lockout(self):
        for _ in range(5):
            self.login(password="wrong password")
        self.assertEqual(self.login(password="wrong password").status, 429)
        self.assertEqual(self.login().status, 429, "even the right password waits out the lock")

    def test_password_change_logs_out_other_devices(self):
        a, b = self.login(), self.login()
        r = self.post(a, "/api/password", {"current": PASS, "password": "A brand new password 2"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(a.get("/api/me").json()["admin"], "this device stays signed in")
        self.assertFalse(b.get("/api/me").json()["admin"], "other devices are logged out")

    def test_logout_everywhere(self):
        a, b = self.login(), self.login()
        self.assertEqual(self.post(a, "/api/logout-all", {}).status_code, 200)
        self.assertFalse(b.get("/api/me").json()["admin"])

    @mock.patch.dict(os.environ, {"EMAIL_PROVIDER": "console", "EMAIL_FROM": "League <league@example.com>"})
    def test_invite_email_sent(self):
        c = self.login()
        r = self.post(c, "/api/admins", {"username": "kofi", "email": "kofi@example.com"}).json()
        self.assertEqual(r, {"ok": True, "emailed": True})

    @mock.patch.dict(os.environ, {"EMAIL_PROVIDER": "brevo", "EMAIL_API_KEY": "k", "EMAIL_FROM": "league@example.com"})
    def test_brevo_request(self):
        from . import emailer
        with mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.status = 201
            emailer.send("kofi@example.com", "Hi", "text", "<p>html</p>")
            req = op.call_args[0][0]
            self.assertEqual(req.full_url, "https://api.brevo.com/v3/smtp/email")
            self.assertEqual(json.loads(req.data)["to"], [{"email": "kofi@example.com"}])


class EnsureAdminTest(TestCase):
    def test_weak_password_refused_and_random_first_login(self):
        with mock.patch.dict(os.environ, {"ADMIN_USER": "boss", "ADMIN_PASSWORD": "short"}):
            call_command("ensure_admin", stdout=open(os.devnull, "w"), stderr=open(os.devnull, "w"))
        self.assertFalse(Admin.objects.filter(username="boss").exists())
        a = Admin.objects.get(username="admin")
        self.assertTrue(a.must_change and a.role == Admin.OWNER)

    def test_creates_owner_and_does_not_reset_same_password(self):
        env = {"ADMIN_USER": "boss", "ADMIN_PASSWORD": PASS, "ADMIN_EMAIL": "boss@example.com"}
        with mock.patch.dict(os.environ, env):
            call_command("ensure_admin", stdout=open(os.devnull, "w"))
            epoch = Admin.objects.get(username="boss").session_epoch
            call_command("ensure_admin", stdout=open(os.devnull, "w"))
        a = Admin.objects.get(username="boss")
        self.assertEqual((a.role, a.email, a.session_epoch), (Admin.OWNER, "boss@example.com", epoch))
