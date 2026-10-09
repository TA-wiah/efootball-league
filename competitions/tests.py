"""Competitions: fixture generation, standings with tie-breakers, results, permissions and isolation."""
import base64
import itertools
from collections import Counter
from types import SimpleNamespace as NS

from django.test import TestCase

from orgs.tests import Browser, Helpers

from . import engine
from .models import Match

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
SVG = "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode()


class EngineTest(TestCase):
    def test_round_robin_everyone_meets_everyone_once_per_leg(self):
        for n in range(2, 11):
            for legs in (1, 2):
                fx = engine.round_robin(range(n), legs)
                pairs = Counter(frozenset((h, a)) for _, h, a in fx)
                self.assertEqual(len(pairs), n * (n - 1) // 2, (n, legs))
                self.assertTrue(all(v == legs for v in pairs.values()), (n, legs))
                for rnd, games in itertools.groupby(sorted(fx), key=lambda x: x[0]):
                    teams = [t for _, h, a in games for t in (h, a)]
                    self.assertEqual(len(teams), len(set(teams)), "no team plays twice in a round")
                if legs == 2:
                    homes = Counter(h for _, h, _ in fx)
                    self.assertTrue(all(homes[t] == n - 1 for t in range(n)), "double round robin: equal home games")

    def _table(self, results, tiebreakers=None, pts=(3, 1, 0), adjust=None):
        comp = NS(points_win=pts[0], points_draw=pts[1], points_loss=pts[2], tiebreakers=tiebreakers or [])
        teams = {n: NS(id=i, team=NS(name=n), points_adjustment=(adjust or {}).get(n, 0)) for i, n in enumerate("ABCD")}
        ms = [NS(id=k, home_id=teams[h].id, away_id=teams[a].id, home_score=x, away_score=y, kickoff=None, round=k)
              for k, (h, a, x, y) in enumerate(results)]
        return [r["team"].name for r in engine.standings(comp, list(teams.values()), ms)], engine.standings(comp, list(teams.values()), ms)

    def test_points_and_goal_difference(self):
        order, rows = self._table([("A", "B", 3, 0), ("C", "D", 1, 1), ("A", "C", 0, 1), ("B", "D", 2, 0)])
        self.assertEqual(order, ["C", "A", "B", "D"])
        a = rows[1]
        self.assertEqual((a["played"], a["won"], a["lost"], a["goalsFor"], a["goalsAgainst"], a["goalDifference"], a["points"]),
                         (2, 1, 1, 3, 1, 2, 3))
        self.assertEqual(a["form"], ["W", "L"])

    def test_head_to_head_beats_goal_difference_when_configured(self):
        # A and B both on 3 points; B has the better goal difference, but A beat B.
        res = [("A", "B", 1, 0), ("B", "D", 6, 0), ("C", "A", 1, 0), ("C", "B", 1, 0)]   # A and B both on 3 points
        gd_first = self._table(res, ["points", "goal_difference"])[0]
        h2h_first = self._table(res, ["points", "head_to_head_points", "goal_difference"])[0]
        self.assertLess(gd_first.index("B"), gd_first.index("A"))
        self.assertLess(h2h_first.index("A"), h2h_first.index("B"))

    def test_custom_points_and_adjustments(self):
        order, rows = self._table([("A", "B", 1, 1), ("C", "D", 2, 1)], pts=(2, 1, 0), adjust={"C": -3})
        self.assertEqual({r["team"].name: r["points"] for r in rows}, {"A": 1, "B": 1, "C": -1, "D": 0})
        self.assertEqual(order[-1], "C")


class CompetitionApiTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner)
        self.base = f"/api/orgs/{self.slug}"

    def comp(self, **extra):
        r = self.owner.call("post", self.base + "/competitions", {"name": "Sunday League", "season": "2026/27", **extra})
        self.assertEqual(r.status_code, 200, r.json())
        return r.json()["competition"]["slug"]

    def teams(self, names):
        return [self.owner.call("post", self.base + "/teams", {"name": n, "venue": f"{n} Park"}).json()["team"]["id"] for n in names]

    def setup_league(self, names="ABCD", **extra):
        cs = self.comp(**extra)
        ids = self.teams([f"Team {n}" for n in names])
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": ids}).json()["added"], len(ids))
        return cs, ids

    def test_full_league_flow(self):
        cs, ids = self.setup_league()
        r = self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {"legs": 2, "start": "2026-10-04T15:00:00Z", "daysBetween": 7})
        self.assertEqual(r.json()["created"], 12)
        ms = self.owner.get(f"{self.base}/competitions/{cs}/matches").json()["matches"]
        self.assertEqual(len(ms), 12)
        self.assertTrue(all(m["venue"].endswith("Park") and m["kickoff"] for m in ms))
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {}).status_code, 409, "asks before replacing")
        first = ms[0]
        r = self.owner.call("patch", f"{self.base}/matches/{first['id']}", {"homeScore": 2, "awayScore": 1})
        self.assertEqual(r.json()["match"]["status"], "finished")
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {"replace": True}).status_code, 409, "results protect fixtures")
        table = self.owner.get(f"{self.base}/competitions/{cs}/standings").json()
        top = table["groups"][0]["rows"][0]
        self.assertEqual((top["team"]["name"], top["points"], top["played"]), (first["home"]["name"], 3, 1))
        self.assertEqual(len(table["groups"][0]["rows"]), 4)
        summary = self.owner.get(f"{self.base}/summary").json()
        self.assertEqual((summary["competitions"], summary["teams"], summary["completed"]), (1, 4, 1))

    def test_groups_get_separate_fixtures_and_tables(self):
        cs = self.comp(format="groups_knockout")
        a = self.teams(["A1", "A2", "A3"])
        b = self.teams(["B1", "B2", "B3", "B4"])
        self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": a, "group": "A"})
        self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": b, "group": "B"})
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {}).json()["created"], 3 + 6)
        self.assertTrue(all(m.home.group == m.away.group == m.group for m in Match.objects.all()), "no cross-group games")
        groups = self.owner.get(f"{self.base}/competitions/{cs}/standings").json()["groups"]
        self.assertEqual([(g["name"], len(g["rows"])) for g in groups], [("A", 3), ("B", 4)])

    def test_knockout_draw_and_events(self):
        cs, ids = self.setup_league("ABCDEF", format="knockout")
        entries = [e["id"] for e in self.owner.get(f"{self.base}/competitions/{cs}").json()["entries"]]
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{cs}/draw", {"entryIds": entries[:3]}).status_code, 400)
        r = self.owner.call("post", f"{self.base}/competitions/{cs}/draw", {"entryIds": entries[:4], "roundName": "Semi-finals", "legs": 2}).json()
        self.assertEqual(len(r["matches"]), 4)
        m = r["matches"][0]
        home_team = m["home"]["id"]
        p = self.owner.call("post", f"{self.base}/teams/{home_team}/players", {"name": "Kwame", "number": 9, "position": "FW"}).json()["player"]
        other = self.owner.call("post", f"{self.base}/teams/{m['away']['id']}/players", {"name": "Not Here"}).json()["player"]
        ev = self.owner.call("post", f"{self.base}/matches/{m['id']}/events", {"kind": "goal", "side": "home", "minute": 23, "playerId": p["id"], "assistName": "Ama"})
        self.assertEqual(ev.status_code, 200)
        self.assertEqual(self.owner.call("post", f"{self.base}/matches/{m['id']}/events", {"kind": "goal", "side": "home", "playerId": other["id"]}).status_code, 400,
                         "a player from the other team can't score for this one")
        scorers = self.owner.get(f"{self.base}/competitions/{cs}/scorers").json()["scorers"]
        self.assertEqual([(s["name"], s["goals"]) for s in scorers][:1], [("Kwame", 1)])
        self.assertIn(("Ama", 0, 1), [(s["name"], s["goals"], s["assists"]) for s in scorers])

    def test_roles_enforced_by_the_server(self):
        cs, ids = self.setup_league()
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {})
        match = self.owner.get(f"{self.base}/competitions/{cs}/matches").json()["matches"][0]["id"]
        people = {}
        for role in ["editor", "moderator", "viewer", "admin"]:
            people[role] = self.signup(role + "1")
            self.invite_and_join(self.owner, self.slug, people[role], role)
        for role, b in people.items():
            staff, scorer = role == "admin", role in ("admin", "editor")
            self.assertEqual(b.call("post", self.base + "/competitions", {"name": "New Cup"}).status_code, 200 if staff else 403, role)
            self.assertEqual(b.call("patch", f"{self.base}/matches/{match}", {"homeScore": 1, "awayScore": 0}).status_code, 200 if scorer else 403, role)
            self.assertEqual(b.call("patch", f"{self.base}/matches/{match}", {"roundName": "Opening day"}).status_code, 200 if staff else 403, role)
            self.assertEqual(b.call("post", self.base + "/teams", {"name": f"{role} FC"}).status_code, 200 if scorer else 403, role)
            self.assertEqual(b.call("patch", f"{self.base}/competitions/{cs}", {"description": "x"}).status_code, 200 if scorer else 403, role)
            self.assertEqual(b.call("patch", f"{self.base}/competitions/{cs}", {"pointsWin": 2}).status_code, 200 if staff else 403, role)
            self.assertEqual(b.get(f"{self.base}/competitions/{cs}/standings").status_code, 200, "everyone can read")
        ed = people["editor"]
        self.assertEqual(ed.call("delete", f"{self.base}/competitions/{cs}", {"confirm": "Sunday League"}).status_code, 403)
        self.assertEqual(ed.call("post", f"{self.base}/competitions/{cs}/generate", {"replace": True}).status_code, 403)

    def test_other_organizations_cannot_touch_anything(self):
        cs, ids = self.setup_league()
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {})
        match = self.owner.get(f"{self.base}/competitions/{cs}/matches").json()["matches"][0]["id"]
        mallory = self.signup("mallory")
        own = self.new_org(mallory, "Mallory League")
        mine = f"/api/orgs/{own}"
        mallory.call("post", mine + "/competitions", {"name": "Mallory Cup"})
        # through their own organization, using the victim's IDs
        self.assertEqual(mallory.call("post", f"{mine}/competitions/mallory-cup/entries", {"teamIds": ids[:2]}).status_code, 400)
        self.assertEqual(mallory.call("patch", f"{mine}/matches/{match}", {"homeScore": 9, "awayScore": 0}).status_code, 404)
        self.assertEqual(mallory.call("patch", f"{mine}/teams/{ids[0]}", {"name": "Hacked"}).status_code, 404)
        self.assertEqual(mallory.get(f"{mine}/teams/{ids[0]}").status_code, 404)
        # through the victim's organization
        for url in [f"{self.base}/competitions", f"{self.base}/competitions/{cs}/standings", f"{self.base}/matches/{match}", f"{self.base}/teams"]:
            self.assertEqual(mallory.get(url).status_code, 404, url)
        self.assertEqual(Match.objects.get(id=match).home_score, None)

    def test_deleting_is_protected(self):
        cs, ids = self.setup_league("AB")
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {})
        entry = self.owner.get(f"{self.base}/competitions/{cs}").json()["entries"][0]["id"]
        self.assertEqual(self.owner.call("delete", f"{self.base}/teams/{ids[0]}").status_code, 409)
        self.assertEqual(self.owner.call("delete", f"{self.base}/competitions/{cs}/entries/{entry}").status_code, 409)
        self.assertEqual(self.owner.call("delete", f"{self.base}/competitions/{cs}", {"confirm": "nope"}).status_code, 400)
        self.assertEqual(self.owner.call("delete", f"{self.base}/competitions/{cs}", {"confirm": "Sunday League"}).status_code, 200)
        self.assertEqual(self.owner.call("delete", f"{self.base}/teams/{ids[0]}").status_code, 200, "free once its matches are gone")

    def test_deleting_an_organization_removes_everything(self):
        cs, ids = self.setup_league("ABC")
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {})
        self.assertEqual(self.owner.call("delete", self.base, {"confirm": "Kasoa Community League"}).status_code, 200)
        self.assertEqual(Match.objects.count(), 0)

    def test_logos(self):
        cs = self.comp(visibility="private")
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{cs}/logo", {"image": SVG}).status_code, 400, "SVG can carry scripts")
        url = self.owner.call("post", f"{self.base}/competitions/{cs}/logo", {"image": PNG}).json()["logo"]
        r = self.owner.get(url)
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        self.assertEqual(Browser().get(url).status_code, 404, "private competition logos need membership")
        self.owner.call("patch", f"{self.base}/competitions/{cs}", {"visibility": "public"})
        self.assertEqual(Browser().get(url).status_code, 200)

    def test_announcements_permissions(self):
        ed, mod = self.signup("eddie"), self.signup("modo")
        self.invite_and_join(self.owner, self.slug, ed, "editor")
        self.invite_and_join(self.owner, self.slug, mod, "moderator")
        a = ed.call("post", self.base + "/announcements", {"title": "Kick-off moved", "body": "15:00 now"}).json()["announcement"]["id"]
        self.assertEqual(mod.call("post", self.base + "/announcements", {"title": "x"}).status_code, 403)
        self.assertEqual(ed.call("patch", f"{self.base}/announcements/{a}", {"title": "Kick-off moved to 15:00"}).status_code, 200)
        self.assertEqual(mod.call("patch", f"{self.base}/announcements/{a}", {"published": False}).status_code, 200, "moderators can unpublish")
        self.assertEqual(mod.call("patch", f"{self.base}/announcements/{a}", {"title": "changed"}).status_code, 403)
        self.assertEqual(mod.call("delete", f"{self.base}/announcements/{a}").status_code, 200)

    def test_fixture_schedule_uses_saved_settings_and_dates(self):
        from datetime import date, timedelta
        cs, ids = self.setup_league()                                     # 4 teams: 6 matches once, 12 home and away
        start = date.today() + timedelta(days=10)
        url = f"{self.base}/competitions/{cs}"
        bad = [{"perDay": 1}, {"perDay": 21}, {"everyDays": 0}, {"everyDays": 6}, {"time": "25:00"}, {"nope": 1}]
        for s in bad:
            self.assertEqual(self.owner.call("patch", url, {"schedule": s}).status_code, 400, s)
        r = self.owner.call("patch", url, {"startDate": start.isoformat(), "endDate": (start + timedelta(days=30)).isoformat(),
                                            "schedule": {"perDay": 4, "everyDays": 2, "time": "19:00", "gap": 30}})
        self.assertEqual(r.json()["competition"]["schedule"], {"perDay": 4, "everyDays": 2, "time": "19:00", "gap": 30})
        r = self.owner.call("post", url + "/generate", {"legs": 2})                # nothing asked: start date and saved schedule
        self.assertEqual(r.status_code, 200, r.json())
        ms = sorted(self.owner.get(url + "/matches").json()["matches"], key=lambda m: m["kickoff"])
        days = sorted({m["kickoff"][:10] for m in ms})
        self.assertEqual(days, [(start + timedelta(days=2 * i)).isoformat() for i in range(3)], "4 a day, every 2 days, from the start date")
        self.assertEqual([m["kickoff"][11:16] for m in ms[:4]], ["19:00", "19:30", "20:00", "20:30"])
        self.assertEqual([m["round"] for m in ms], sorted(m["round"] for m in ms), "matchday by matchday")
        # too long for the end date: refused with a suggestion, and nothing changes
        self.owner.call("patch", url, {"endDate": (start + timedelta(days=2)).isoformat()})
        r = self.owner.call("post", url + "/generate", {"legs": 2, "replace": True, "schedule": {"perDay": 2, "everyDays": 1}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Play 4 matches a day", r.json()["error"])
        self.assertEqual(len(self.owner.get(url + "/matches").json()["matches"]), 12)
        # knockout rounds follow on after the last scheduled match
        self.owner.call("patch", url, {"endDate": None, "format": "groups_knockout"})
        entries = self.owner.get(url).json()["entries"]
        r = self.owner.call("post", url + "/draw", {"entryIds": [e["id"] for e in entries], "roundName": "Semi-finals"})
        self.assertEqual(r.status_code, 200, r.json())
        last_league = ms[-1]["kickoff"][:10]
        self.assertTrue(all(m["kickoff"][:10] > last_league for m in r.json()["matches"]))

    def groups_comp(self, groups=2, per=4, q=2):
        cs = self.comp(format="groups_knockout", qualifiersPerGroup=q)
        url = f"{self.base}/competitions/{cs}"
        names = [f"{chr(65 + g)}{i}" for g in range(groups) for i in range(1, per + 1)]
        ids = self.teams(names)
        for tid, n in zip(ids, names):
            self.owner.call("post", url + "/entries", {"teamId": tid, "group": n[0]})
        entries = {e["team"]["name"]: e["id"] for e in self.owner.get(url).json()["entries"]}
        return cs, url, entries

    def test_group_stage_only_within_groups(self):
        cs, url, e = self.groups_comp()
        r = self.owner.call("post", url + "/matches", {"stage": "league", "homeId": e["A1"], "awayId": e["B1"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("own group", r.json()["error"])
        r = self.owner.call("post", url + "/matches", {"stage": "league", "homeId": e["A1"], "awayId": e["A2"], "group": "B"})
        self.assertEqual((r.status_code, r.json()["match"]["group"]), (200, "A"), "the group follows the teams")
        self.owner.call("post", url + "/generate", {"replace": True})
        ms = self.owner.get(url + "/matches").json()["matches"]
        ids = {v: k for k, v in e.items()}
        self.assertTrue(all(ids[m["home"]["entryId"]][0] == ids[m["away"]["entryId"]][0] == m["group"] for m in ms if m["stage"] == "league"))
        self.assertEqual(self.owner.call("patch", f"{url}/entries/{e['A1']}", {"group": "B"}).status_code, 409, "can't move once it has group games")

    def test_knockout_plan_fills_itself_in(self):
        from competitions.models import Match
        cs, url, e = self.groups_comp()
        r = self.owner.call("post", url + "/generate", {"knockout": {"mode": "cross"}})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["knockout"]["rounds"], ["Semi-finals", "Final"])
        ko = [m for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout"]
        self.assertEqual([(m["homeFrom"], m["awayFrom"]) for m in ko],
                         [("Group A winner", "Group B runner-up"), ("Group B winner", "Group A runner-up"), ("Winner of Semi-final 1", "Winner of Semi-final 2")])
        self.assertTrue(all(m["home"] is None for m in ko), "nobody is known yet")
        # play group A: the top two go into their semi-final slots; group B is still unknown
        for m in Match.objects.filter(competition__slug=cs, stage="league", group="A"):
            h = m.home.team.name
            score = {"A1": 3, "A2": 2, "A3": 1, "A4": 0}
            a = m.away.team.name
            self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": score[h], "awayScore": score[a]})
        ko = [m for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout"]
        self.assertEqual((ko[0]["home"]["name"], ko[0]["away"], ko[1]["away"]["name"]), ("A1", None, "A2"))
        for m in Match.objects.filter(competition__slug=cs, stage="league", group="B"):
            score = {"B1": 3, "B2": 2, "B3": 1, "B4": 0}
            self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": score[m.home.team.name], "awayScore": score[m.away.team.name]})
        semis = [m for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout"][:2]
        self.assertEqual([(m["home"]["name"], m["away"]["name"]) for m in semis], [("A1", "B2"), ("B1", "A2")])
        # a semi-final level after 90 minutes needs penalties before the final fills in
        self.owner.call("patch", f"{self.base}/matches/{semis[0]['id']}", {"homeScore": 1, "awayScore": 1})
        final = next(m for m in self.owner.get(url + "/matches").json()["matches"] if m["roundName"] == "Final")
        self.assertIsNone(final["home"])
        self.owner.call("patch", f"{self.base}/matches/{semis[0]['id']}", {"homePens": 4, "awayPens": 5})
        self.owner.call("patch", f"{self.base}/matches/{semis[1]['id']}", {"homeScore": 2, "awayScore": 0})
        final = next(m for m in self.owner.get(url + "/matches").json()["matches"] if m["roundName"] == "Final")
        self.assertEqual((final["home"]["name"], final["away"]["name"]), ("B2", "B1"))
        # results are in, so the plan can't be changed any more
        self.assertEqual(self.owner.call("post", url + "/knockout-plan", {"replace": True}).status_code, 409)

    def test_knockout_plan_rules(self):
        cs, url, e = self.groups_comp(groups=4, per=3, q=2)       # 8 through: quarter-finals
        r = self.owner.call("post", url + "/knockout-plan", {"mode": "random", "legs": 2})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["rounds"], ["Quarter-finals", "Semi-finals", "Final"])
        ko = [m for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout"]
        firsts = [m for m in ko if m["roundName"] == "Quarter-finals" and m["leg"] == 1]
        self.assertEqual(len(firsts), 4)
        for m in firsts:
            self.assertTrue(m["homeFrom"].endswith("winner") and m["awayFrom"].endswith("runner-up"), m)
            self.assertNotEqual(m["homeFrom"].split()[1], m["awayFrom"].split()[1], "never someone from their own group")
        self.assertEqual(len([m for m in ko if m["roundName"] == "Final"]), 1, "the final is one match")
        self.assertEqual(self.owner.call("post", url + "/knockout-plan", {}).status_code, 409, "asks before replacing")
        self.owner.call("patch", url, {"qualifiersPerGroup": 3})              # 12 through: round of 16, winners get byes
        r = self.owner.call("post", url + "/knockout-plan", {"replace": True})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["rounds"], ["Round of 16", "Quarter-finals", "Semi-finals", "Final"])
        r16 = [m for m in self.owner.get(url + "/matches").json()["matches"] if m["roundName"] == "Round of 16" and m["leg"] == 1]
        byes = [m for m in r16 if "Bye" in (m["homeFrom"], m["awayFrom"])]
        self.assertEqual(sorted(m["homeFrom"] for m in byes), [f"Group {g} winner" for g in "ABCD"])
        for m in r16:
            if m not in byes:
                self.assertNotEqual(m["homeFrom"].split()[1], m["awayFrom"].split()[1], "never someone from their own group")
        plain = self.comp()
        self.assertEqual(self.owner.call("post", f"{self.base}/competitions/{plain}/knockout-plan", {}).status_code, 400)

    def test_third_place_needs_the_points_or_stays_out(self):
        from competitions.models import Match
        cs, url, e = self.groups_comp(groups=2, per=4, q=3)
        self.assertEqual(self.owner.call("patch", url, {"thirdMinPoints": 4}).json()["competition"]["thirdMinPoints"], 4)
        r = self.owner.call("post", url + "/generate", {"knockout": {"mode": "cross"}})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["knockout"]["rounds"], ["Quarter-finals", "Semi-finals", "Final"], "6 through: group winners get byes")
        # group A: A3 finishes 3rd with 3 points (not enough); group B: B3 finishes 3rd with 4 points
        wins = {("A1", "A2"): (2, 0), ("A1", "A3"): (2, 0), ("A1", "A4"): (2, 0), ("A2", "A3"): (2, 0), ("A2", "A4"): (2, 0), ("A3", "A4"): (2, 0),
                ("B1", "B2"): (2, 0), ("B1", "B3"): (2, 0), ("B1", "B4"): (2, 0), ("B2", "B3"): (1, 1), ("B2", "B4"): (3, 0), ("B3", "B4"): (2, 0)}
        for m in Match.objects.filter(competition__slug=cs, stage="league"):
            h, a = m.home.team.name, m.away.team.name
            hs, as_ = wins[(h, a)] if (h, a) in wins else wins[(a, h)][::-1]
            self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": hs, "awayScore": as_})
        table = self.owner.get(url + "/standings").json()
        marks = {r["team"]["name"]: (r["position"], r["points"], r["qualifies"], r["short"]) for g in table["groups"] for r in g["rows"]}
        self.assertEqual(marks["A3"], (3, 3, False, True), "3rd without enough points: faded, not through")
        self.assertEqual(marks["B3"][2:], (True, False))
        self.assertEqual((marks["A2"][2], marks["A4"][2]), (True, False))
        ko = [m for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout"]
        teams = lambda m: (m["home"]["name"] if m["home"] else None, m["away"]["name"] if m["away"] else None)
        names = [n for m in ko for n in teams(m) if n]
        self.assertNotIn("A3", names, "the 3rd without the points isn't added")
        self.assertIn("B3", names)
        qf = [m for m in ko if m["roundName"] == "Quarter-finals"]
        self.assertEqual(sum(m["decided"] == "bye" for m in qf), 3, "two group winners' byes, and A3's opponent goes straight through")
        semis = [teams(m) for m in ko if m["roundName"] == "Semi-finals"]
        self.assertIn("A1", [x for t in semis for x in t])
        self.assertIn("B1", [x for t in semis for x in t])
        page = __import__("django.test", fromlist=["Client"]).Client().get(f"/competition/{cs}/table").content.decode()
        self.assertIn("3rd place needs 4+ points", page)
        # a corrected result giving A3 the points puts it back in
        m = Match.objects.get(competition__slug=cs, stage="league", home__team__name__in=["A2", "A3"], away__team__name__in=["A2", "A3"])
        self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": 1, "awayScore": 1})
        names = [n for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout" for n in teams(m) if n]
        self.assertIn("A3", names)

    def play_groups(self, cs, goals):
        """Every group match: home scores goals[home], away scores goals[away]."""
        from competitions.models import Match
        for m in Match.objects.filter(competition__slug=cs, stage="league"):
            self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": goals[m.home.team.name], "awayScore": goals[m.away.team.name]})

    def ko_matches(self, url, name=None):
        return [m for m in self.owner.get(url + "/matches").json()["matches"] if m["stage"] == "knockout" and (name is None or m["roundName"] == name)]

    def test_draw_after_the_groups_everyone_plays(self):
        cs, url, e = self.groups_comp(groups=2, per=4, q=3)
        self.owner.call("patch", url, {"thirdMinPoints": 4})
        r = self.owner.call("post", url + "/generate", {"knockout": {"mode": "ranked"}})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["knockout"]["rounds"], ["Quarter-finals", "Semi-finals", "Final"])
        qf = self.ko_matches(url, "Quarter-finals")
        self.assertEqual(qf[0]["homeFrom"], "Seed 1 (by group results)")
        self.assertNotIn("Bye", [x for m in qf for x in (m["homeFrom"], m["awayFrom"])])
        # A1, B1, A2, B2, B3 qualify (A3 has 3 points, short of 4). Topped up to 8 with the best of the rest: A3, B4, A4
        self.play_groups(cs, {"A1": 4, "A2": 2, "A3": 1, "A4": 0, "B1": 2, "B2": 1, "B3": 1, "B4": 0})
        qf = self.ko_matches(url, "Quarter-finals")
        self.assertFalse(any(m["decided"] for m in qf), "no byes: everyone plays")
        pairs = {frozenset((m["home"]["name"], m["away"]["name"])) for m in qf}
        self.assertEqual(pairs, {frozenset(p) for p in (("A1", "B4"), ("B1", "A4"), ("A2", "B3"), ("B2", "A3"))},
                         "best v weakest, never someone from their own group")
        # not enough teams to fill the places: asks for another setting
        cs2, url2, _ = self.groups_comp(groups=2, per=3, q=3)
        r = self.owner.call("post", url2 + "/knockout-plan", {"mode": "ranked"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("needs 8 teams", r.json()["error"])

    def test_best_third_placed_teams_world_cup_style(self):
        cs, url, e = self.groups_comp(groups=3, per=4, q=2)
        self.assertEqual(self.owner.call("patch", url, {"bestThirds": 4}).status_code, 200)
        self.assertEqual(self.owner.call("post", url + "/knockout-plan", {}).status_code, 400, "only 3 groups, so at most 3 thirds")
        r = self.owner.call("patch", url, {"bestThirds": "auto"})
        self.assertEqual(r.json()["competition"]["bestThirds"], "auto", "Auto: 3 groups × 2 = 6, so the best 2 thirds make 8")
        r = self.owner.call("post", url + "/generate", {"knockout": {"mode": "cross"}})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["knockout"]["rounds"], ["Quarter-finals", "Semi-finals", "Final"], "6 + 2 best thirds = 8, no byes")
        froms = [x for m in self.ko_matches(url, "Quarter-finals") for x in (m["homeFrom"], m["awayFrom"])]
        self.assertEqual(sorted(f for f in froms if f.startswith("Best")), ["Best 3rd-placed team 1", "Best 3rd-placed team 2"])
        self.assertNotIn("Bye", froms)
        # A3 goal difference 0, B3 -11, C3 -13: A3 and B3 go through
        self.play_groups(cs, {"A1": 9, "A2": 6, "A3": 5, "A4": 0, "B1": 9, "B2": 6, "B3": 2, "B4": 0, "C1": 9, "C2": 6, "C3": 1, "C4": 0})
        table = self.owner.get(url + "/standings").json()
        marks = {r["team"]["name"]: ("q" if r["qualifies"] else "wc" if r["wildcard"] else "") for g in table["groups"] for r in g["rows"]}
        self.assertEqual((marks["A3"], marks["B3"], marks["C3"], marks["A2"]), ("wc", "wc", "", "q"))
        names = {m[k]["name"] for m in self.ko_matches(url, "Quarter-finals") for k in ("home", "away") if m[k]}
        self.assertEqual(names, {"A1", "A2", "B1", "B2", "C1", "C2", "A3", "B3"})
        from django.test import Client
        self.assertIn("the best 2 from the next place", Client().get(f"/competition/{cs}/table").content.decode())
        # with a points minimum, a third that falls short is replaced by nobody: its opponent goes straight through
        self.owner.call("patch", url, {"thirdMinPoints": 4})
        from competitions import bracket
        from competitions.models import Competition
        bracket.resolve(Competition.objects.get(slug=cs))
        self.assertEqual(sum(m["decided"] == "bye" for m in self.ko_matches(url, "Quarter-finals")), 2, "A3 and B3 have 3 points")

    def test_knockout_dates_follow_the_group_stage(self):
        from datetime import date, timedelta
        cs, url, e = self.groups_comp()
        self.owner.call("patch", url, {"startDate": (date.today() + timedelta(days=5)).isoformat(), "schedule": {"perDay": 20, "everyDays": 2}})
        self.owner.call("post", url + "/generate", {"knockout": {"mode": "cross"}})
        ms = self.owner.get(url + "/matches").json()["matches"]
        last_group = max(m["kickoff"] for m in ms if m["stage"] == "league")[:10]
        semis = {m["kickoff"][:10] for m in ms if m["roundName"] == "Semi-finals"}
        final = {m["kickoff"][:10] for m in ms if m["roundName"] == "Final"}
        self.assertEqual(len(semis), 1)
        self.assertTrue(last_group < min(semis) < min(final), "each round on its own day, after the groups")

    def test_set_dates_keeps_pairings(self):
        from datetime import date, timedelta
        cs, ids = self.setup_league()
        url = f"{self.base}/competitions/{cs}"
        self.owner.call("post", url + "/generate", {})
        Match = __import__("competitions.models", fromlist=["Match"]).Match
        Match.objects.filter(competition__slug=cs).update(kickoff=None)
        before = [(m.home_id, m.away_id) for m in Match.objects.filter(competition__slug=cs).order_by("round", "id")]
        start = date.today() + timedelta(days=3)
        self.owner.call("patch", url, {"startDate": start.isoformat()})
        r = self.owner.call("post", url + "/dates", {"schedule": {"perDay": 2, "everyDays": 1}})
        self.assertEqual((r.status_code, r.json()["updated"]), (200, 6))
        after = list(Match.objects.filter(competition__slug=cs).order_by("round", "id"))
        self.assertEqual([(m.home_id, m.away_id) for m in after], before, "same pairings")
        self.assertEqual(len({m.kickoff.date() for m in after}), 3, "2 a day over 3 days")
        self.assertEqual(self.owner.call("post", url + "/dates", {}).status_code, 400, "nothing left to date")


class ImportLeagueTest(Helpers, TestCase):
    def test_original_league_becomes_a_competition(self):
        import json as _json

        from django.core.management import call_command

        from league.models import League
        owner = self.signup("boss")
        slug = self.new_org(owner)
        state = {"g": {"A": ["Ama", "Bo", "Cee", "Dee"], "B": ["E1", "E2", "E3", "E4", "E5"]},
                 "r": {"A0_0": [2, 1], "B0_0": [0, 3]},     # old page: matchday 1, first match of each group
                 "ev": {"r.A0_0": [{"s": 0, "m": 12, "p": "Haaland", "a": "Saka"}]},
                 "cfg": {"a": 2}, "ui": {"t": "CHAMPIONS LEAGUE", "s": "eFOOTBALL LEAGUE"}}
        League.objects.create(pk=1, data=_json.dumps(state), rev=1)
        call_command("import_league", slug, stdout=open(__import__("os").devnull, "w"))
        c = owner.get(f"/api/orgs/{slug}/competitions").json()["competitions"][0]
        self.assertEqual((c["name"], c["format"], c["teams"], c["matches"], c["finished"]), ("Champions League", "groups_knockout", 9, 12 + 20, 2))
        a = Match.objects.get(group="A", round=1, home__team__name="Ama")
        self.assertEqual((a.away.team.name, a.home_score, a.away_score), ("Dee", 2, 1), "same pairing as the old page (FX4)")
        self.assertEqual([(e.player_name, e.assist_name, e.minute) for e in a.events.all()], [("Haaland", "Saka", 12)])
        b = Match.objects.get(group="B", round=1, home_score=0)
        self.assertEqual((b.home.team.name, b.away.team.name, b.away_score), ("E2", "E5", 3), "old page's circle method for 5 teams (E1 rests)")
        table = owner.get(f"/api/orgs/{slug}/competitions/{c['slug']}/standings").json()["groups"]
        self.assertEqual(table[0]["rows"][0]["team"]["name"], "Ama")
