"""Friendlies: a friendly series has no table, friendlies count less in rankings, and teams challenge other organizations' teams."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from orgs.tests import Helpers
from superadmin import store

from . import rankings
from .models import Competition, Match


class FriendliesTest(Helpers, TestCase):
    def setUp(self):
        self.boss, self.chief = self.signup("boss"), self.signup("chief")
        self.a = self.new_org(self.boss, "Accra League")
        self.b = self.new_org(self.chief, "Kumasi Cup")
        self.lions = self.team(self.boss, self.a, "Lions")
        self.tigers = self.team(self.chief, self.b, "Tigers")
        # Tigers play in a public competition, so they have a public page and can be challenged
        cs = self.chief.call("post", f"/api/orgs/{self.b}/competitions", {"name": "Kumasi Season", "visibility": "public"}).json()["competition"]["slug"]
        self.chief.call("post", f"/api/orgs/{self.b}/competitions/{cs}/entries", {"teamIds": [self.tigers]})

    def team(self, who, slug, name):
        return who.call("post", f"/api/orgs/{slug}/teams", {"name": name}).json()["team"]["id"]

    def soon(self, days=3):
        return (timezone.now() + timedelta(days=days)).isoformat()

    def test_challenge_accept_and_play(self):
        found = self.boss.get("/api/friendly-teams?q=tig").json()["teams"]
        self.assertEqual([(t["name"], t["org"]["name"]) for t in found], [("Tigers", "Kumasi Cup")])
        url = f"/api/orgs/{self.a}/friendlies"
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": (timezone.now() - timedelta(hours=1)).isoformat()}).status_code, 400)
        r = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon(), "message": "Warm-up?"})
        self.assertEqual(r.status_code, 200, r.json())
        ch = r.json()["challenge"]
        self.assertEqual((ch["status"], ch["direction"], ch["canCancel"]), ("pending", "sent", True))
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon(4)}).status_code, 409)
        # a player can't send challenges; the challenger can't accept their own
        fan = self.signup("fan")
        self.invite_and_join(self.boss, self.a, fan, "player")
        self.assertEqual(fan.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon(5)}).status_code, 403)
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/accept").status_code, 403)
        # the other organization answers
        theirs = self.chief.get(f"/api/orgs/{self.b}/friendlies").json()["challenges"]
        self.assertEqual([(c["direction"], c["canAnswer"], c["message"]) for c in theirs], [("received", True, "Warm-up?")])
        r = self.chief.call("post", f"/api/orgs/{self.b}/friendlies/{ch['id']}/accept")
        self.assertEqual(r.status_code, 200, r.json())
        m = Match.objects.get(id=r.json()["challenge"]["match"]["id"])
        self.assertEqual((m.competition.kind, m.competition.org.slug, m.home.team.name, m.away.team.name), ("friendly", self.a, "Lions", "Tigers"))
        self.assertEqual(self.chief.call("post", f"/api/orgs/{self.b}/friendlies/{ch['id']}/decline").status_code, 403, "already answered")
        # both teams can report the match; the host enters the result
        self.assertEqual(self.chief.get(f"/api/report/{m.id}").status_code, 200)
        self.boss.call("patch", f"/api/orgs/{self.a}/matches/{m.id}", {"homeScore": 2, "awayScore": 1})
        page = Client().get(f"/competition/{m.competition.slug}").content.decode()
        self.assertIn("Head to head", page)
        self.assertIn("1 played", page)
        self.assertNotIn(f'href="/competition/{m.competition.slug}/table"', page, "no league table for friendlies")
        self.assertEqual(Client().get(f"/competition/{m.competition.slug}/table").status_code, 404)

    def test_decline_cancel_and_own_team(self):
        url = f"/api/orgs/{self.a}/friendlies"
        own = self.team(self.boss, self.a, "Leopards")
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": own, "kickoff": self.soon()}).status_code, 404, "no public page")
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        self.assertEqual(self.chief.call("post", f"/api/orgs/{self.b}/friendlies/{ch['id']}/decline").json()["challenge"]["status"], "declined")
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/cancel").json()["challenge"]["status"], "cancelled")
        self.assertEqual(self.chief.call("post", f"/api/orgs/{self.b}/friendlies/{ch['id']}/accept").status_code, 403)
        outsider = self.signup("nosy")
        self.assertEqual(outsider.get(url).status_code, 404, "other people can't see an organization's challenges")

    def test_private_teams_only_when_their_organization_allows_it(self):
        hidden = self.team(self.chief, self.b, "Hidden Hawks")               # in no public competition
        url = f"/api/orgs/{self.a}/friendlies"
        self.assertEqual(self.boss.get("/api/friendly-teams?q=hawks").json()["teams"], [])
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": hidden, "kickoff": self.soon()}).status_code, 404)
        self.assertIn(self.boss.call("patch", f"/api/orgs/{self.b}", {"openToFriendlies": True}).status_code, (403, 404), "only their own admins")
        self.assertTrue(self.chief.call("patch", f"/api/orgs/{self.b}", {"openToFriendlies": True}).json()["org"]["openToFriendlies"])
        found = self.boss.get("/api/friendly-teams?q=hawks").json()["teams"]
        self.assertEqual([(t["name"], t["org"]["name"]) for t in found], [("Hidden Hawks", "Kumasi Cup")])
        self.assertEqual(sorted(t["name"] for t in self.boss.get("/api/friendly-teams?q=kumasi").json()["teams"]), ["Hidden Hawks", "Tigers"],
                         "found by the organization's name too")
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": hidden, "kickoff": self.soon()}).status_code, 200)

    def test_friendly_series_is_always_a_plain_series_and_counts_less(self):
        r = self.boss.call("post", f"/api/orgs/{self.a}/competitions", {"name": "Summer Friendlies", "kind": "friendly", "format": "groups_knockout", "visibility": "public"})
        c = Competition.objects.get(slug=r.json()["competition"]["slug"])
        self.assertEqual(c.format, "league", "a friendly series has no groups or knockouts")
        cheetahs = self.team(self.boss, self.a, "Cheetahs")
        self.boss.call("post", f"/api/orgs/{self.a}/competitions/{c.slug}/entries", {"teamIds": [self.lions, cheetahs]})
        mid = self.boss.call("post", f"/api/orgs/{self.a}/competitions/{c.slug}/matches", {"stage": "league", "homeId": c.entries.get(team_id=self.lions).id,
                                                                                        "awayId": c.entries.get(team_id=cheetahs).id}).json()["match"]["id"]
        self.boss.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": 1, "awayScore": 0})
        rating = {}
        for w in ("0", "0.5", "1"):
            store.update("rankings", {"enabled": True, "friendly_weight": w})
            rating[w] = next(round(r["rating"], 1) for r in rankings.compute()["teams"] if r["team"].name == "Lions")
        self.assertEqual(rating["0"], 1500.0)
        self.assertEqual(rating["0.5"], 1508.0)
        self.assertEqual(rating["1"], 1516.0)
