"""Text written by the text AI (stand-in answers here; tests never call the real services): match reports, round-ups,
competition descriptions, WhatsApp share lines and lineup flyer captions, with built-in wording when the AI is away."""
from unittest import mock

from django.test import Client, TestCase

from orgs.tests import Helpers

from . import writer
from .models import Match, MatchEvent


class WriterTest(Helpers, TestCase):
    def setUp(self):
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        self.base = f"/api/orgs/{self.slug}"
        ids = [self.boss.call("post", self.base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Lions", "Tigers")]
        self.cs = self.boss.call("post", self.base + "/competitions", {"name": "Open Cup", "visibility": "public"}).json()["competition"]["slug"]
        self.boss.call("post", f"{self.base}/competitions/{self.cs}/entries", {"teamIds": ids})
        self.boss.call("post", f"{self.base}/competitions/{self.cs}/generate", {"start": "2026-01-01T15:00:00Z"})
        self.m = Match.objects.select_related("home__team", "away__team").filter(competition__slug=self.cs).first()
        self.tid = ids[0]
        self.h, self.a = self.m.home.team.name, self.m.away.team.name     # the draw decides who is at home
        from django.core.cache import cache
        cache.clear()

    def finish(self):
        MatchEvent.objects.create(match=self.m, kind="goal", side="home", minute=12, player_name="Kofi Mensah")
        MatchEvent.objects.create(match=self.m, kind="goal", side="home", minute=70, player_name="Kofi Mensah")
        return self.boss.call("patch", f"{self.base}/matches/{self.m.id}", {"homeScore": 2, "awayScore": 0})

    def test_report_written_when_the_result_is_entered(self):
        self.finish()                                           # offline in tests: our own words
        m = Match.objects.get(id=self.m.id)
        self.assertEqual(m.report_by, "built-in")
        self.assertIn("beat", m.report)
        self.assertIn("Kofi Mensah (2) scored", m.report)
        page = Client().get(f"/match/{m.slug}").content.decode()
        self.assertIn("Match report", page)
        # written by Aries when it answers; an edited report is never overwritten by the automatic one
        with mock.patch.object(writer, "ask_aries", lambda prompt, model: "Lions cruised past Tigers 2-0 with a Kofi Mensah double. 🔥"):
            r = self.boss.call("post", f"{self.base}/matches/{m.id}/report-text").json()
        self.assertEqual((r["by"], r["report"]), ("aries", "Lions cruised past Tigers 2-0 with a Kofi Mensah double."))
        self.boss.call("patch", f"{self.base}/matches/{m.id}/report-text", {"text": "Our own words."})
        writer.write_report(Match.objects.get(id=m.id))
        self.assertEqual(Match.objects.get(id=m.id).report, "Our own words.")
        self.assertEqual(self.boss.get(f"{self.base}/matches/{m.id}").json()["match"]["report"], "Our own words.")

    def test_facts_only_and_share_line(self):
        self.finish()
        prompts = []
        with mock.patch.object(writer, "ask_aries", lambda prompt, model: prompts.append(prompt) or "A clean sheet and a double for the Lions."):
            writer.write_report(Match.objects.get(id=self.m.id), force=True)
        self.assertIn(f"{self.h} 2-0 {self.a}", prompts[0])
        self.assertIn(f"Kofi Mensah 12 min for {self.h}", prompts[0])
        self.assertIn("never invent", prompts[0])
        r = Client().get(f"/api/share-text?path=/match/{self.m.slug}").json()
        self.assertEqual(r["text"], "A clean sheet and a double for the Lions.", "match pages share their report's first line")

    def test_roundup_description_and_lineup_caption(self):
        self.finish()
        with mock.patch.object(writer, "ask_aries", lambda prompt, model: "Lions lead the way after a 2-0 win over Tigers."):
            r = self.boss.call("post", f"{self.base}/roundup", {"competition": self.cs}).json()
            self.assertEqual(r["body"], "Lions lead the way after a 2-0 win over Tigers.")
            d = self.boss.call("post", f"{self.base}/describe", {"name": "Open Cup", "kind": "Cup", "format": "Knockout"}).json()
            self.assertEqual(d["by"], "aries")
            c = self.boss.call("post", f"{self.base}/teams/{self.tid}/lineup-caption").json()
            self.assertEqual(c["caption"], "Lions lead the way after a 2-0 win over Tigers.")
        r = self.boss.call("post", f"{self.base}/roundup", {"competition": self.cs}).json()
        self.assertIn(f"Results: {self.h} 2-0 {self.a}", r["body"], "offline: built-in round-up")
        self.assertEqual(self.boss.call("post", f"{self.base}/roundup", {"competition": "nope"}).status_code, 400)
        player = self.signup("ama")
        self.invite_and_join(self.boss, self.slug, player, "player")
        self.assertEqual(player.call("post", f"{self.base}/roundup", {"competition": self.cs}).status_code, 403, "staff only")
