"""eFootball lineup screenshots → squad (the AI call is replaced by a fake that answers like the real one), saving
the reviewed lineup, and the "complete your squad" reminders."""
from unittest import mock

from django.test import TestCase

from league import emailer
from league.http import ApiError
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
        self.assertEqual(self.owner.get(self.scan).json()["limit"], 10, "one upload a day by default: up to 10 screenshots")
        self.assertEqual(self.owner.get(self.scan).json()["left"], 8)
        with mock.patch.object(lineup, "read_image", lambda cfg, image: READ):
            for _ in range(8):
                self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 200)
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 429)
        store.update("ai", {"lineup_per_day": 2})
        self.assertEqual(self.owner.get(self.scan).json()["left"], 10, "a second upload when the platform allows two")

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


class GeminiKeysTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        slug = self.new_org(self.owner)
        tid = self.owner.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        self.scan = f"/api/orgs/{slug}/teams/{tid}/lineup-scan"
        store.update("ai", {"enabled": True, "vision": "gemini", "gemini_key": "k1", "gemini_key2": "k2", "gemini_key3": "k3", "daily_limit": 5})

    def test_keys_take_turns_and_a_busy_key_hands_over(self):
        used = []

        def fake(cfg, key, image):
            used.append(key)
            if key == "k1":
                raise lineup.Busy("429")
            return READ
        lineup._turn["n"] = 0
        with mock.patch.object(lineup, "read_with", fake):
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 200)
            self.assertEqual(used, ["k1", "k2"], "k1 busy: k2 took over")
            used.clear()
            self.owner.call("post", self.scan, {"image": IMG})
            self.assertEqual(used, ["k2"], "the next request starts with the next key")
        with mock.patch.object(lineup, "read_with", lambda cfg, key, image: (_ for _ in ()).throw(lineup.Busy("429"))):
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 429, "all keys busy")
        from superadmin.store import masked
        m = masked("ai")
        self.assertTrue(m["gemini_key2_set"] and not m["gemini_key4_set"])
        self.assertFalse(any(v in ("k1", "k2", "k3") for v in m.values()), "keys never go to browsers")

    def test_platform_cap(self):
        store.update("ai", {"vision_daily_total": 2})
        with mock.patch.object(lineup, "read_with", lambda cfg, key, image: READ):
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 200)
            self.assertEqual(self.owner.call("post", self.scan, {"image": IMG}).status_code, 200)
            r = self.owner.call("post", self.scan, {"image": IMG})
        self.assertEqual(r.status_code, 429)
        self.assertIn("shared by everyone", r.json()["error"])


class MergeTest(TestCase):
    def test_pairing_one_card_can_settle_another(self):
        """From real screenshots: AMF 103 (starter) and AMF 99 (sub) on the lineup; Bergkamp AMF 97 and Fermín López
        AMF 95 in the players list. The usual boost (+4) pairs López with 99, which leaves only Bergkamp for 103."""
        p = lambda pos, r, name="", st=False: {"position": pos, "rating": r, "name": name, "starter": st}
        lineup_shot = {"screen": "lineup", "formation": "", "players": [p("GK", 97, st=True), p("SS", 104, st=True), p("CMF", 100, st=True),
                                                                         p("AMF", 103, st=True), p("AMF", 99)]}
        cards = {"screen": "collection", "formation": "", "players": [p("GK", 94, "Gianluigi Donnarumma"), p("SS", 100, "Lionel Messi"),
                                                                       p("CMF", 96, "Declan Rice"), p("AMF", 97, "Dennis Bergkamp"), p("AMF", 95, "Fermín López")]}
        rows, extras, _ = lineup.merge([lineup_shot, cards])
        self.assertEqual([r["name"] for r in rows], ["Gianluigi Donnarumma", "Lionel Messi", "Declan Rice", "Dennis Bergkamp", "Fermín López"])
        self.assertEqual(extras, [])

    def test_flags_and_badges_pair_players_used_out_of_position(self):
        p = lambda pos, r, badge, name="", st=True: {"position": pos, "rating": r, "name": name, "starter": st, "badge": badge}
        shot = {"screen": "lineup", "formation": "", "players": [p("CB", 99, "croatia"), p("CB", 99, "barcelona"), p("CB", 100, "paris saintgermain")]}
        cards = {"screen": "collection", "formation": "", "players": [p("LB", 95, "croatia", "Josko Gvardiol", False), p("CMF", 95, "barcelona", "Frenkie de Jong", False),
                                                                       p("CB", 95, "psg", "Willian Pacho", False), p("CB", 94, "germany", "Nico Schlotterbeck", False)]}
        rows, _, _ = lineup.merge([shot, cards])
        self.assertEqual([r["name"] for r in rows][:2], ["Josko Gvardiol", "Frenkie de Jong"], "an LB and a CMF card played at CB")
        self.assertIn(rows[2]["name"], ("", "Willian Pacho"), "the same club written differently never blocks a pairing")


