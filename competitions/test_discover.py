"""Landing page, discovery and search: only public competitions are ever listed."""
import re

from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from orgs.tests import Helpers

from .models import Competition


class DiscoverTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner, "Kasoa Community League")
        self.base = f"/api/orgs/{self.slug}"
        self.make("Kasoa Sunday League", "public", ["Kasoa Stars", "Winneba Lions"], country="Ghana", region="Central", kind="league")
        self.make("Accra Corporate Cup", "public", ["Bank FC", "Telco United"], country="Ghana", region="Greater Accra", kind="cup")
        self.make("Lagos Schools Shield", "public", ["Kings College", "Queens College"], country="Nigeria", region="Lagos", kind="championship")
        self.make("Hidden Unlisted Cup", "unlisted", ["Alpha", "Beta"], country="Ghana")
        self.make("Secret Private League", "private", ["Gamma", "Delta"], country="Ghana")

    def make(self, name, visibility, teams, **extra):
        cs = self.owner.call("post", self.base + "/competitions", {"name": name, "visibility": visibility, **extra}).json()["competition"]["slug"]
        ids = [self.owner.call("post", self.base + "/teams", {"name": t}).json()["team"]["id"] for t in teams]
        self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": ids})
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {"start": "2099-10-11T15:00:00Z"})
        return cs

    def text(self, url):
        r = Client().get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.content.decode()

    def test_landing_explains_the_platform(self):
        html = self.text("/")
        for s in ("What the platform does", "Organizer log in", "Browse competitions", "League organizers", "Teams", "Fans",
                  "Automatic standings", "Kasoa Sunday League", "Browse by country", "Ghana"):
            self.assertIn(s, html)
        self.assertNotIn("/app/signup", html, "creating needs a login: no sign-up or create buttons on public pages")
        self.assertNotIn("/app/new", html)
        self.assertNotIn("Hidden Unlisted Cup", html)
        self.assertNotIn("Secret Private League", html)
        self.assertIn('href="/competitions"', html)

    def test_discovery_lists_only_public_and_filters(self):
        html = self.text("/competitions")
        self.assertIn("Lagos Schools Shield", html)
        self.assertNotIn("Hidden Unlisted Cup", html)
        self.assertNotIn("Secret Private League", html)
        ghana = self.text("/competitions?country=Ghana")
        self.assertIn("Accra Corporate Cup", ghana)
        self.assertNotIn("Lagos Schools Shield", ghana)
        self.assertIn("Greater Accra", ghana, "regions of the chosen country are offered")
        self.assertIn("Accra Corporate Cup", self.text("/competitions?type=cup"))
        self.assertNotIn("Kasoa Sunday League", self.text("/competitions?type=cup"))
        self.assertIn("Kasoa Sunday League", self.text("/competitions?q=kasoa"))
        self.assertIn("No competitions found", self.text("/competitions?q=zzzz"))
        self.assertIn("Kasoa Sunday League", self.text("/competitions?status=active"), "generating fixtures starts a competition")
        self.assertEqual(Client().get("/competitions?type=<script>&sort=bad&page=999").status_code, 200, "bad parameters are ignored")

    def test_featured_and_popular(self):
        call_command("feature", "lagos-schools-shield", stdout=open(__import__("os").devnull, "w"))
        html = self.text("/competitions")
        self.assertLess(html.index("Lagos Schools Shield"), html.index("Kasoa Sunday League"), "featured first")
        for _ in range(3):
            Client().get("/competition/accra-corporate-cup")
        self.assertEqual(Competition.objects.get(slug="accra-corporate-cup").views, 3)
        Client().get("/competition/hidden-unlisted-cup")
        self.assertEqual(Competition.objects.get(slug="hidden-unlisted-cup").views, 0, "only public pages count")
        pop = self.text("/competitions?sort=popular")
        self.assertLess(pop.index("Accra Corporate Cup"), pop.index("Kasoa Sunday League"))

    def test_search(self):
        html = self.text("/search?q=kasoa")
        self.assertIn("Kasoa Sunday League", html)
        self.assertIn("/team/kasoa-stars", html)
        self.assertIn("/organization/kasoa-community-league", html)
        vs = self.text("/search?q=Kasoa%20vs%20Winneba")
        self.assertTrue(re.search(r"/match/(kasoa-stars-vs-winneba-lions|winneba-lions-vs-kasoa-stars)", vs),
                        "finds the match whichever team was at home")
        for hidden in ("Hidden", "Secret", "Gamma", "Alpha"):
            page = self.text(f"/search?q={hidden}")
            self.assertIn("Nothing found", page, hidden)
        self.assertIn("at least 2 characters", self.text("/search?q=k"))
        self.assertEqual(Client().get("/search?q=kasoa")["X-Robots-Tag"], "noindex")

    def test_original_league_moves_to_classic_with_opt_out(self):
        self.assertIn("eFootball", self.text("/classic"))
        self.assertIn("What the platform does", self.text("/"))
        with override_settings(HOME_PAGE="league", LEAGUE_PATH="/"):
            self.assertIn("eFootball", self.text("/"))
        self.assertIn("/competitions", self.text("/sitemap.xml"))
