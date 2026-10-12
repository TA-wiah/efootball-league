"""Result screenshots read by AI (score and goals on the right sides), goals added when the result is confirmed,
and automatic confirmation when both teams send the same score (when the organization turns it on)."""
from unittest import mock

from django.test import TestCase

from orgs.models import Membership, Organization
from orgs.tests import Helpers
from superadmin import store

from . import lineup
from .models import Match, MatchEvent, Player, Team

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class ResultAiTest(Helpers, TestCase):
    def setUp(self):
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        base = f"/api/orgs/{self.slug}"
        self.ids = {n: self.boss.call("post", base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Kumasi Lions", "Accra Tigers")}
        cs = self.boss.call("post", base + "/competitions", {"name": "Cup"}).json()["competition"]["slug"]
        self.boss.call("post", f"{base}/competitions/{cs}/entries", {"teamIds": list(self.ids.values())})
        self.boss.call("post", f"{base}/competitions/{cs}/generate", {"start": "2026-01-01T15:00:00Z"})
        self.m = Match.objects.select_related("home__team", "away__team").filter(competition__slug=cs).first()
        self.players = {}
        for side in ("home", "away"):
            team = getattr(self.m, side).team
            who = self.signup(f"p_{side}")
            self.invite_and_join(self.boss, self.slug, who, "player")
            Membership.objects.get(user__username=f"p_{side}").teams.add(team)
            self.players[side] = who
        Player.objects.create(team=self.m.home.team, name="Lionel Messi", position="SS")
        store.update("ai", {"enabled": True, "vision": "gemini", "gemini_key": "k1"})

    def screen(self, left, right):
        """What the AI reads off a result screen: the left team and the right team as shown in the game."""
        return lambda cfg, key, image: {"isResult": True, "left": left, "right": right}

    def test_read_puts_score_and_goals_on_our_sides(self):
        home, away = self.m.home.team.name, self.m.away.team.name
        shown = self.screen({"name": away.upper(), "score": 1, "goals": [{"name": "K. Mensah", "minute": 50}]},
                            {"name": home, "score": 2, "goals": [{"name": "Messi", "minute": 23, "assist": "D. Rice"},
                                                                 {"name": "Y. Boateng", "minute": 80, "ownGoal": True}]})
        with mock.patch.object(lineup, "read_with", shown):
            r = self.players["home"].call("post", f"/api/report/{self.m.id}/read", {"image": PNG}).json()
        self.assertTrue(r["swapped"], "the game showed the teams the other way round")
        self.assertEqual((r["homeScore"], r["awayScore"]), (2, 1))
        self.assertEqual([(e["side"], e["name"], e["kind"]) for e in r["events"]],
                         [("home", "Messi", "goal"), ("home", "Y. Boateng", "own_goal"), ("away", "K. Mensah", "goal")])
        self.assertEqual(r["events"][1]["assist"], "", "own goals have no assist")
        with mock.patch.object(lineup, "read_with", lambda cfg, key, image: {"isResult": False}):
            self.assertEqual(self.players["home"].call("post", f"/api/report/{self.m.id}/read", {"image": PNG}).status_code, 400)
        stranger = self.signup("nosy")
        self.assertEqual(stranger.call("post", f"/api/report/{self.m.id}/read", {"image": PNG}).status_code, 404)

    def send(self, who, side, hs, as_, events=()):
        return self.players[who].call("post", f"/api/report/{self.m.id}/proof",
                                      {"side": side, "kind": "result", "image": PNG, "homeScore": hs, "awayScore": as_, "events": list(events)}).json()

    def test_confirming_adds_the_goals(self):
        goals = [{"side": "home", "kind": "goal", "name": "Messi", "minute": 23, "assist": "Kofi"},
                 {"side": "home", "kind": "penalty_goal", "name": "Lionel Messi", "minute": 70, "assist": ""},
                 {"side": "away", "kind": "goal", "name": "K. Mensah", "minute": 50, "assist": "<b>x</b>"}]
        claim = self.send("home", "home", 2, 1, goals)["claims"][0]
        self.assertEqual(len(claim["events"]), 3)
        r = self.boss.call("post", f"/api/report/{self.m.id}/approve", {"proofId": claim["id"]}).json()
        self.assertEqual(r["goalsAdded"], 3)
        evs = MatchEvent.objects.filter(match=self.m).order_by("minute")
        self.assertEqual(evs[0].player.name, "Lionel Messi", "'Messi' is linked to the squad player")
        self.assertEqual(evs[2].kind, "penalty_goal")
        self.assertEqual(Match.objects.get(id=self.m.id).home_score, 2)

    def test_auto_confirm_only_when_both_teams_agree_and_it_is_on(self):
        self.send("home", "home", 2, 1)
        self.send("away", "away", 2, 1)
        self.assertIsNone(Match.objects.get(id=self.m.id).home_score, "off by default: it waits for the organization")
        Organization.objects.filter(slug=self.slug).update(auto_confirm_results=True)
        self.send("away", "away", 1, 1)
        self.assertIsNone(Match.objects.get(id=self.m.id).home_score, "they disagree now (latest claims): it waits")
        r = self.send("away", "away", 2, 1, [{"side": "away", "kind": "goal", "name": "K. Mensah", "minute": 50}])
        m = Match.objects.get(id=self.m.id)
        self.assertEqual((m.home_score, m.away_score, m.status), (2, 1, "finished"), "both agree: confirmed straight away")
        self.assertIn("automatically", m.notes)
        self.assertEqual(MatchEvent.objects.filter(match=m).count(), 1, "the claim that listed goals is used")
        self.assertEqual(r["match"]["homeScore"], 2)
