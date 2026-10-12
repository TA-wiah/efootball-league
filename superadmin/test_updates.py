"""What's new: public updates for everyone, the rest only for the roles they're for; super admins write them."""
from django.test import Client, TestCase

from league.models import Admin
from orgs.tests import Helpers

from .models import PlatformUpdate


class UpdatesTest(Helpers, TestCase):
    def setUp(self):
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        self.pl = self.signup("ama")
        self.invite_and_join(self.boss, self.slug, self.pl, "player")

    def titles(self, who):
        return {u["title"] for u in who.get("/api/updates").json()["updates"]}

    def test_who_sees_what(self):
        public = {u.title for u in PlatformUpdate.objects.all() if u.audience == ["public"]}
        self.assertTrue(public, "the first updates are there after migrating")
        self.assertEqual(self.titles(Client()), public, "visitors: public only")
        self.assertEqual(self.titles(self.pl), public, "players: public only")
        owner = self.titles(self.boss)
        self.assertIn("Results sent by teams, approved by you", owner, "owners also see the organizers' updates")
        self.assertNotIn("AI for lineup screenshots: OpenAI or Google Gemini", owner)
        self.assertIn("AI for lineup screenshots: OpenAI or Google Gemini", self.titles(self.root))
        page = Client().get("/updates").content.decode()
        self.assertIn("Fill your squad from screenshots", page)
        self.assertNotIn("Results sent by teams, approved by you", page, "role-only updates never go public")
        self.assertIn('aria-label="Updates"', Client().get("/competitions").content.decode(), "in the top bar")

    def test_super_admins_write_them(self):
        self.assertEqual(self.boss.call("post", "/api/admin/updates", {"title": "Hi", "audience": ["public"]}).status_code, 403)
        r = self.root.call("post", "/api/admin/updates", {"title": "Coaches can now do X", "body": "Intro\n- one\n- two", "audience": ["coach", "team_manager"]})
        self.assertEqual(r.status_code, 200, r.json())
        u = r.json()["update"]
        self.assertEqual(u["blocks"], [{"type": "p", "text": "Intro"}, {"type": "list", "items": ["one", "two"]}])
        self.assertNotIn("Coaches can now do X", self.titles(self.pl))
        coach = self.signup("kojo")
        self.invite_and_join(self.boss, self.slug, coach, "coach")
        self.assertIn("Coaches can now do X", self.titles(coach))
        self.assertEqual(self.root.call("post", "/api/admin/updates", {"title": "Bad", "audience": ["nobody"]}).status_code, 400)
        self.root.call("patch", f"/api/admin/updates/{u['id']}", {"audience": ["public", "coach"]})
        self.assertEqual(PlatformUpdate.objects.get(id=u["id"]).audience, ["public"], "everyone means everyone")
        self.assertIn("Coaches can now do X", Client().get("/updates").content.decode())
        self.root.call("patch", f"/api/admin/updates/{u['id']}", {"published": False})
        self.assertNotIn("Coaches can now do X", self.titles(Client()), "hidden updates don't show")
        self.assertEqual(self.root.call("delete", f"/api/admin/updates/{u['id']}").status_code, 200)
        self.assertFalse(PlatformUpdate.objects.filter(id=u["id"]).exists())
