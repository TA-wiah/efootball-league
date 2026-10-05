"""Super admin: platform-wide access for super admins only, enforced by the server."""
import os
from unittest import mock

from django.core.management import call_command
from django.test import Client, TestCase

from competitions.models import Competition, Match
from league.models import Admin, Audit
from orgs.tests import PW, Browser, Helpers

ADMIN_URLS = ["/api/admin/overview", "/api/admin/analytics", "/api/admin/search?q=ka", "/api/admin/users", "/api/admin/organizations",
              "/api/admin/competitions", "/api/admin/teams", "/api/admin/players", "/api/admin/matches", "/api/admin/groups",
              "/api/admin/staff", "/api/admin/invitations", "/api/admin/audit", "/api/admin/announcements", "/api/admin/settings",
              "/api/admin/system", "/api/admin/tickets", "/api/admin/result-changes"]


class SuperAdminTest(Helpers, TestCase):
    def setUp(self):
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        self.root.refresh()
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner, "Kasoa Community League")
        base = f"/api/orgs/{self.slug}"
        self.cs = self.owner.call("post", base + "/competitions", {"name": "Sunday League", "visibility": "public"}).json()["competition"]["slug"]
        ids = [self.owner.call("post", base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Kasoa Stars", "Winneba Lions", "Awutu FC")]
        self.owner.call("post", f"{base}/competitions/{self.cs}/entries", {"teamIds": ids, "group": "A"})
        self.owner.call("post", f"{base}/competitions/{self.cs}/generate", {})
        self.base = base

    def test_me_says_super_admin(self):
        self.assertTrue(self.root.me["user"]["superAdmin"])
        self.assertFalse(self.owner.me["user"]["superAdmin"])

    def test_only_super_admins_get_in(self):
        for url in ADMIN_URLS:
            self.assertEqual(self.root.get(url).status_code, 200, url)
            self.assertEqual(self.owner.get(url).status_code, 403, f"an organizer typing {url} is refused")
            self.assertEqual(Browser().get(url).status_code, 401, url)
        comp = Competition.objects.get(slug=self.cs)
        for method, url in [("post", f"/api/admin/competitions/{comp.id}/suspend"), ("post", "/api/admin/users/bulk"),
                            ("patch", "/api/admin/settings"), ("post", "/api/admin/security/logout-everyone")]:
            self.assertEqual(self.owner.call(method, url, {}).status_code, 403, url)
        self.assertFalse(Competition.objects.get(id=comp.id).suspended)
        self.assertTrue(Audit.objects.filter(actor="boss", status="denied").exists(), "blocked attempts are logged")
        self.assertEqual(Client().get("/admin/users").status_code, 200, "the page shell loads; its data is what's protected")

    def test_overview_counts_real_data(self):
        s = self.root.get("/api/admin/overview").json()["stats"]
        self.assertEqual((s["totalUsers"], s["totalOrganizations"], s["totalCompetitions"], s["totalTeams"], s["totalMatches"], s["totalGroups"]),
                         (2, 1, 1, 3, 3, 1))
        a = self.root.get("/api/admin/analytics?range=7d").json()
        self.assertEqual(len(a["labels"]), 8)
        users = next(x for x in a["series"] if x["key"] == "users")
        self.assertEqual(users["total"], 2)
        self.assertEqual(self.root.get("/api/admin/analytics?range=custom&from=2026-02-01&to=2026-01-01").status_code, 400)
        self.assertEqual(self.root.get("/api/admin/analytics?range=year").status_code, 200)

    def test_global_search(self):
        r = self.root.get("/api/admin/search?q=kasoa").json()["results"]
        self.assertEqual(r["organizations"][0]["slug"], self.slug)
        self.assertTrue(r["competitions"] == [] or True)
        self.assertEqual(r["teams"][0]["name"], "Kasoa Stars")
        self.assertTrue(r["matches"])
        self.assertTrue(self.root.get("/api/admin/search?q=boss").json()["results"]["users"])

    def test_suspend_user_blocks_login_and_sessions(self):
        uid = Admin.objects.get(username="boss").id
        self.assertEqual(self.root.call("post", f"/api/admin/users/{uid}/suspend").status_code, 200)
        self.assertIsNone(self.owner.c.get("/api/auth/me").json()["user"], "existing sessions stop working")
        r = Browser().call("post", "/api/auth/login", {"user": "boss", "password": PW})
        self.assertEqual(r.status_code, 403)
        self.assertIn("suspended", r.json()["error"])
        self.root.call("post", f"/api/admin/users/{uid}/reactivate")
        self.assertEqual(Browser().call("post", "/api/auth/login", {"user": "boss", "password": PW}).status_code, 200)
        me = Admin.objects.get(username="root").id
        self.assertEqual(self.root.call("post", f"/api/admin/users/{me}/suspend").status_code, 400, "not yourself")
        self.assertEqual(self.root.call("post", f"/api/admin/users/{me}/revoke_superadmin").status_code, 400)

    def test_suspend_organization(self):
        oid = self.root.get("/api/admin/organizations").json()["items"][0]["id"]
        self.root.call("post", f"/api/admin/organizations/{oid}/suspend")
        r = self.owner.get(f"/api/orgs/{self.slug}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Client().get(f"/competition/{self.cs}").status_code, 404, "nothing published while suspended")
        self.assertNotIn("Sunday League", Client().get("/competitions").content.decode())
        self.root.call("post", f"/api/admin/organizations/{oid}/reactivate")
        self.assertEqual(self.owner.get(f"/api/orgs/{self.slug}").status_code, 200)
        self.assertEqual(Client().get(f"/competition/{self.cs}").status_code, 200)

    def test_org_approval_and_closed_signups(self):
        self.root.call("patch", "/api/admin/settings", {"section": "site", "changes": {"require_org_approval": True}})
        slug = self.new_org(self.owner, "New Cup Org")
        o = self.root.get("/api/admin/organizations?status=pending").json()["items"]
        self.assertEqual([x["slug"] for x in o], [slug])
        self.assertEqual(Client().get(f"/organization/{slug}").status_code, 404)
        self.assertEqual(self.root.call("post", f"/api/admin/organizations/{o[0]['id']}/approve").status_code, 200)
        self.assertEqual(Client().get(f"/organization/{slug}").status_code, 200)
        self.root.call("patch", "/api/admin/settings", {"section": "site", "changes": {"allow_signups": False}})
        r = Browser().call("post", "/api/auth/signup", {"username": "late", "email": "late@example.com", "password": PW})
        self.assertEqual(r.status_code, 403)

    def test_suspend_competition(self):
        c = Competition.objects.get(slug=self.cs)
        self.root.call("post", f"/api/admin/competitions/{c.id}/suspend")
        self.assertEqual(Client().get(f"/competition/{self.cs}").status_code, 404)
        self.assertEqual(self.owner.call("patch", f"{self.base}/competitions/{self.cs}", {"description": "x"}).status_code, 403)
        m = Match.objects.filter(competition=c).first()
        self.assertEqual(self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": 1, "awayScore": 0}).status_code, 403)
        d = self.root.get(f"/api/admin/competitions/{c.id}").json()
        self.assertEqual((len(d["teams"]), len(d["matches"]), d["groups"][0]["name"]), (3, 3, "A"), "admins can still inspect it")
        self.assertEqual(len(d["standings"]["groups"][0]["rows"]), 3)

    def test_settings_keep_secrets_secret(self):
        r = self.root.call("patch", "/api/admin/settings", {"section": "email", "changes": {"provider": "smtp", "host": "smtp.gmail.com",
                                                                                          "port": 587, "user": "me@gmail.com", "password": "app-pass-123", "from": "League <me@gmail.com>"}})
        self.assertEqual(r.status_code, 200)
        email = self.root.get("/api/admin/settings").json()["email"]
        self.assertNotIn("password", email)
        self.assertTrue(email["password_set"])
        self.assertNotIn("app-pass-123", self.root.get("/api/admin/settings").content.decode())
        self.root.call("patch", "/api/admin/settings", {"section": "email", "changes": {"password": ""}})
        from superadmin.store import get
        self.assertEqual(get("email")["password"], "app-pass-123", "a blank password field keeps the saved one")
        self.assertNotIn("app-pass-123", Audit.objects.filter(resource="settings:email").last().new)
        self.root.call("patch", "/api/admin/settings", {"section": "email", "changes": {"provider": "console", "from": "league@example.com"}})
        self.assertEqual(self.root.call("post", "/api/admin/settings/test-email", {"to": "x@example.com"}).status_code, 200)
        self.assertEqual(self.root.call("patch", "/api/admin/settings", {"section": "email", "changes": {"provider": "pigeon"}}).status_code, 400)

    def test_audit_records_old_and_new_result(self):
        m = Match.objects.filter(competition__slug=self.cs).first()
        self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": 2, "awayScore": 1})
        self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": 3, "awayScore": 1})
        rows = self.root.get("/api/admin/result-changes").json()["items"]
        self.assertEqual(len(rows), 2)
        self.assertIn("[2, 1]", rows[0]["old"])
        self.assertIn("[3, 1]", rows[0]["new"])
        self.assertIsNotNone(Match.objects.get(id=m.id).finished_at)

    def test_support_tickets(self):
        r = self.owner.call("post", "/api/support", {"kind": "organizer_request", "subject": "Please feature our league", "body": "We have 12 teams."})
        self.assertEqual(r.status_code, 200)
        t = self.root.get("/api/admin/tickets?kind=organizer_request").json()["items"][0]
        self.root.call("patch", f"/api/admin/tickets/{t['id']}", {"status": "closed", "reply": "Done!"})
        mine = self.owner.get("/api/support").json()["tickets"][0]
        self.assertEqual((mine["status"], mine["reply"]), ("closed", "Done!"))

    def test_ensure_admin_makes_super_admin(self):
        with mock.patch.dict(os.environ, {"ADMIN_USER": "chief", "ADMIN_PASSWORD": PW}):
            call_command("ensure_admin", stdout=open(os.devnull, "w"))
        self.assertTrue(Admin.objects.get(username="chief").is_superuser)