class FailedReadsTest(Helpers, TestCase):
    def test_failed_reads_never_use_the_allowance(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        tid = owner.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        scan = f"/api/orgs/{slug}/teams/{tid}/lineup-scan"
        store.update("ai", {"enabled": True, "vision": "gemini", "gemini_key": "k1", "vision_daily_total": 1})
        with mock.patch.object(lineup, "read_with", lambda cfg, key, image: (_ for _ in ()).throw(lineup.Busy("503"))):
            for _ in range(3):
                self.assertEqual(owner.call("post", scan, {"image": IMG}).status_code, 502)
        with mock.patch.object(lineup, "read_with", lambda cfg, key, image: {"players": []}):
            owner.call("post", scan, {"image": IMG})                  # nothing read: doesn't count either
        self.assertEqual(owner.get(scan).json()["left"], 10, "failed and empty reads don't count")
        with mock.patch.object(lineup, "read_with", lambda cfg, key, image: READ):
            self.assertEqual(owner.call("post", scan, {"image": IMG}).status_code, 200, "the platform cap still has room")
            self.assertEqual(owner.call("post", scan, {"image": IMG}).status_code, 429, "now the shared cap of 1 is used")
        self.assertEqual(self.signup("root").get("/api/admin/settings").status_code in (403, 404), True)


class FormationTest(Helpers, TestCase):
    def test_standard_and_fluid_formations(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        tid = owner.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        url = f"/api/orgs/{slug}/teams/{tid}/formation"
        pl = self.signup("ama")
        self.invite_and_join(owner, slug, pl, "player")
        Membership.objects.get(user__username="ama").teams.add(Team.objects.get(id=tid))
        self.assertEqual(pl.call("post", url, {"style": "standard", "standard": "4–3–3"}).json()["formation"]["standard"], "4-3-3", "players can set it")
        self.assertEqual(owner.call("post", url, {"style": "standard", "standard": "4-4-4"}).status_code, 400, "10 outfield players")
        self.assertEqual(owner.call("post", url, {"style": "fluid", "attack": "3-4-3"}).status_code, 400, "fluid needs both shapes")
        r = owner.call("post", url, {"style": "fluid", "standard": "4-3-3", "attack": "3-4-3", "defence": "4-4-2"}).json()["formation"]
        self.assertEqual((r["style"], r["attack"], r["defence"]), ("fluid", "3-4-3", "4-4-2"))
        self.assertEqual(owner.get(f"/api/orgs/{slug}/teams/{tid}").json()["team"]["formation"]["style"], "fluid")

    def test_grok_reads_screenshots_and_answers_in_a_code_block(self):
        store.update("ai", {"enabled": True, "vision": "grok", "grok_key": "xai-test"})
        cfg = lineup.config()
        self.assertEqual((cfg["enabled"], cfg["vision"], cfg["keys"]), (True, "grok", ["xai-test"]))
        self.assertEqual(lineup.json_in('Here you go:\n```json\n{"players": []}\n```'), {"players": []})


class ReaderChainTest(TestCase):
    def test_openai_then_gemini_then_grok(self):
        store.update("ai", {"enabled": True, "api_key": "sk-o", "gemini_key": "g1", "grok_key": "x1"})
        cfg = lineup.config()
        self.assertEqual([n for n, k in cfg["chain"]], ["openai", "gemini", "grok"])
        tried = []

        def fake(c, key, image):
            tried.append(c["vision"])
            if c["vision"] == "openai":
                raise lineup.Busy("429")
            if c["vision"] == "gemini":
                raise ApiError(400, "couldn't read")
            return READ
        with mock.patch.object(lineup, "read_with", fake):
            self.assertEqual(lineup.read_image(cfg, IMG), READ)
        self.assertEqual(tried, ["openai", "gemini", "grok"], "each hands over to the next")
        store.update("ai", {"api_key_clear": True})
        self.assertEqual([n for n, k in lineup.config()["chain"]], ["gemini", "grok"], "no OpenAI key: it starts with Gemini")


class XiAndNumbersTest(Helpers, TestCase):
    def test_tick_the_xi_and_numbers_from_screenshots(self):
        owner = self.signup("boss")
        slug = self.new_org(owner)
        tid = owner.call("post", f"/api/orgs/{slug}/teams", {"name": "Lions"}).json()["team"]["id"]
        pids = [owner.call("post", f"/api/orgs/{slug}/teams/{tid}/players", {"name": f"Player {i}"}).json()["player"]["id"] for i in range(13)]
        pl = self.signup("ama")
        self.invite_and_join(owner, slug, pl, "player")
        Membership.objects.get(user__username="ama").teams.add(Team.objects.get(id=tid))
        for pid in pids[:11]:
            self.assertEqual(pl.call("patch", f"/api/orgs/{slug}/players/{pid}", {"lineup": "starter"}).status_code, 200, "players tick the XI")
        r = pl.call("patch", f"/api/orgs/{slug}/players/{pids[11]}", {"lineup": "starter"})
        self.assertEqual(r.status_code, 400, "only eleven start")
        self.assertEqual(pl.call("patch", f"/api/orgs/{slug}/players/{pids[0]}", {"lineup": "sub"}).status_code, 200)
        self.assertEqual(pl.call("patch", f"/api/orgs/{slug}/players/{pids[11]}", {"lineup": "starter"}).status_code, 200)
        self.assertEqual(pl.call("patch", f"/api/orgs/{slug}/players/{pids[0]}", {"lineup": "starter", "name": "x"}).status_code, 403, "players: XI and positions only")
        # numbers: read from the screenshot when printed, saved with the lineup
        rows, _ = lineup.clean({"players": [{"position": "CF", "rating": 100, "starter": True, "name": "Kofi", "number": 9},
                                            {"position": "GK", "rating": 90, "starter": True, "name": "Ama", "number": "x"}]})
        self.assertEqual([r["number"] for r in rows], [9, None])
        owner.call("post", f"/api/orgs/{slug}/teams/{tid}/lineup", {"players": [{"name": "Kofi Striker", "position": "CF", "starter": True, "number": 9}]})
        self.assertEqual(Player.objects.get(name="Kofi Striker").number, 9)
        self.assertEqual(owner.call("post", f"/api/orgs/{slug}/teams/{tid}/lineup", {"players": [{"name": "Kofi Striker", "position": "CF", "number": 1000}]}).status_code, 400)
