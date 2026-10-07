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


class Helpers:
    """Shared by the platform and competition tests."""

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


class PlatformTest(Helpers, TestCase):
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
        self.assertEqual(assignable_roles("admin"), ["organizer", "team_manager", "coach", "scorekeeper", "editor", "moderator", "player", "viewer"])
        self.assertEqual(assignable_roles("organizer"), ["team_manager", "coach", "scorekeeper", "editor", "moderator", "player", "viewer"])
        self.assertTrue(can("scorekeeper", "results.enter") and not can("scorekeeper", "teams.manage"))
        self.assertTrue(can("coach", "teams.manage_assigned") and not can("coach", "teams.manage"))
        self.assertTrue(can("admin", "org.settings") and not can("organizer", "org.settings"))
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
            self.assertEqual(b.call("patch", f"/api/orgs/{slug}", {"region": "X"}).status_code, 200 if role == "admin" else 403, role)
            self.assertEqual(b.call("delete", f"/api/orgs/{slug}", {"confirm": "Kasoa Community League"}).status_code, 403, role)
            self.assertEqual(b.get(f"/api/orgs/{slug}/members").status_code, 200, role)
        # rank rules: an admin manages everyone below them but can't create another admin;
        # a competition manager invites staff but can't change members
        admin, org1 = people["admin"], people["organizer"]
        self.assertEqual(admin.call("patch", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'viewer1')}", {"role": "admin"}).status_code, 403)
        self.assertEqual(admin.call("patch", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'viewer1')}", {"role": "editor"}).status_code, 200)
        self.assertEqual(admin.call("post", f"/api/orgs/{slug}/invitations", {"role": "admin"}).status_code, 403)
        self.assertEqual(admin.call("post", f"/api/orgs/{slug}/invitations", {"role": "organizer"}).status_code, 200)
        self.assertEqual(org1.call("post", f"/api/orgs/{slug}/invitations", {"role": "organizer"}).status_code, 403)
        self.assertEqual(org1.call("post", f"/api/orgs/{slug}/invitations", {"role": "coach"}).status_code, 200)
        self.assertEqual(org1.call("patch", f"/api/orgs/{slug}/members/{self.member_id(org1, slug, 'viewer1')}", {"role": "player"}).status_code, 403)
        # nobody but the owner can remove the owner
        self.assertEqual(admin.call("delete", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'boss')}").status_code, 403)
        self.assertEqual(admin.call("delete", f"/api/orgs/{slug}/members/{self.member_id(admin, slug, 'organizer1')}").status_code, 200)
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
        self.assertEqual(Membership.objects.get(org__slug=slug, user__username="boss").role, "admin")
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


