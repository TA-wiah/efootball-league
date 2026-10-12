"""The eFootball room code on friendly challenges and matches (only the two teams and staff see it), and the public
match page showing how a result was confirmed, waiting results, and missing scorers."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from orgs.tests import Helpers

from .models import Match, MatchEvent, MatchProof
from . import test_friendlies as tf

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class RoomCodeTest(Helpers, TestCase):
    setUp, team, soon = tf.FriendliesTest.setUp, tf.FriendliesTest.team, tf.FriendliesTest.soon     # the same two organizations

    def test_room_code_goes_to_the_two_teams_only(self):
        url, theirs = f"/api/orgs/{self.a}/friendlies", f"/api/orgs/{self.b}/friendlies"
        ch = self.boss.call("post", url, {"fromTeamId": self.lions, "toTeamId": self.tigers, "kickoff": self.soon(), "roomCode": "AB12-CD <x>"}).json()["challenge"]
        self.assertEqual(ch["roomCode"], "AB12-CD x", "letters, digits, spaces and dashes only")
        theirs_list = self.chief.get(theirs).json()["challenges"]
        self.assertEqual(theirs_list[0]["roomCode"], "AB12-CD x", "the challenged team sees it")
        mid = self.chief.call("post", f"{theirs}/{ch['id']}/accept").json()["challenge"]["match"]["id"]
        self.assertEqual(Match.objects.get(id=mid).room_code, "AB12-CD x", "carried onto the match")
        # either team changes it on the match page; outsiders can't see the match at all
        self.assertEqual(self.chief.get(f"/api/report/{mid}").json()["roomCode"], "AB12-CD x")
        self.assertEqual(self.chief.call("post", f"/api/report/{mid}/room", {"roomCode": "ZZ99"}).json()["roomCode"], "ZZ99")
        self.assertEqual(self.boss.get(f"/api/report/{mid}").json()["roomCode"], "ZZ99")
        stranger = self.signup("nosy")
        self.assertEqual(stranger.get(f"/api/report/{mid}").status_code, 404)
        self.assertEqual(stranger.call("post", f"/api/report/{mid}/room", {"roomCode": "HACK"}).status_code, 404)
        page = Client().get(f"/match/{Match.objects.get(id=mid).slug}").content.decode()
        self.assertNotIn("ZZ99", page, "never on public pages")

    def test_open_requests_never_show_the_code(self):
        board = f"/api/orgs/{self.a}/friendly-board"
        leopards = self.team(self.boss, self.a, "Leopards")
        self.boss.call("post", board, {"teamId": self.lions, "kickoff": self.soon(), "roomCode": "OPEN1"})
        self.boss.call("post", board, {"teamId": self.lions, "toTeamId": leopards, "kickoff": self.soon(4), "roomCode": "DIRECT1"})
        data = self.boss.get(board).json()
        self.assertTrue(all(x.get("roomCode") is None for x in data["open"]), "an open request is seen by every team")
        self.assertEqual([x["roomCode"] for x in data["outgoing"]], ["DIRECT1"])


class PublicTrustTest(Helpers, TestCase):
    def setUp(self):
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        base = f"/api/orgs/{self.slug}"
        cs = self.boss.call("post", base + "/competitions", {"name": "Open Cup", "visibility": "public"}).json()["competition"]["slug"]
        ids = [self.boss.call("post", base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Lions", "Tigers")]
        self.boss.call("post", f"{base}/competitions/{cs}/entries", {"teamIds": ids})
        self.boss.call("post", f"{base}/competitions/{cs}/generate", {"start": "2026-01-01T15:00:00Z"})
        self.m = Match.objects.filter(competition__slug=cs).first()

    def test_waiting_confirmed_and_missing_scorers(self):
        Match.objects.filter(id=self.m.id).update(kickoff=timezone.now() - timedelta(hours=5))
        page = Client().get(f"/match/{self.m.slug}").content.decode()
        self.assertIn("Awaiting result", page)
        self.assertIn("waiting for the teams to send the result", page)
        MatchProof.objects.create(match=self.m, side="home", kind="result", image=b"x", content_type="image/png", home_score=2, away_score=1)
        Match.objects.filter(id=self.m.id).update(home_score=2, away_score=1, status="finished")
        MatchEvent.objects.create(match=self.m, kind="goal", side="home", minute=10, player_name="Kofi Mensah")
        page = Client().get(f"/match/{self.m.slug}").content.decode()
        self.assertIn("Result confirmed by", page)
        self.assertIn("backed by a result screenshot", page)
        self.assertIn("2 goals without a name", page)
        self.assertNotIn("Awaiting result", page)
        self.assertIn('<h1 class="sr">', page, "the match page has a real heading")
