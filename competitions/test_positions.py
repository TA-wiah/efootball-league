"""Positions worked out from real data (other teams' squads, goals and assists), players setting their squad's
positions, and adding a typed scorer to the squad from the goal form."""
from collections import Counter

from django.test import TestCase

from orgs.models import Membership
from orgs.tests import Helpers

from . import positions
from .models import Match, Player, Team


class GuessRulesTest(TestCase):
    def test_other_teams_must_clearly_agree(self):
        self.assertIsNone(positions.from_others(Counter({"CB": 1})), "one team isn't enough")
        self.assertEqual(positions.from_others(Counter({"CB": 2}))["label"], "CB")
        self.assertEqual(positions.from_others(Counter({"CB": 3, "RB": 1}))["pos"], "CB", "three out of four agree")
        self.assertIsNone(positions.from_others(Counter({"CB": 2, "DMF": 2})), "split: no guess")
        self.assertEqual(positions.from_others(Counter({"RWF": 1, "CF": 1, "SS": 1}))["label"], "FWD", "they only agree on the line")

    def test_goals_and_assists_only_when_clear(self):
        self.assertEqual(positions.from_stats(5, 1)["label"], "FWD")
        self.assertEqual(positions.from_stats(1, 4)["label"], "MID")
        self.assertIsNone(positions.from_stats(3, 0), "too few goals to tell")
        self.assertIsNone(positions.from_stats(6, 4), "a mix: no guess")
        self.assertIsNone(positions.from_stats(0, 0), "defenders are never guessed from goals")

    def test_name_order_and_accents_dont_matter(self):
        self.assertEqual(positions.key("Van Dijk Virgil"), positions.key("virgil van dijk"))
        self.assertEqual(positions.key("Luís Suárez"), positions.key("Suarez Luis"))


class AutoPositionTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner)
        self.base = f"/api/orgs/{self.slug}"
        self.ids = {n: self.owner.call("post", self.base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Lions", "Tigers", "Bears", "Owls")}

    def add(self, team, name, pos=""):
        return self.owner.call("post", f"{self.base}/teams/{self.ids[team]}/players", {"name": name, "position": pos}).json()["player"]["id"]

    def test_squad_guesses_from_other_teams_and_goals(self):
        vvd = self.add("Lions", "Virgil van Dijk")
        striker = self.add("Lions", "Kofi Goals")
        quiet = self.add("Lions", "Yaw Quiet")
        self.add("Tigers", "Van Dijk Virgil", "CB")
        guesses = self.owner.get(f"{self.base}/teams/{self.ids['Lions']}").json()["team"]["guesses"]
        self.assertNotIn(str(vvd), guesses, "only one other team so far: no guess yet")
        self.add("Bears", "virgil van dijk", "CB")
        cs = self.owner.call("post", self.base + "/competitions", {"name": "Cup"}).json()["competition"]["slug"]
        self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": [self.ids["Lions"], self.ids["Owls"]]})
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {"start": "2026-01-01T15:00:00Z"})
        m = Match.objects.filter(competition__slug=cs).select_related("home__team").first()
        side = "home" if m.home.team_id == self.ids["Lions"] else "away"
        for minute in range(5):
            self.owner.call("post", f"{self.base}/matches/{m.id}/events", {"kind": "goal", "side": side, "minute": minute + 1, "playerId": striker})
        guesses = self.owner.get(f"{self.base}/teams/{self.ids['Lions']}").json()["team"]["guesses"]
        self.assertEqual(guesses[str(vvd)]["label"], "CB")
        self.assertIn("2 other teams", guesses[str(vvd)]["why"])
        self.assertEqual(guesses[str(striker)]["label"], "FWD")
        self.assertNotIn(str(quiet), guesses, "no data, no guess")
        self.owner.call("patch", f"{self.base}/players/{vvd}", {"position": "LB"})
        guesses = self.owner.get(f"{self.base}/teams/{self.ids['Lions']}").json()["team"]["guesses"]
        self.assertNotIn(str(vvd), guesses, "a position someone set always wins")

    def test_team_players_set_their_squads_positions_only(self):
        pid = self.add("Lions", "Ama Owusu")
        other = self.add("Tigers", "Kojo Other")
        pl = self.signup("ama")
        self.invite_and_join(self.owner, self.slug, pl, "player")
        Membership.objects.get(user__username="ama").teams.add(Team.objects.get(id=self.ids["Lions"]))
        self.assertEqual(pl.call("patch", f"{self.base}/players/{pid}", {"position": "CMF"}).status_code, 200)
        self.assertEqual(Player.objects.get(id=pid).position, "CMF")
        self.assertEqual(pl.call("patch", f"{self.base}/players/{pid}", {"name": "Someone Else"}).status_code, 403, "only the position")
        self.assertEqual(pl.call("patch", f"{self.base}/players/{pid}", {"position": "GK", "active": False}).status_code, 403)
        self.assertEqual(pl.call("delete", f"{self.base}/players/{pid}").status_code, 403)
        self.assertEqual(pl.call("patch", f"{self.base}/players/{other}", {"position": "GK"}).status_code, 403, "not another team's squad")

    def test_goal_form_offers_to_add_a_new_name_to_the_squad(self):
        self.add("Tigers", "Lamine Yamal", "RWF")
        self.add("Bears", "Yamal Lamine", "RWF")
        url = f"{self.base}/teams/{self.ids['Lions']}/name-check?name="
        r = self.owner.get(url + "Lamine%20Yamal").json()
        self.assertTrue(r["canAdd"])
        self.assertEqual(r["guess"]["pos"], "RWF", "the position other teams use")
        self.add("Lions", "Lamine Yamal", r["guess"]["pos"])
        r = self.owner.get(url + "Yamal").json()
        self.assertEqual((r["as"], r["squad"]), ("Lamine Yamal", True))
        self.assertFalse(r.get("canAdd"), "already in the squad")
