"""eFootball lineup screenshots → squad (the AI call is replaced by a fake that answers like the real one), saving
the reviewed lineup, and the "complete your squad" reminders."""
from unittest import mock

from django.test import TestCase

from league import emailer
from orgs.models import Membership
from orgs.tests import Helpers
from superadmin import store

from . import lineup, reminders
from .models import Player, Team

IMG = "data:image/jpeg;base64,/9j/AAAA"
# what the AI reads from a screenshot like the one in the conversation: 11 on the pitch, subs beside it, no names
READ = {"formation": "4-2-1-3", "players": [
    {"position": "GK", "rating": 97, "starter": True, "name": ""}, {"position": "CB", "rating": 99, "starter": True, "name": ""},
    {"position": "CB", "rating": 99, "starter": True, "name": ""}, {"position": "CB", "rating": 100, "starter": True, "name": ""},
    {"position": "RB", "rating": 99, "starter": True, "name": ""}, {"position": "CMF", "rating": 100, "starter": True, "name": ""},
    {"position": "CMF", "rating": 99, "starter": True, "name": ""}, {"position": "AMF", "rating": 103, "starter": True, "name": ""},
    {"position": "CF", "rating": 102, "starter": True, "name": ""}, {"position": "CF", "rating": 102, "starter": True, "name": ""},
    {"position": "SS", "rating": 104, "starter": True, "name": ""},
    {"position": "AMF", "rating": 99, "starter": True, "name": ""},          # a 12th "starter": becomes a sub
    {"position": "XYZ", "rating": 99, "starter": False, "name": ""},         # not a position: dropped
    {"position": "LWF", "rating": 999, "starter": False, "name": "Printed Name"}]}


class LineupTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner)
        self.base = f"/api/orgs/{self.slug}"
        self.tid = self.owner.call("post", self.base + "/teams", {"name": "Lions"}).json()["team"]["id"]
        self.scan = f"{self.base}/teams/{self.tid}/lineup-scan"
        self.save = f"{self.base}/teams/{self.tid}/lineup"

    def test_screenshot_to_squad(self):
        self.assertFalse(self.owner.get(self.scan).json()["enabled"], "off until a super admin turns AI on")
        self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 400)
        store.update("ai", {"enabled": True, "api_key": "sk-test", "daily_limit": 3})
        self.assertEqual(self.owner.call("post", self.scan, {"image": "not an image"}).status_code, 400)
        with mock.patch.object(lineup, "read_image", lambda cfg, image: READ):
            r = self.owner.call("post", self.scan, {"image": IMG}).json()
        rows = r["players"]
        self.assertEqual(r["formation"], "4-2-1-3")
        self.assertEqual(len(rows), 13, "the unknown position is dropped")
        self.assertEqual(sum(x["starter"] for x in rows), 11, "never more than 11 starters")
        self.assertEqual(rows[0]["position"], "GK")
        self.assertIsNone(rows[-1]["rating"], "impossible ratings are left out")
        self.assertEqual(rows[-1]["name"], "Printed Name", "names are kept when the screen prints them")
        # the people fill in the names, then save
        names = ["Gigi Keeper", "Cole Centre", "Rio Back", "Ade Wall", "Trent Right", "Dec Mid", "Enzo Mid", "Frank Ten", "Pierre Nine", "Alex Nine", "Leo Ten", "Jude Sub"]
        for x, n in zip(rows, names):
            x["name"] = n
        res = self.owner.call("post", self.save, {"players": rows}).json()
        self.assertEqual((res["added"], res["updated"]), (13, 0), "the printed name is kept and added")
        squad = {p.name: p for p in Team.objects.get(id=self.tid).players.all()}
        self.assertEqual((squad["Gigi Keeper"].position, squad["Gigi Keeper"].rating, squad["Gigi Keeper"].lineup), ("GK", 97, "starter"))
        self.assertEqual(squad["Jude Sub"].lineup, "sub")
        # next week's screenshot: returning players are recognised by position and rating
        with mock.patch.object(lineup, "read_image", lambda cfg, image: READ):
            again = self.owner.call("post", self.scan, {"image": IMG}).json()["players"]
        self.assertEqual(again[0]["name"], "Gigi Keeper")
        self.assertEqual(again[3]["name"], "Ade Wall", "the only CB rated 100")
        self.assertEqual(again[1]["name"], "", "two CBs rated 99: left for people to choose")
        again[1]["name"], again[2]["name"] = "Cole Centre", "Rio Back"
        res = self.owner.call("post", self.save, {"players": again}).json()
        self.assertEqual(res["added"], 0, "everyone named is already in the squad")
        self.assertEqual(Team.objects.get(id=self.tid).players.count(), 13, "no duplicates")
        # the daily limit
        with mock.patch.object(lineup, "read_image", lambda cfg, image: READ):
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 200, "the third of three today")
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 429)

    def test_who_can_save_and_checks(self):
        pl = self.signup("ama")
        self.invite_and_join(self.owner, self.slug, pl, "player")
        rows = [{"name": "Ama Owusu", "position": "CMF", "rating": 95, "starter": True}]
        self.assertEqual(pl.call("post", self.save, {"players": rows}).status_code, 403, "not on this team")
        Membership.objects.get(user__username="ama").teams.add(Team.objects.get(id=self.tid))
        self.assertEqual(pl.call("post", self.save, {"players": rows}).status_code, 200, "players of the team can")
        self.assertEqual(pl.get(self.scan).status_code, 200)
        self.assertEqual(self.owner.call("post", self.save, {"players": [{"name": "X", "position": "QB"}]}).status_code, 400)
        self.assertEqual(self.owner.call("post", self.save, {"players": [{"name": "X", "position": "CB", "rating": 500}]}).status_code, 400)
        twelve = [{"name": f"P{i}", "position": "CB", "starter": True} for i in range(12)]
        self.assertEqual(self.owner.call("post", self.save, {"players": twelve}).status_code, 400, "only 11 can start")
        self.assertEqual(Player.objects.get(name="Ama Owusu").lineup, "starter")

    def test_complete_your_squad_reminders(self):
        from .models import Competition
        cs = self.owner.call("post", self.base + "/competitions", {"name": "Cup"}).json()["competition"]["slug"]
        other = self.owner.call("post", self.base + "/teams", {"name": "Tigers"}).json()["team"]["id"]
        self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": [self.tid, other]})
        Competition.objects.filter(slug=cs).update(status="active")
        pl = self.signup("kojo", "kojo@example.com")
        self.invite_and_join(self.owner, self.slug, pl, "player")
        Membership.objects.get(user__username="kojo").teams.add(Team.objects.get(id=self.tid))
        todo = self.owner.get(self.base + "/summary").json()["squadTodo"]
        self.assertEqual({t["name"]: t["missing"] for t in todo}, {"Lions": "no goalkeeper listed", "Tigers": "no goalkeeper listed"})
        self.assertEqual([t["name"] for t in pl.get(self.base + "/summary").json()["squadTodo"]], ["Lions"], "players see their own team")
        mails = []
        with mock.patch.object(emailer, "ready", lambda cfg=None: True), mock.patch.object(emailer, "send", lambda to, subj, t, h: mails.append((to, subj))):
            self.assertEqual(reminders.send_squad_due(force=True), 2)
            self.assertEqual(reminders.send_squad_due(force=True), 0, "at most once a week")
        self.assertIn(("kojo@example.com", "Complete Lions's squad"), mails)
        rows = [{"name": f"P{i}", "position": "GK" if i == 0 else "CB", "starter": True} for i in range(11)]
        self.owner.call("post", self.save, {"players": rows})
        self.assertEqual([t["name"] for t in self.owner.get(self.base + "/summary").json()["squadTodo"]], ["Tigers"], "Lions is complete now")
