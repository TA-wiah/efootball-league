"""Flyers for quarter-finals, semi-finals and finals; captions written by Aries (text AI) or built in."""
import urllib.error
from unittest import mock

from django.test import TestCase

from orgs.models import Membership
from orgs.tests import Helpers
from superadmin import store

from . import flyers
from .models import Competition, Entry, Match, Team


class FlyersTest(Helpers, TestCase):
    def setUp(self):
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        base = f"/api/orgs/{self.slug}"
        ids = [self.boss.call("post", base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Lions", "Tigers", "Bears", "Owls")]
        cs = self.boss.call("post", base + "/competitions", {"name": "Champions Cup"}).json()["competition"]["slug"]
        c = Competition.objects.get(slug=cs)
        e = {t: Entry.objects.create(competition=c, team_id=t) for t in ids}
        mk = lambda h, a, name, rnd: Match.objects.create(competition=c, stage="knockout", round=rnd, round_name=name, home=e[h], away=e[a],
                                                          slug=f"m-{h}-{a}-{rnd}")
        self.final = mk(ids[0], ids[1], "Final", 2)
        self.semi = mk(ids[2], ids[3], "Semi-finals", 1)
        Match.objects.create(competition=c, stage="knockout", round=0, round_name="Round of 16", home=e[ids[0]], away=e[ids[2]], slug="r16")
        self.ids = ids

    def test_big_matches_only_and_team_members_see_their_own(self):
        r = self.boss.get(f"/api/orgs/{self.slug}/flyers").json()
        self.assertEqual(sorted(f["stage"] for f in r["flyers"]), ["Grand Final", "Semi-final"], "no flyer for the round of 16")
        pl = self.signup("ama")
        self.invite_and_join(self.boss, self.slug, pl, "player")
        Membership.objects.get(user__username="ama").teams.add(Team.objects.get(id=self.ids[2]))
        mine = pl.get(f"/api/orgs/{self.slug}/flyers").json()["flyers"]
        self.assertEqual([f["stage"] for f in mine], ["Semi-final"], "a player sees their own team's")
        self.assertEqual(self.signup("nosy").get(f"/api/orgs/{self.slug}/flyers").status_code, 404)

    def test_caption_from_aries_cleaned_or_built_in(self):
        url = f"/api/orgs/{self.slug}/flyers/{self.final.id}/caption"
        with mock.patch.object(flyers, "ask_aries", lambda prompt, model: "🔥 Lions vs Tigers: one night, one trophy! #final"):
            r = self.boss.call("post", url).json()
        self.assertEqual(r["by"], "aries")
        self.assertEqual(r["caption"], "Lions vs Tigers: one night, one trophy! final", "no emojis or hashtags")
        self.assertEqual(r["headline"], "Grand Final")
        def down(prompt, model):
            raise urllib.error.URLError("down")
        with mock.patch.object(flyers, "ask_aries", down):
            r = self.boss.call("post", url).json()
        self.assertEqual((r["by"], r["caption"]), ("built-in", "One match. One trophy. Who lifts it?"))
        store.update("ai", {"text": "off"})
        with mock.patch.object(flyers, "ask_aries", lambda prompt, model: self.fail("Aries is off")):
            self.assertEqual(self.boss.call("post", url).json()["by"], "built-in")
        self.assertEqual(self.boss.call("post", f"/api/orgs/{self.slug}/flyers/999999/caption").status_code, 404)

    def test_aries_is_the_default_text_ai(self):
        self.assertEqual(flyers.text_ai(), {"provider": "aries", "model": "aries-1.3", "key": ""})

    def test_champions_and_winner_on_penalties(self):
        Match.objects.filter(id=self.final.id).update(home_score=1, away_score=1, home_pens=3, away_pens=4, status="finished")
        f = {x["id"]: x for x in self.boss.get(f"/api/orgs/{self.slug}/flyers").json()["flyers"]}[self.final.id]
        self.assertEqual(f["winner"], "away", "won on penalties")
        store.update("ai", {"text": "off"})
        r = self.boss.call("post", f"/api/orgs/{self.slug}/flyers/{self.final.id}/caption").json()
        self.assertEqual(r["headline"], "Champions")

    def test_grok_writes_only_with_a_key(self):
        store.update("ai", {"text": "grok"})
        self.assertEqual(flyers.text_ai()["provider"], "aries", "no Grok key saved: Aries writes")
        store.update("ai", {"grok_key": "xai-test"})
        self.assertEqual(flyers.text_ai()["provider"], "grok")
        with mock.patch.object(flyers, "ask_grok", lambda prompt, model, key: "One last step to glory."):
            r = self.boss.call("post", f"/api/orgs/{self.slug}/flyers/{self.semi.id}/caption").json()
        self.assertEqual((r["by"], r["caption"]), ("grok", "One last step to glory."))
        with mock.patch.object(flyers, "ask_grok", lambda prompt, model, key: "Aries neural service is currently under high load."):
            self.assertEqual(self.boss.call("post", f"/api/orgs/{self.slug}/flyers/{self.semi.id}/caption").json()["by"], "built-in")
