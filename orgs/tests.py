"""Platform tests: accounts, organizations, roles and invitations. Every permission is checked against the API
directly (as an attacker would), not through the UI."""
import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from league.models import Admin

from .models import Invitation, Membership, Organization
from .permissions import ROLES, assignable_roles, can

PW = "Long enough password 1"


class Browser:
    """A logged-in (or anonymous) browser that sends the CSRF token like the real page."""

    def __init__(self):
        self.c = Client(enforce_csrf_checks=True)
        self.refresh()

    def refresh(self):
        self.me = self.c.get("/api/auth/me").json()
        self.csrf = self.me["csrf"]

    def call(self, method, url, data=None):
        r = getattr(self.c, method)(url, json.dumps(data or {}), content_type="application/json", HTTP_X_CSRF=self.csrf)
        if url.startswith("/api/auth/") or url.endswith("/accept"):
            self.refresh()
        return r

    def get(self, url):
        return self.c.get(url)


class PlatformTest(TestCase):
    def signup(self, name, email=None):
        b = Browser()
        r = b.call("post", "/api/auth/signup", {"username": name, "email": email or f"{name}@example.com", "password": PW})
        assert r.status_code == 200, r.json()
        return b

    def new_org(self, b, name="Kasoa Community League"):
        r = b.call("post", "/api/orgs", {"name": name, "country": "Ghana", "region": "Central"})
        self.assertEqual(r.status_code, 200, r.json())
        return r.json()["org"]["slug"]

    def invite_and_join(self, owner, slug, member, role):
        link = owner.call("post", f"/api/orgs/{slug}/invitations", {"role": role}).json()["link"]
        token = link.rsplit("/", 1)[1]
        r = member.call("post", f"/api/invitations/{token}/accept")
        self.assertEqual(r.status_code, 200, r.json())

    def member_id(self, b, slug, username):
        return next(m["id"] for m in b.get(f"/api/orgs/{slug}/members").json()["members"] if m["username"] == username)

    # ---------- accounts ----------
    def test_signup_login_logout(self):
        b = self.signup("kofi")
        self.assertEqual(b.me["user"]["username"], "kofi")
        self.assertFalse(b.me["user"]["leagueAccess"], "new accounts can't edit the original league")
        b.call("post", "/api/auth/logout")
        self.assertIsNone(b.me["user"])
        r = b.call("post", "/api/auth/login", {"user": "kofi@example.com", "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b.me["user"]["username"], "kofi")

    def test_signup_validation(self):
        self.signup("kofi")
        b = Browser()
        for data, status in [({"username": "x", "email": "a@b.co", "password": PW}, 400),
                             ({"username": "ama", "email": "nope", "password": PW}, 400),
                             ({"username": "ama", "email": "ama@example.com", "password": "short"}, 400),
                             ({"username": "KOFI", "email": "new@example.com", "password": PW}, 409),
                             ({"username": "ama", "email": "KOFI@example.com", "password": PW}, 409)]:
            self.assertEqual(b.call("post", "/api/auth/signup", data).status_code, status, data)

    def test_platform_account_cannot_edit_original_league(self):
        b = self.signup("kofi")
        csrf = b.c.get("/api/me").json()["csrf"]
        self.assertFalse(b.c.get("/api/me").json()["admin"])
        r = b.c.put("/api/state", "{}", content_type="application/json", HTTP_X_CSRF=csrf, HTTP_X_REV="1")
        self.assertEqual(r.status_code, 401)
        r = b.c.post("/api/login", json.dumps({"user": "kofi", "password": PW}), content_type="application/json", HTTP_X_CSRF=csrf)
        self.assertEqual(r.status_code, 401, "league login is only for league editors")

    # ---------- organizations ----------
    def test_create_org_owner_and_unique_slug(self):
        b = self.signup("kofi")
        s1, s2 = self.new_org(b), self.new_org(b)
        self.assertEqual((s1, s2), ("kasoa-community-league", "kasoa-community-league-2"))
        d = b.get(f"/api/orgs/{s1}").json()
        self.assertEqual(d["me"]["role"], "owner")
        self.assertIn("org.delete", d["me"]["perms"])
        self.assertEqual(d["stats"]["members"], 1)
        self.assertEqual(len(Browser().get("/api/auth/me").json()["orgs"]), 0)

    def test_organizations_are_isolated(self):
        a, b = self.signup("kofi"), self.signup("ama")
        slug = self.new_org(a)
        for method, url, data in [("get", f"/api/orgs/{slug}", None), ("get", f"/api/orgs/{slug}/members", None),
                                  ("get", f"/api/orgs/{slug}/invitations", None), ("patch", f"/api/orgs/{slug}", {"name": "Hacked"}),
                                  ("delete", f"/api/orgs/{slug}", {"confirm": "Kasoa Community League"}),
                                  ("post", f"/api/orgs/{slug}/invitations", {"role": "admin"}),
                                  ("post", f"/api/orgs/{slug}/transfer", {"memberId": 1})]:
            r = b.get(url) if method == "get" else b.call(method, url, data)
            self.assertEqual(r.status_code, 404, f"{method} {url} must look like it doesn't exist")
        self.assertEqual(Organization.objects.get(slug=slug).name, "Kasoa Community League")
        self.assertEqual(Browser().get(f"/api/orgs/{slug}").status_code, 401)

    def test_user_in_several_orgs_with_different_roles(self):
        a, b = self.signup("kofi"), self.signup("ama")
        own = self.new_org(a, "Kasoa Community League")
        other = self.new_org(b, "School Championship")
        self.invite_and_join(b, other, a, "editor")
        roles = {o["slug"]: o["role"] for o in a.me["orgs"]}
        self.assertEqual(roles, {own: "owner", other: "editor"})
        self.assertEqual(a.call("post", f"/api/orgs/{other}/invitations", {"role": "viewer"}).status_code, 403)

    # ---------- the permission table ----------
    def test_permission_matrix(self):
        self.assertTrue(all(can("owner", p) for p in ["org.delete", "org.transfer", "org.settings", "members.manage"]))
        for role in ["organizer", "admin"]:
            self.assertTrue(can(role, "competitions.manage") and can(role, "results.enter") and can(role, "members.invite"))
            self.assertFalse(can(role, "org.delete") or can(role, "org.transfer"))
        self.assertTrue(can("editor", "results.enter") and can("editor", "teams.manage") and can("editor", "content.edit"))
        self.assertFalse(can("editor", "members.invite") or can("editor", "org.delete"))
        self.assertTrue(can("moderator", "content.moderate"))
        self.assertFalse(can("moderator", "results.enter"))
        self.assertEqual([r for r in ROLES if can("viewer", r)], [])
        self.assertFalse(any(can("viewer", p) for p in ["members.invite", "results.enter", "content.edit"]))
        self.assertEqual(assignable_roles("admin"), ["editor", "moderator", "viewer"])
        self.assertNotIn("owner", assignable_roles("owner"))

    def test_each_role_enforced_by_the_server(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        people = {}
        for role in ["organizer", "admin", "editor", "moderator", "viewer"]:
            people[role] = self.signup(role + "1")
            self.invite_and_join(owner, slug, people[role], role)
        for role, b in people.items():
            allowed = role in ("organizer", "admin")
            self.assertEqual(b.get(f"/api/orgs/{slug}/invitations").status_code, 200 if allowed else 403, role)
            self.assertEqual(b.call("post", f"/api/orgs/{slug}/invitations", {"role": "viewer"}).status_code, 200 if allowed else 403, role)
            self.assertEqual(b.call("patch", f"/api/orgs/{slug}", {"name": "X"}).status_code, 403, role)
            self.assertEqual(b.call("delete", f"/api/orgs/{slug}", {"confirm": "Kasoa Community League"}).status_code, 403, role)
            self.assertEqual(b.get(f"/api/orgs/{slug}/members").status_code, 200, role)
        # rank rules: an admin can't touch an organizer or promote anyone to admin+
        admin = people["admin"]
        self.assertEqual(admin.call("patch", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'organizer1')}", {"role": "viewer"}).status_code, 403)
        self.assertEqual(admin.call("patch", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'viewer1')}", {"role": "admin"}).status_code, 403)
        self.assertEqual(admin.call("patch", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'viewer1')}", {"role": "editor"}).status_code, 200)
        self.assertEqual(admin.call("post", f"/api/orgs/{slug}/invitations", {"role": "organizer"}).status_code, 403)
        # nobody but the owner can remove the owner, and the owner can't be demoted
        org1 = people["organizer"]
        self.assertEqual(org1.call("delete", f"/api/orgs/{slug}/members/{self.member_id(org1, slug, 'boss')}").status_code, 403)
        self.assertEqual(org1.call("delete", f"/api/orgs/{slug}/members/{self.member_id(org1, slug, 'admin1')}").status_code, 200)
        # emails are only shown to staff
        emails = {m["username"]: m["email"] for m in people["viewer"].get(f"/api/orgs/{slug}/members").json()["members"]}
        self.assertIsNone(emails["boss"])
        self.assertEqual(emails["viewer1"], "viewer1@example.com", "you can see your own")

    def test_settings_transfer_leave_delete(self):
        owner, ama = self.signup("boss"), self.signup("ama")
        slug = self.new_org(owner)
        self.invite_and_join(owner, slug, ama, "editor")
        self.assertEqual(owner.call("patch", f"/api/orgs/{slug}", {"name": "Kasoa League", "region": "Central"}).status_code, 200)
        self.assertEqual(owner.call("post", f"/api/orgs/{slug}/leave").status_code, 400, "owner must transfer first")
        self.assertEqual(owner.call("post", f"/api/orgs/{slug}/transfer", {"memberId": self.member_id(owner, slug, "ama")}).status_code, 200)
        self.assertEqual(Membership.objects.get(org__slug=slug, user__username="ama").role, "owner")
        self.assertEqual(Membership.objects.get(org__slug=slug, user__username="boss").role, "organizer")
        self.assertEqual(owner.call("delete", f"/api/orgs/{slug}", {"confirm": "Kasoa League"}).status_code, 403, "no longer the owner")
        self.assertEqual(owner.call("post", f"/api/orgs/{slug}/leave").status_code, 200)
        self.assertEqual(ama.call("delete", f"/api/orgs/{slug}", {"confirm": "wrong"}).status_code, 400)
        self.assertEqual(ama.call("delete", f"/api/orgs/{slug}", {"confirm": "Kasoa League"}).status_code, 200)
        self.assertFalse(Organization.objects.filter(slug=slug).exists())

    # ---------- invitations ----------
    def test_link_invitation_flow_and_single_use(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        r = owner.call("post", f"/api/orgs/{slug}/invitations", {"role": "editor"}).json()
        token = r["link"].rsplit("/", 1)[1]
        info = Browser().get(f"/api/invitations/{token}").json()
        self.assertEqual((info["org"]["name"], info["role"], info["status"]), ("Kasoa Community League", "editor", "pending"))
        guest = Browser()
        self.assertEqual(guest.call("post", f"/api/invitations/{token}/accept").status_code, 401, "must log in or sign up first")
        ama = self.signup("ama")
        self.assertEqual(ama.call("post", f"/api/invitations/{token}/accept").status_code, 200)
        self.assertEqual({o["slug"]: o["role"] for o in ama.me["orgs"]}, {slug: "editor"}, "dashboard shows the new org")
        yaw = self.signup("yaw")
        self.assertEqual(yaw.call("post", f"/api/invitations/{token}/accept").status_code, 410, "links work once")
        self.assertEqual(Browser().get("/api/invitations/not-a-real-token-at-all-xxxx").status_code, 404)

    def test_email_invitation_must_match_and_shows_on_dashboard(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        with mock.patch.dict("os.environ", {"EMAIL_PROVIDER": "console", "EMAIL_FROM": "league@example.com"}):
            r = owner.call("post", f"/api/orgs/{slug}/invitations", {"role": "moderator", "email": "Ama@Example.com"}).json()
        self.assertTrue(r["emailed"])
        token = r["link"].rsplit("/", 1)[1]
        self.assertEqual(owner.call("post", f"/api/orgs/{slug}/invitations", {"role": "viewer", "email": "ama@example.com"}).status_code, 409)
        wrong = self.signup("kofi")
        self.assertEqual(wrong.call("post", f"/api/invitations/{token}/accept").status_code, 403)
        ama = self.signup("ama", "ama@example.com")
        mine = ama.get("/api/me/invitations").json()["invitations"]
        self.assertEqual([(i["org"]["slug"], i["role"]) for i in mine], [(slug, "moderator")])
        self.assertEqual(ama.call("post", f"/api/me/invitations/{mine[0]['id']}/accept").status_code, 200)
        self.assertEqual(ama.get("/api/me/invitations").json()["invitations"], [])

    def test_invitation_states_revoke_expire_resend(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        r1 = owner.call("post", f"/api/orgs/{slug}/invitations", {"role": "viewer"}).json()
        r2 = owner.call("post", f"/api/orgs/{slug}/invitations", {"role": "viewer", "email": "late@example.com"}).json()
        owner.call("post", f"/api/orgs/{slug}/invitations/{r1['invitation']['id']}/revoke")
        Invitation.objects.filter(id=r2["invitation"]["id"]).update(expires=timezone.now() - timedelta(days=1))
        states = {i["id"]: i["status"] for i in owner.get(f"/api/orgs/{slug}/invitations").json()["invitations"]}
        self.assertEqual((states[r1["invitation"]["id"]], states[r2["invitation"]["id"]]), ("revoked", "expired"))
        late = self.signup("late", "late@example.com")
        self.assertEqual(late.call("post", f"/api/invitations/{r2['link'].rsplit('/', 1)[1]}/accept").status_code, 410)
        self.assertEqual(late.call("post", f"/api/invitations/{r1['link'].rsplit('/', 1)[1]}/accept").status_code, 410)
        again = owner.call("post", f"/api/orgs/{slug}/invitations/{r2['invitation']['id']}/resend").json()
        self.assertEqual(again["invitation"]["status"], "pending")
        self.assertEqual(late.call("post", f"/api/invitations/{r2['link'].rsplit('/', 1)[1]}/accept").status_code, 404, "old link is dead")
        self.assertEqual(late.call("post", f"/api/invitations/{again['link'].rsplit('/', 1)[1]}/accept").status_code, 200)

    def test_pages_are_served(self):
        for url in ["/app", "/app/login", "/app/org/anything/members", "/invite/abc"]:
            r = Client().get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("script-src 'nonce-", r["Content-Security-Policy"])
        self.assertEqual(Client().get("/").status_code, 200, "the original league page still works")
        self.assertEqual(Admin.objects.count(), 0)
