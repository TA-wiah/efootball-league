"""Friendlies: a friendly series has no table, friendlies count less in rankings, and teams challenge other organizations' teams."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from orgs.tests import Helpers
from superadmin import store

from . import rankings
from .models import Competition, Match


def FriendlyChallengeId(who, url):
    return who.get(url).json()["challenges"][0]["id"]


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

    def test_friendlies_inside_the_organization(self):
        url = f"/api/orgs/{self.a}/friendlies"
        leopards = self.team(self.boss, self.a, "Leopards")                # private: no public page needed inside the organization
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.lions, "kickoff": self.soon()}).status_code, 400)
        # the owner manages both teams: the match is set straight away
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": self.soon()}).json()["challenge"]
        self.assertEqual((ch["status"], ch["internal"], ch["to"]["name"]), ("accepted", True, "Leopards"))
        m = Match.objects.get(id=ch["match"]["id"])
        self.assertEqual((m.competition.kind, m.home.team.name, m.away.team.name), ("friendly", "Lions", "Leopards"))
        # a team manager of Lions challenges Leopards: Leopards' managers (here the owner) answer
        tm = self.signup("tm1")
        self.invite_and_join(self.boss, self.a, tm, "team_manager")
        mid = self.member_id(self.boss, self.a, "tm1")
        self.boss.call("patch", f"/api/orgs/{self.a}/members/{mid}", {"teams": [self.lions]})
        ch = tm.call("post", url, {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": self.soon(6)}).json()["challenge"]
        self.assertEqual((ch["status"], ch["canCancel"], ch["canAnswer"]), ("pending", True, False))
        self.assertEqual(tm.call("post", f"{url}/{ch['id']}/accept").status_code, 403, "can't accept for a team they don't manage")
        theirs = next(c for c in self.boss.get(url).json()["challenges"] if c["id"] == ch["id"])
        self.assertTrue(theirs["canAnswer"])
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/accept").json()["challenge"]["status"], "accepted")

    def test_any_time_now_and_already_played(self):
        url = f"/api/orgs/{self.a}/friendlies"
        far = (timezone.now() + timedelta(days=400)).isoformat()
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": far}).status_code, 200, "more than months ahead")
        self.boss.call("post", f"{url}/{FriendlyChallengeId(self.boss, url)}/cancel")
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": "now"}).json()["challenge"]
        self.assertEqual(ch["status"], "pending")
        # an hour later it's still open; accepted then, the match is set for that moment
        from .models import FriendlyChallenge
        FriendlyChallenge.objects.filter(id=ch["id"]).update(kickoff=timezone.now() - timedelta(hours=1))
        r = self.chief.call("post", f"/api/orgs/{self.b}/friendlies/{ch['id']}/accept").json()
        m = Match.objects.get(id=r["challenge"]["match"]["id"])
        self.assertLess(abs((m.kickoff - timezone.now()).total_seconds()), 60)
        # a challenge can't be in the past, but whoever manages both teams can record one already played
        leopards = self.team(self.boss, self.a, "Leopards")
        past = (timezone.now() - timedelta(days=2)).isoformat()
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": past}).status_code, 400)
        r = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": past})
        self.assertEqual(r.json()["challenge"]["status"], "accepted")

    def test_delete(self):
        url, theirs = f"/api/orgs/{self.a}/friendlies", f"/api/orgs/{self.b}/friendlies"
        # a waiting challenge: either side can delete it
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        self.assertTrue(ch["canDelete"])
        self.assertEqual(self.chief.call("post", f"{theirs}/{ch['id']}/delete").status_code, 200)
        self.assertEqual(self.boss.get(url).json()["challenges"], [])
        # accepted, not played: the other team's managers can delete it, match and all
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        mid = self.chief.call("post", f"{theirs}/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        series = Match.objects.get(id=mid).competition
        self.assertEqual(self.chief.call("post", f"{theirs}/{ch['id']}/delete").status_code, 200)
        self.assertFalse(Match.objects.filter(id=mid).exists())
        self.assertEqual(series.entries.count(), 0, "teams with no friendly left are taken out of the series")
        # played: only the hosting organization's staff can delete it
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        mid = self.chief.call("post", f"{theirs}/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        self.boss.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": 3, "awayScore": 0})
        r = self.chief.call("post", f"{theirs}/{ch['id']}/delete")
        self.assertEqual(r.status_code, 403)
        self.assertIn("organization hosting it", r.json()["error"])
        self.assertFalse(next(c for c in self.chief.get(theirs).json()["challenges"] if c["id"] == ch["id"])["canDelete"])
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/delete").status_code, 200)
        self.assertFalse(Match.objects.filter(id=mid).exists())
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/delete").status_code, 404)

    def test_how_a_match_ended(self):
        url = f"/api/orgs/{self.a}/friendlies"
        leopards = self.team(self.boss, self.a, "Leopards")
        mid = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": "now"}).json()["challenge"]["match"]["id"]
        murl = f"/api/orgs/{self.a}/matches/{mid}"
        self.assertEqual(self.boss.call("patch", murl, {"decided": "forfeit"}).status_code, 400, "needs the score that stands")
        self.assertEqual(self.boss.call("patch", murl, {"decided": "nonsense", "homeScore": 1, "awayScore": 0}).status_code, 400)
        # no screenshots needed: the organizer just records it
        r = self.boss.call("patch", murl, {"homeScore": 3, "awayScore": 0, "status": "finished", "decided": "disconnect", "notes": "Leopards lost connection at 70'"})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual((r.json()["match"]["decided"], r.json()["match"]["homeScore"]), ("disconnect", 3))
        page = Client().get(f"/match/{Match.objects.get(id=mid).slug}").content.decode()
        self.assertIn("Connection dropped", page)
        self.assertIn("Leopards lost connection", page)
        # a forfeit doesn't move the rankings; a connection-dropped win does
        store.update("rankings", {"enabled": True, "friendly_weight": "1"})
        lions = lambda: next((round(t["rating"]) for t in rankings.compute()["teams"] if t["team"].name == "Lions"), None)
        self.assertGreater(lions(), 1500)
        self.boss.call("patch", murl, {"decided": "forfeit"})
        self.assertIsNone(lions())
        # abandoned: no result
        r = self.boss.call("patch", murl, {"decided": "abandoned"}).json()["match"]
        self.assertEqual((r["status"], r["homeScore"], r["decided"]), ("cancelled", None, "abandoned"))
        # changing the score later without saying how it ended keeps it a normal result
        r = self.boss.call("patch", murl, {"homeScore": 2, "awayScore": 2, "status": "finished", "decided": ""}).json()["match"]
        self.assertEqual((r["decided"], r["homeScore"]), (None, 2))

    def test_challenge_an_organization_they_choose_the_team(self):
        url, theirs = f"/api/orgs/{self.a}/friendlies", f"/api/orgs/{self.b}/friendlies"
        found = self.boss.get("/api/friendly-teams?q=kumasi").json()
        self.assertEqual([(o["name"], o["teams"]) for o in found["orgs"]], [("Kumasi Cup", 1)])
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toOrg": "nowhere", "kickoff": self.soon()}).status_code, 404)
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toOrg": self.a, "kickoff": self.soon()}).status_code, 404, "not your own")
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toOrg": self.b, "kickoff": self.soon()}).json()["challenge"]
        self.assertEqual((ch["to"]["choice"], ch["to"]["org"]["name"], ch["status"]), (True, "Kumasi Cup", "pending"))
        self.assertEqual(self.boss.call("post", url, {"fromTeamId": self.lions, "toOrg": self.b, "kickoff": self.soon(2)}).status_code, 409)
        mine = self.chief.get(theirs).json()["challenges"]
        self.assertEqual([(c["direction"], c["canAnswer"]) for c in mine], [("received", True)])
        self.assertEqual(self.chief.call("post", f"{theirs}/{ch['id']}/accept", {}).status_code, 400, "must choose a team")
        hawks = self.team(self.chief, self.b, "Hawks")
        r = self.chief.call("post", f"{theirs}/{ch['id']}/accept", {"teamId": hawks})
        self.assertEqual(r.status_code, 200, r.json())
        m = Match.objects.get(id=r.json()["challenge"]["match"]["id"])
        self.assertEqual((m.home.team.name, m.away.team.name), ("Lions", "Hawks"))
        self.assertEqual(self.boss.get(url).json()["challenges"][0]["to"]["name"], "Hawks")

    def test_friendlies_stay_out_of_standings(self):
        leopards = self.team(self.boss, self.a, "Leopards")
        mid = self.boss.call("post", f"/api/orgs/{self.a}/friendlies", {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": "now"}).json()["challenge"]["match"]["id"]
        self.boss.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": 1, "awayScore": 0, "status": "finished"})
        m = Match.objects.get(id=mid)
        self.assertNotIn('class="st', Client().get(f"/match/{m.slug}").content.decode(), "no table on a friendly's match page")
        org_page = Client().get(f"/org/{self.a}").content.decode()
        self.assertNotIn('id="standings"', org_page, "the organization page has no standings for friendlies")

    def test_friendlies_on_team_pages(self):
        url = f"/api/orgs/{self.a}/friendlies"
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        mid = self.chief.call("post", f"/api/orgs/{self.b}/friendlies/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        self.boss.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": 1, "awayScore": 2, "status": "finished"})
        # the other organization's dashboard shows it on its team
        fr = self.chief.get(f"/api/orgs/{self.b}/teams/{self.tigers}").json()["team"]["friendlies"]
        self.assertEqual([(f["opponent"]["name"], f["mine"], f["theirs"], f["home"]) for f in fr], [("Lions", 2, 1, False)])
        # the public team page: its own Friendlies section, and it doesn't count in the form
        from .models import Team
        page = Client().get(f"/team/{Team.objects.get(id=self.tigers).slug}").content.decode()
        self.assertIn("Friendlies</div>", page)
        self.assertIn("1</b> won", page)
        self.assertNotIn('class="fm W"', page, "not in the league form")

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
