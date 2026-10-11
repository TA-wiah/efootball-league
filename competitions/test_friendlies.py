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
        self.assertIn("Friendly table", page, "a friendly table (no points) instead of a league table")
        table = Client().get(f"/competition/{m.competition.slug}/table").content.decode()
        self.assertIn("Win %", table)
        self.assertIn("<th>Pts</th>", table)
        self.assertIn("100%", table, "Lions won their only friendly")

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
        # accepted: the other team can't delete it any more; the hosting organization can, match and all
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        mid = self.chief.call("post", f"{theirs}/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        series = Match.objects.get(id=mid).competition
        self.assertEqual(self.chief.call("post", f"{theirs}/{ch['id']}/delete").status_code, 403)
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/delete").status_code, 200)
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

    def test_edit(self):
        url, theirs = f"/api/orgs/{self.a}/friendlies", f"/api/orgs/{self.b}/friendlies"
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        self.assertTrue(ch["canEdit"])
        self.assertFalse(self.chief.get(theirs).json()["challenges"][0]["canEdit"], "waiting: only the sender edits it")
        self.assertEqual(self.chief.call("post", f"{theirs}/{ch['id']}/edit", {"message": "x"}).status_code, 403)
        later = (timezone.now() + timedelta(days=5)).replace(microsecond=0)
        r = self.boss.call("post", f"{url}/{ch['id']}/edit", {"kickoff": later.isoformat(), "message": "Moved to Friday"}).json()["challenge"]
        self.assertEqual((r["message"], r["kickoff"][:16]), ("Moved to Friday", later.isoformat()[:16]))
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/edit", {"kickoff": (timezone.now() - timedelta(days=1)).isoformat()}).status_code, 400)
        # accepted: only the hosting organization moves the time, and the match moves with it
        mid = self.chief.call("post", f"{theirs}/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        sooner = (timezone.now() + timedelta(days=2)).replace(microsecond=0)
        self.assertEqual(self.chief.call("post", f"{theirs}/{ch['id']}/edit", {"kickoff": sooner.isoformat()}).status_code, 403)
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/edit", {"kickoff": sooner.isoformat()}).status_code, 200)
        self.assertEqual(Match.objects.get(id=mid).kickoff, sooner)
        # played: no more editing (the result is changed with Enter result)
        self.boss.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": 1, "awayScore": 1, "status": "finished"})
        self.assertEqual(self.boss.call("post", f"{url}/{ch['id']}/edit", {"message": "late"}).status_code, 403)

    def test_a_player_sees_only_their_teams_friendlies(self):
        url = f"/api/orgs/{self.a}/friendlies"
        leopards, bulls = self.team(self.boss, self.a, "Leopards"), self.team(self.boss, self.a, "Bulls")
        self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": self.soon()})
        self.boss.call("post", url, {"fromTeamId": bulls, "toTeamId": leopards, "kickoff": self.soon(2)})
        kofi = self.signup("kofi_p")
        link = self.boss.call("post", f"/api/orgs/{self.a}/teams/{self.lions}/invitations", {"role": "player"}).json()["link"]
        kofi.call("post", "/api/invitations/" + link.rsplit("/", 1)[1] + "/accept")
        mine = kofi.get(url).json()
        self.assertEqual([(c["from"]["name"], c["to"]["name"]) for c in mine["challenges"]], [("Lions", "Leopards")])
        self.assertEqual(mine["teams"], [], "a player can't send challenges for the club")
        self.assertEqual(len(self.boss.get(url).json()["challenges"]), 2, "the owner sees them all")

    def test_teams_submit_results_with_screenshots_organization_confirms(self):
        url, theirs = f"/api/orgs/{self.a}/friendlies", f"/api/orgs/{self.b}/friendlies"
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon()}).json()["challenge"]
        mid = self.chief.call("post", f"{theirs}/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        tm = self.signup("tm_tigers")                     # a team manager of Tigers sends their result
        link = self.chief.call("post", f"/api/orgs/{self.b}/teams/{self.tigers}/invitations", {"role": "team_manager"}).json()["link"]
        tm.call("post", "/api/invitations/" + link.rsplit("/", 1)[1] + "/accept")
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        self.assertEqual(tm.call("post", f"/api/report/{mid}/proof", {"kind": "result", "image": png}).status_code, 400, "the score is needed")
        self.assertEqual(tm.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": 0, "awayScore": 5}).status_code, 404, "no direct editing")
        r = tm.call("post", f"/api/report/{mid}/proof", {"kind": "result", "image": png, "homeScore": 1, "awayScore": 2})
        self.assertEqual(r.status_code, 200, r.json())
        claim = r.json()["claims"][0]
        self.assertEqual(tm.call("post", f"/api/report/{mid}/approve", {"proofId": claim["id"]}).status_code, 403, "teams can't confirm")
        waiting = self.boss.get(f"/api/orgs/{self.a}/pending-results").json()["matches"]
        self.assertEqual([(x["id"], x["claims"][0]["awayScore"]) for x in waiting], [(mid, 2)])
        r = self.boss.call("post", f"/api/report/{mid}/approve", {"proofId": claim["id"]}).json()
        self.assertEqual((r["match"]["homeScore"], r["match"]["awayScore"], r["match"]["status"], r["claims"]), (1, 2, "finished", []))
        self.assertEqual(self.boss.get(f"/api/orgs/{self.a}/pending-results").json()["matches"], [])

    def test_find_a_friendly_board(self):
        board = f"/api/orgs/{self.a}/friendly-board"
        leopards = self.team(self.boss, self.a, "Leopards")
        def join(name, team, role):
            who = self.signup(name)
            link = self.boss.call("post", f"/api/orgs/{self.a}/teams/{team}/invitations", {"role": role}).json()["link"]
            who.call("post", "/api/invitations/" + link.rsplit("/", 1)[1] + "/accept")
            return who
        p1, p2 = join("lion_player", self.lions, "player"), join("leo_coach", leopards, "coach")
        outsider = self.signup("fan_viewer")
        self.invite_and_join(self.boss, self.a, outsider, "viewer")
        self.assertEqual(p1.call("post", board, {"teamId": leopards, "kickoff": "now"}).status_code, 403, "only your own team")
        r = p1.call("post", board, {"teamId": self.lions, "kickoff": self.soon(), "message": "Anyone tonight?"})
        self.assertEqual(r.status_code, 200, r.json())
        rid = r.json()["request"]["id"]
        seen = p2.get(board).json()
        self.assertEqual([(x["team"]["name"], x["mine"]) for x in seen["open"]], [("Lions", False)])
        self.assertEqual(self.boss.get(f"/api/orgs/{self.a}/friendlies").json()["challenges"], [], "open requests stay on the board")
        self.assertEqual(p2.call("post", f"{board}/{rid}/take", {"teamId": self.lions}).status_code, 400, "not with the team that posted it")
        self.assertEqual(p2.call("post", f"{board}/{rid}/cancel").status_code, 403)
        r = p2.call("post", f"{board}/{rid}/take", {"teamId": leopards})
        self.assertEqual(r.status_code, 200, r.json())
        m = Match.objects.get(id=r.json()["match"]["id"])
        self.assertEqual((m.home.team.name, m.away.team.name, m.competition.kind), ("Lions", "Leopards", "friendly"))
        self.assertEqual(p1.call("post", f"{board}/{rid}/take", {"teamId": self.lions}).status_code, 404, "taken already")
        self.assertEqual([u["away"] for u in p1.get(board).json()["upcoming"]], ["Leopards"])
        # results: only sent with a screenshot, then confirmed by the organization
        self.assertEqual(p1.call("patch", f"/api/orgs/{self.a}/matches/{m.id}", {"homeScore": 9, "awayScore": 0}).status_code, 403)
        self.assertEqual(outsider.call("post", board, {"teamId": leopards, "kickoff": "now"}).status_code, 403, "not in a team: can look, not post")
        self.assertEqual(len(outsider.get(board).json()["open"]), 0)

    def test_friendly_table_has_points(self):
        from . import engine
        leopards = self.team(self.boss, self.a, "Leopards")
        url = f"/api/orgs/{self.a}/friendlies"
        for score in ((2, 0), (1, 1)):
            mid = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": leopards, "kickoff": "now"}).json()["challenge"]["match"]["id"]
            self.boss.call("patch", f"/api/orgs/{self.a}/matches/{mid}", {"homeScore": score[0], "awayScore": score[1], "status": "finished"})
        c = Match.objects.get(id=mid).competition
        rows = {r["team"].name: (r["points"], r["position"]) for r in engine.friendly_table(c.matches.select_related("home__team", "away__team"), c)}
        self.assertEqual(rows, {"Lions": (4, 1), "Leopards": (1, 2)})
        self.assertIn("<th>Pts</th>", Client().get(f"/competition/{c.slug}/table").content.decode())

    def join_team(self, name, team, role, email=None):
        who = self.signup(name, email)
        link = self.boss.call("post", f"/api/orgs/{self.a}/teams/{team}/invitations", {"role": role}).json()["link"]
        who.call("post", "/api/invitations/" + link.rsplit("/", 1)[1] + "/accept")
        return who

    def test_find_a_friendly_challenge_a_team_they_approve_everyone_emailed(self):
        from unittest import mock
        from league import emailer
        board = f"/api/orgs/{self.a}/friendly-board"
        leopards = self.team(self.boss, self.a, "Leopards")
        p1 = self.join_team("lion_p", self.lions, "player")
        p2 = self.join_team("leo_p", leopards, "player", "leo_p@example.com")
        teams = {t["name"]: t for t in p1.get(board).json()["orgTeams"]}
        self.assertEqual((teams["Lions"]["mine"], teams["Leopards"]["mine"]), (True, False))
        mails = []
        with mock.patch.object(emailer, "ready", lambda cfg=None: True), mock.patch.object(emailer, "send", lambda to, subj, t, h: mails.append((to, subj))):
            r = p1.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon()})
            self.assertEqual(r.status_code, 200, r.json())
            self.assertIn(("leo_p@example.com", "Lions challenged Leopards to a friendly"), mails, "the other team's player is told")
            cid = r.json()["challenge"]["id"]
            self.assertEqual([x["to"]["name"] for x in p1.get(board).json()["outgoing"]], ["Leopards"])
            self.assertEqual(p1.call("post", f"{board}/{cid}/approve").status_code, 403, "only the challenged team answers")
            self.assertEqual([x["team"]["name"] for x in p2.get(board).json()["incoming"]], ["Lions"])
            r = p2.call("post", f"{board}/{cid}/approve")
        self.assertEqual(r.json()["status"], "accepted")
        m = Match.objects.get(id=r.json()["match"]["id"])
        self.assertEqual((m.home.team.name, m.away.team.name), ("Lions", "Leopards"))
        # decline and cancel
        cid = p1.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon(3)}).json()["challenge"]["id"]
        self.assertEqual(p2.call("post", f"{board}/{cid}/decline").json()["status"], "declined")
        cid = p1.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon(4)}).json()["challenge"]["id"]
        self.assertEqual(p1.call("post", f"{board}/{cid}/cancel").json()["status"], "cancelled")
        self.assertEqual(p1.call("post", board, {"teamId": self.lions, "toTeamId": self.lions, "kickoff": self.soon()}).status_code, 400)

    def test_match_reminders_email_and_sms(self):
        from unittest import mock
        from league import emailer
        from orgs.models import Organization
        from sms.models import SmsAccount, SmsMessage
        from . import reminders
        leopards = self.team(self.boss, self.a, "Leopards")
        p1 = self.join_team("lion_r", self.lions, "player", "lion_r@example.com")
        from league.models import Admin
        Admin.objects.filter(username="lion_r").update(phone="0241234567")
        mid = self.boss.call("post", f"/api/orgs/{self.a}/friendlies", {"fromTeamId": self.lions, "toTeamId": leopards,
                                                                       "kickoff": (timezone.now() + timedelta(hours=2)).isoformat()}).json()["challenge"]["match"]["id"]
        Organization.objects.filter(slug=self.a).update(auto_sms_reminders=True)
        SmsAccount.objects.update_or_create(org=Organization.objects.get(slug=self.a), defaults={"credits": 10})
        store.update("sms", {"enabled": True, "provider": "console", "sender": "League"})
        mails = []
        with mock.patch.object(emailer, "ready", lambda cfg=None: True), mock.patch.object(emailer, "send", lambda to, subj, t, h: mails.append((to, subj))):
            self.assertEqual(reminders.send_due(force=True), 1)
            self.assertEqual(reminders.send_due(force=True), 0, "once per match")
        self.assertIn(("lion_r@example.com", "Match reminder: Lions v Leopards"), mails)
        self.assertIsNotNone(Match.objects.get(id=mid).reminded)
        sms = SmsMessage.objects.filter(org__slug=self.a, kind="reminder")
        self.assertEqual(sms.count(), 1)
        self.assertIn("Lions v Leopards", sms[0].body)
        self.assertLess(SmsAccount.objects.get(org__slug=self.a).credits, 10, "the organization's credits pay for it")

    def test_friendly_notices_by_sms_when_turned_on(self):
        from league.models import Admin
        from orgs.models import Organization
        from sms.models import SmsAccount, SmsMessage
        board = f"/api/orgs/{self.a}/friendly-board"
        leopards = self.team(self.boss, self.a, "Leopards")
        p1 = self.join_team("lion_s", self.lions, "player")
        self.join_team("leo_s", leopards, "player")
        Admin.objects.filter(username__in=["lion_s", "leo_s"]).update(phone="0241234567")
        store.update("sms", {"enabled": True, "provider": "console", "sender": "League"})
        org = Organization.objects.get(slug=self.a)
        SmsAccount.objects.update_or_create(org=org, defaults={"credits": 20})
        first = p1.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon()}).json()["challenge"]["id"]
        self.assertEqual(SmsMessage.objects.filter(org=org).count(), 0, "off by default: emails only")
        p1.call("post", f"{board}/{first}/cancel")
        self.assertEqual(self.boss.call("patch", f"/api/orgs/{self.a}", {"autoSmsReminders": True}).json()["org"]["autoSmsReminders"], True)
        cid = p1.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon(3)}).json()["challenge"]["id"]
        texts = list(SmsMessage.objects.filter(org=org, kind="notice").values_list("body", flat=True))
        self.assertTrue(any("Lions challenged Leopards" in t for t in texts), texts)
        self.assertLess(SmsAccount.objects.get(org=org).credits, 20)
        # the Leopards player approves from their dashboard's data, and the Lions get a text
        leo = self.signup("leo_s2")
        link = self.boss.call("post", f"/api/orgs/{self.a}/teams/{leopards}/invitations", {"role": "team_manager"}).json()["link"]
        leo.call("post", "/api/invitations/" + link.rsplit("/", 1)[1] + "/accept")
        self.assertEqual([x["id"] for x in leo.get(board).json()["incoming"]], [cid])
        leo.call("post", f"{board}/{cid}/approve")
        self.assertTrue(SmsMessage.objects.filter(org=org, kind="notice", body__contains="Leopards accepted your friendly").exists())

    def test_who_is_told(self):
        from unittest import mock
        from league import emailer
        from league.models import Admin
        board = f"/api/orgs/{self.a}/friendly-board"
        leopards, bulls = self.team(self.boss, self.a, "Leopards"), self.team(self.boss, self.a, "Bulls")
        p1 = self.join_team("lion_w", self.lions, "player", "lion_w@example.com")
        self.join_team("leo_w", leopards, "player", "leo_w@example.com")
        self.join_team("bull_w", bulls, "player", "bull_w@example.com")
        self.join_team("bull_c", bulls, "coach", "bull_coach@example.com")
        self.boss.call("patch", f"/api/orgs/{self.a}", {"openToFriendlies": True})      # other organizations can find these teams
        mails = []
        def told(fn):
            mails.clear()
            with mock.patch.object(emailer, "ready", lambda cfg=None: True), mock.patch.object(emailer, "send", lambda to, subj, t, h: mails.append(to)):
                fn()
            return sorted(set(mails))
        # a challenge to a particular team: only that team
        self.assertEqual(told(lambda: p1.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon()})), ["leo_w@example.com"])
        # an open request: everyone on the other teams, and the managers (the owner); not the team that posted it
        self.assertEqual(told(lambda: p1.call("post", board, {"teamId": self.lions, "kickoff": self.soon(2)})),
                         sorted(["boss@example.com", "bull_coach@example.com", "bull_w@example.com", "leo_w@example.com"]))
        # a challenge from outside to a particular team: only that team; to the whole organization: everyone there
        self.assertEqual(told(lambda: self.chief.call("post", f"/api/orgs/{self.b}/friendlies", {"fromTeamId": self.tigers, "toTeamId": self.lions, "kickoff": self.soon()})),
                         ["lion_w@example.com"])
        everyone = told(lambda: self.chief.call("post", f"/api/orgs/{self.b}/friendlies", {"fromTeamId": self.tigers, "toOrg": self.a, "kickoff": self.soon(3)}))
        self.assertEqual(everyone, sorted(["boss@example.com", "bull_coach@example.com", "bull_w@example.com", "leo_w@example.com", "lion_w@example.com"]))

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
