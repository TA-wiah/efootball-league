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


# the collection (players list) screens print names; their ratings are a few points lower than on the lineup screen
COLLECTION = {"screen": "collection", "players": [
    {"position": "GK", "rating": 94, "starter": False, "name": "Gianluigi Donnarumma"}, {"position": "CB", "rating": 95, "starter": False, "name": "Nico Schlotterbeck"},
    {"position": "CB", "rating": 95, "starter": False, "name": "Willian Pacho"}, {"position": "CB", "rating": 96, "starter": False, "name": "Josko Gvardiol"},
    {"position": "RB", "rating": 95, "starter": False, "name": "T. Alexander-Arnold"}, {"position": "SS", "rating": 100, "starter": False, "name": "Lionel Messi"},
    {"position": "AMF", "rating": 99, "starter": False, "name": "Dennis Bergkamp"}, {"position": "CMF", "rating": 96, "starter": False, "name": "Declan Rice"},
    {"position": "LWF", "rating": 97, "starter": False, "name": "Marco Reus"}]}


class LineupTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner)
        self.base = f"/api/orgs/{self.slug}"
        self.tid = self.owner.call("post", self.base + "/teams", {"name": "Lions"}).json()["team"]["id"]
        self.scan = f"{self.base}/teams/{self.tid}/lineup-scan"
        self.save = f"{self.base}/teams/{self.tid}/lineup"
        self.merge = f"{self.base}/teams/{self.tid}/lineup-merge"

    def read(self, *shots):
        """Each screenshot read on its own (as the page does), then put together."""
        reads = []
        for shot in shots:
            with mock.patch.object(lineup, "read_image", lambda cfg, image, shot=shot: shot):
                r = self.owner.call("post", self.scan, {"image": IMG})
            self.assertEqual(r.status_code, 200, r.json())
            reads.append(r.json())
        return self.owner.call("post", self.merge, {"reads": reads}).json()

    def test_screenshot_to_squad(self):
        self.assertFalse(self.owner.get(self.scan).json()["enabled"], "off until a super admin turns AI on")
        self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 400)
        store.update("ai", {"enabled": True, "api_key": "sk-test", "daily_limit": 3})
        self.assertEqual(self.owner.call("post", self.scan, {"image": "not an image"}).status_code, 400)
        r = self.read(READ)
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
        again = self.read(READ)["players"]
        self.assertEqual(again[0]["name"], "Gigi Keeper")
        self.assertEqual(again[3]["name"], "Ade Wall", "the only CB rated 100")
        self.assertEqual(again[1]["name"], "", "two CBs rated 99: left for people to choose")
        again[1]["name"], again[2]["name"] = "Cole Centre", "Rio Back"
        res = self.owner.call("post", self.save, {"players": again}).json()
        self.assertEqual(res["added"], 0, "everyone named is already in the squad")
        self.assertEqual(Team.objects.get(id=self.tid).players.count(), 13, "no duplicates")
        # the daily limit: each use can be up to 10 screenshots
        store.update("ai", {"daily_limit": 1})
        self.assertEqual(self.owner.get(self.scan).json()["left"], 8)
        with mock.patch.object(lineup, "read_image", lambda cfg, image: READ):
            for _ in range(8):
                self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 200)
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 429)

    def test_names_from_the_players_list_screenshots(self):
        store.update("ai", {"enabled": True, "vision": "gemini", "gemini_key": "AIza-test", "daily_limit": 3})
        self.assertTrue(self.owner.get(self.scan).json()["enabled"], "Gemini works without an OpenAI key")
        r = self.read(READ, COLLECTION, COLLECTION)            # the same players list twice: no duplicates
        self.assertEqual(r["lineups"], 1)
        rows = r["players"]
        got = [(x["position"], x["rating"], x["name"]) for x in rows]
        self.assertEqual(got[0], ("GK", 97, "Gianluigi Donnarumma"), "97 on the lineup, 94 in the list: the team boost")
        self.assertEqual(got[3], ("CB", 100, "Josko Gvardiol"), "the only CB that fits 100")
        self.assertEqual(got[4], ("RB", 99, "T. Alexander-Arnold"))
        self.assertEqual(got[10], ("SS", 104, "Lionel Messi"))
        self.assertEqual(got[7], ("AMF", 103, "Dennis Bergkamp"))
        self.assertEqual((got[1][2], got[2][2]), ("", ""), "two CBs at 99 and two possible cards: left for people to choose")
        self.assertTrue(rows[0]["fromShot"])
        self.assertIn("Marco Reus", [x["name"] for x in r["extras"]], "named players that aren't in the lineup")
        self.assertNotIn("Lionel Messi", [x["name"] for x in r["extras"]])
        only_list = self.read(COLLECTION)                       # no lineup: the players list becomes the squad list
        self.assertEqual(len(only_list["players"]), 9)
        self.assertFalse(any(x["starter"] for x in only_list["players"]))
        self.assertEqual(self.owner.call("post", self.merge, {"reads": [READ] * 11}).status_code, 400, "up to 10")
        from superadmin.store import masked
        self.assertFalse(any("AIza" in str(v) for v in masked("ai").values()), "the Gemini key never goes to browsers")

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