class PortalTest(Helpers, TestCase):
    """The organization portal: cards, setup wizard fields, branding, custom permissions, team access, public website."""

    def test_cards_wizard_fields_and_slug_check(self):
        boss = self.signup("boss")
        self.assertEqual(boss.get("/api/org-slug?slug=kasoa-fc").json()["available"], True)
        r = boss.call("post", "/api/orgs", {"name": "Kasoa FC", "kind": "club", "slug": "kasoa-fc", "brandColor": "#2F7BFF",
                                            "timezone": "Africa/Accra", "website": {"tagline": "Up the Kasoa", "show_players": False}})
        self.assertEqual(r.status_code, 200, r.json())
        org = r.json()["org"]
        self.assertEqual((org["slug"], org["kind"], org["brandColor"], org["timezone"]), ("kasoa-fc", "club", "#2f7bff", "Africa/Accra"))
        self.assertFalse(org["website"]["show_players"])
        self.assertTrue(org["website"]["show_teams"], "defaults fill the rest")
        self.assertEqual(boss.get("/api/org-slug?slug=kasoa-fc").json()["available"], False)
        self.assertEqual(boss.call("post", "/api/orgs", {"name": "Other", "slug": "kasoa-fc"}).status_code, 409)
        for bad in [{"slug": "Bad Slug!"}, {"brandColor": "red; x:y"}, {"timezone": "Mars/Base"}, {"website": {"script": "x"}},
                    {"website": {"show_teams": "yes"}}]:
            self.assertEqual(boss.call("post", "/api/orgs", {"name": "Other", **bad}).status_code, 400, bad)
        self.assertEqual(Browser().get("/api/org-slug?slug=x").status_code, 401)
        card = boss.get("/api/auth/me").json()["orgs"][0]
        for k in ("competitions", "teams", "players", "members", "staff", "lastActivity", "perms", "kindLabel"):
            self.assertIn(k, card)
        self.assertEqual((card["members"], card["kindLabel"]), (1, "Club"))

    def test_owner_configures_permissions(self):
        boss, sk, ed = self.signup("boss"), self.signup("skeeper"), self.signup("editor7")
        slug = self.new_org(boss)
        self.invite_and_join(boss, slug, sk, "scorekeeper")
        self.invite_and_join(boss, slug, ed, "editor")
        self.assertEqual(sk.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).status_code, 403)
        self.assertEqual(sk.call("patch", f"/api/orgs/{slug}/permissions", {"permissions": {"teams.manage": ["scorekeeper"]}}).status_code, 403)
        r = boss.call("patch", f"/api/orgs/{slug}/permissions", {"permissions": {"teams.manage": ["scorekeeper"]}})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(sk.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).status_code, 200, "granted by the owner")
        self.assertEqual(ed.call("post", f"/api/orgs/{slug}/teams", {"name": "Tigers"}).status_code, 403, "and taken from editors")
        for bad in [{"permissions": {"org.delete": ["admin"]}}, {"permissions": {"teams.manage": ["king"]}}, {"permissions": "x"}]:
            self.assertEqual(boss.call("patch", f"/api/orgs/{slug}/permissions", bad).status_code, 400, bad)
        boss.call("patch", f"/api/orgs/{slug}/permissions", {"permissions": {"teams.manage": []}})
        self.assertEqual(boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Owner FC"}).status_code, 200, "owner always can")
        boss.call("patch", f"/api/orgs/{slug}/permissions", {"reset": True})
        self.assertEqual(ed.call("post", f"/api/orgs/{slug}/teams", {"name": "Tigers"}).status_code, 200, "back to defaults")
        self.assertIn("teams.manage", ed.get(f"/api/orgs/{slug}").json()["me"]["perms"])

    def test_team_managers_only_touch_their_teams(self):
        boss, tm = self.signup("boss"), self.signup("tmanager")
        slug = self.new_org(boss)
        lions = boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        tigers = boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Tigers"}).json()["team"]["id"]
        other = self.signup("xavier")
        oslug = self.new_org(other, "Other League")
        foreign = other.call("post", f"/api/orgs/{oslug}/teams", {"name": "Foreign"}).json()["team"]["id"]
        self.invite_and_join(boss, slug, tm, "team_manager")
        mid = self.member_id(boss, slug, "tmanager")
        self.assertEqual(boss.call("patch", f"/api/orgs/{slug}/members/{mid}", {"teams": [foreign]}).status_code, 400)
        self.assertEqual(tm.call("patch", f"/api/orgs/{slug}/members/{mid}", {"teams": [lions, tigers]}).status_code, 403)
        self.assertEqual(boss.call("patch", f"/api/orgs/{slug}/members/{mid}", {"teams": [lions]}).status_code, 200)
        self.assertEqual(tm.call("patch", f"/api/orgs/{slug}/teams/{lions}", {"city": "Kasoa"}).status_code, 200)
        self.assertEqual(tm.call("patch", f"/api/orgs/{slug}/teams/{tigers}", {"city": "Kasoa"}).status_code, 403)
        r = tm.call("post", f"/api/orgs/{slug}/teams/{lions}/players", {"name": "Kofi", "number": 9})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(tm.call("post", f"/api/orgs/{slug}/teams/{tigers}/players", {"name": "Ama"}).status_code, 403)
        pid = boss.call("post", f"/api/orgs/{slug}/teams/{tigers}/players", {"name": "Ama"}).json()["player"]["id"]
        self.assertEqual(tm.call("delete", f"/api/orgs/{slug}/players/{pid}").status_code, 403)
        self.assertEqual(tm.call("delete", f"/api/orgs/{slug}/players/{r.json()['player']['id']}").status_code, 200)
        self.assertEqual(tm.call("delete", f"/api/orgs/{slug}/teams/{lions}").status_code, 403, "can't delete teams")
        self.assertEqual(tm.call("post", f"/api/orgs/{slug}/teams", {"name": "New"}).status_code, 403, "or create them")
        self.assertEqual(tm.get(f"/api/orgs/{slug}").json()["me"]["teams"], [lions])

    def test_branding_logo_and_public_website(self):
        boss, ed = self.signup("boss"), self.signup("editor7")
        slug = self.new_org(boss)
        self.invite_and_join(boss, slug, ed, "editor")
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        self.assertEqual(ed.call("post", f"/api/orgs/{slug}/logo", {"image": png}).status_code, 403)
        r = boss.call("post", f"/api/orgs/{slug}/logo", {"image": png})
        self.assertEqual(r.status_code, 200, r.json())
        logo = Client().get(r.json()["logo"])
        self.assertEqual((logo.status_code, logo["Content-Type"]), (200, "image/png"))
        self.assertEqual(Client().get("/media/org/999/logo").status_code, 404)
        self.assertEqual(boss.call("patch", f"/api/orgs/{slug}", {"brandColor": "#123", "website": {}}).status_code, 400)
        self.assertEqual(boss.call("patch", f"/api/orgs/{slug}", {"brandColor": "#c8102e", "website": {"tagline": "<b>Hi</b>", "show_teams": False}}).status_code, 200)
        boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Secret Lions"})
        page = Client().get(f"/org/{slug}")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("&lt;b&gt;Hi&lt;/b&gt;", html, "escaped")
        self.assertIn("#c8102e", html)
        self.assertNotIn("Secret Lions", html, "teams hidden by the website settings")
        self.assertEqual(Client().get("/org/nope").status_code, 404)
        self.assertIn("public", page["Cache-Control"], "visitors get a short cache")
        self.assertIn("no-store", boss.get(f"/org/{slug}")["Cache-Control"], "organizers always see their latest changes")
        old = Client().get(f"/organization/{slug}")
        self.assertEqual((old.status_code, old["Location"]), (301, f"/org/{slug}"), "old links keep working")

    def test_team_manager_invites_people_to_their_team(self):
        boss, tm = self.signup("boss"), self.signup("tmanager")
        slug = self.new_org(boss)
        lions = boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        tigers = boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Tigers"}).json()["team"]["id"]
        self.invite_and_join(boss, slug, tm, "team_manager")
        boss.call("patch", f"/api/orgs/{slug}/members/{self.member_id(boss, slug, 'tmanager')}", {"teams": [lions]})
        url = f"/api/orgs/{slug}/teams/{lions}/invitations"
        self.assertEqual(tm.get(f"/api/orgs/{slug}/teams/{lions}").json()["team"]["inviteRoles"], ["coach", "player"])
        self.assertEqual(tm.call("post", f"/api/orgs/{slug}/teams/{tigers}/invitations", {"role": "player"}).status_code, 403, "not their team")
        self.assertEqual(tm.call("post", url, {"role": "team_manager"}).status_code, 403, "only coaches and players")
        self.assertEqual(tm.call("post", f"/api/orgs/{slug}/invitations", {"role": "player"}).status_code, 403, "no org-wide invitations")
        r = tm.call("post", url, {"role": "player"})
        self.assertEqual(r.status_code, 200, r.json())
        kofi = self.signup("kofi9")
        self.assertEqual(kofi.call("post", f"/api/invitations/{r.json()['link'].rsplit('/', 1)[1]}/accept").status_code, 200)
        m = Membership.objects.get(org__slug=slug, user__username="kofi9")
        self.assertEqual((m.role, list(m.teams.values_list("id", flat=True))), ("player", [lions]), "joins the team")
        team = tm.get(f"/api/orgs/{slug}/teams/{lions}").json()["team"]
        self.assertIn("kofi9", [x["username"] for x in team["members"]])
        self.assertEqual(len(tm.get(url).json()["invitations"]), 1)
        # revoke their own team's invitations, but not others'
        inv2 = tm.call("post", url, {"role": "coach"}).json()["invitation"]["id"]
        other = boss.call("post", f"/api/orgs/{slug}/invitations", {"role": "player"}).json()["invitation"]["id"]
        self.assertEqual(tm.call("post", f"/api/orgs/{slug}/invitations/{inv2}/revoke").status_code, 200)
        self.assertEqual(tm.call("post", f"/api/orgs/{slug}/invitations/{other}/revoke").status_code, 403)
        # the owner can switch it off
        boss.call("patch", f"/api/orgs/{slug}/permissions", {"permissions": {"team.invite": []}})
        self.assertEqual(tm.call("post", url, {"role": "player"}).status_code, 403)
        # an admin can invite into any team, including team managers
        self.assertEqual(boss.call("post", f"/api/orgs/{slug}/teams/{tigers}/invitations", {"role": "team_manager"}).status_code, 200)

    def test_who_can_invite_which_roles(self):
        boss = self.signup("boss")
        slug = self.new_org(boss)
        expect = {"admin": ["organizer", "team_manager", "coach", "scorekeeper", "editor", "moderator", "player", "viewer"],
                  "organizer": ["team_manager", "coach", "scorekeeper", "editor", "moderator", "player", "viewer"]}
        for role in ["admin", "organizer", "team_manager", "coach", "scorekeeper", "editor", "moderator", "player", "viewer"]:
            who = self.signup("u_" + role)
            self.invite_and_join(boss, slug, who, role)
            self.assertEqual(Membership.objects.get(org__slug=slug, user__username="u_" + role).role, role, "joins with the invited role")
            allowed = expect.get(role, [])
            for target in ["owner", "admin", "organizer", "team_manager", "player", "viewer"]:
                r = who.call("post", f"/api/orgs/{slug}/invitations", {"role": target})
                self.assertEqual(r.status_code == 200, target in allowed, (role, target, r.status_code))

    def test_invite_into_a_team_from_members_page_and_existing_members(self):
        boss, ama = self.signup("boss"), self.signup("ama7")
        slug = self.new_org(boss)
        lions = boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        tigers = boss.call("post", f"/api/orgs/{slug}/teams", {"name": "Tigers"}).json()["team"]["id"]
        url = f"/api/orgs/{slug}/invitations"
        self.assertEqual(boss.call("post", url, {"role": "scorekeeper", "teamId": lions}).status_code, 400, "not a team role")
        self.assertEqual(boss.call("post", url, {"role": "player", "teamId": 99999}).status_code, 404)
        link = boss.call("post", url, {"role": "team_manager", "teamId": lions}).json()["link"]
        token = link.rsplit("/", 1)[1]
        self.assertEqual(ama.get(f"/api/invitations/{token}").json()["teams"], ["Lions"])
        self.assertEqual(ama.call("post", f"/api/invitations/{token}/accept").status_code, 200)
        m = Membership.objects.get(org__slug=slug, user__username="ama7")
        self.assertEqual((m.role, list(m.teams.values_list("id", flat=True))), ("team_manager", [lions]))
        # already a member: an org-wide invitation is refused, a team invitation adds the team (role never goes down)
        self.assertEqual(boss.call("post", url, {"role": "player", "email": "ama7@example.com"}).status_code, 409)
        r = boss.call("post", f"/api/orgs/{slug}/teams/{tigers}/invitations", {"role": "player", "email": "ama7@example.com"})
        self.assertEqual(r.status_code, 200, r.json())
        mine = ama.get("/api/me/invitations").json()["invitations"]
        self.assertEqual([i["teams"] for i in mine], [["Tigers"]], "shows up for an existing member")
        self.assertEqual(ama.call("post", f"/api/me/invitations/{mine[0]['id']}/accept").status_code, 200)
        m.refresh_from_db()
        self.assertEqual((m.role, sorted(m.teams.values_list("id", flat=True))), ("team_manager", sorted([lions, tigers])))
        again = boss.call("post", f"/api/orgs/{slug}/teams/{tigers}/invitations", {"role": "player"}).json()["link"].rsplit("/", 1)[1]
        self.assertEqual(ama.call("post", f"/api/invitations/{again}/accept").status_code, 409, "already in that team")

    def test_custom_website_pages(self):
        boss, viewer = self.signup("boss"), self.signup("viewer1")
        slug = self.new_org(boss)
        self.invite_and_join(boss, slug, viewer, "viewer")
        url = f"/api/orgs/{slug}/pages"
        self.assertEqual(viewer.call("post", url, {"title": "Rules"}).status_code, 403)
        self.assertEqual(boss.call("post", url, {"title": "x"}).status_code, 400)
        body = ("## Match rules\nEach game lasts 10 minutes.\n\n- Be on time\n- Respect the referee\n\n"
                "Questions? See https://example.com/faq.\n<script>alert(1)</script> <b>bold</b> [x](javascript:alert(2))")
        pg = boss.call("post", url, {"title": "Rules & how to join", "body": body}).json()["page"]
        self.assertEqual(pg["url"], f"/org/{slug}/p/rules-how-to-join")
        html = Client().get(pg["url"]).content.decode()
        self.assertIn("<h2>Match rules</h2>", html)
        self.assertIn("<li>Respect the referee</li>", html)
        self.assertIn('<a href="https://example.com/faq" rel="nofollow noopener noreferrer" target="_blank">https://example.com/faq</a>.', html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &lt;b&gt;bold&lt;/b&gt;", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn(f'href="/org/{slug}/p/rules-how-to-join"', Client().get(f"/org/{slug}").content.decode(), "listed in the site menu")
        # drafts stay hidden from visitors
        boss.call("patch", f"{url}/{pg['id']}", {"published": False})
        self.assertEqual(Client().get(pg["url"]).status_code, 404)
        self.assertEqual(boss.get(pg["url"]).status_code, 200, "members can preview")
        self.assertEqual(boss.call("delete", f"{url}/{pg['id']}").status_code, 200)
        self.assertEqual(boss.get(pg["url"]).status_code, 404)
