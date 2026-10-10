"""Platform rankings: Elo ratings across organizations, the on/off switch and organizations opting out."""
from django.test import Client, TestCase

from league.models import Admin
from orgs.tests import Helpers

from . import rankings
from .models import Match


class RankingsTest(Helpers, TestCase):
    def setUp(self):
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        rankings._cache.update(key=None, data=None)
        self.a, self.b = self.signup("boss"), self.signup("other")
        self.sa, self.sb = self.new_org(self.a, "Kasoa League"), self.new_org(self.b, "Accra League")

    def league(self, owner, slug, names, visibility="public"):
        base = f"/api/orgs/{slug}"
        cs = owner.call("post", base + "/competitions", {"name": f"{slug} cup", "visibility": visibility}).json()["competition"]["slug"]
        ids = [owner.call("post", base + "/teams", {"name": n}).json()["team"]["id"] for n in names]
        owner.call("post", f"{base}/competitions/{cs}/entries", {"teamIds": ids})
        owner.call("post", f"{base}/competitions/{cs}/generate", {"start": "2026-01-01T15:00:00Z"})
        return base, cs

    def play(self, owner, base, cs, winner):
        """Everyone plays everyone once; `winner` wins all its games 3-0, the rest draw 1-1."""
        for m in Match.objects.filter(competition__slug=cs).select_related("home__team", "away__team"):
            h, a = m.home.team.name, m.away.team.name
            score = (3, 0) if h == winner else (0, 3) if a == winner else (1, 1)
            owner.call("patch", f"{base}/matches/{m.id}", {"homeScore": score[0], "awayScore": score[1]})

    def test_rankings(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers", "Bears"])
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks", "Owls"])
        self.play(self.a, b1, c1, "Lions")
        self.play(self.b, b2, c2, "Eagles")
        self.assertEqual(Client().get("/rankings").status_code, 404, "off until a super admin switches it on")
        self.assertEqual(self.root.get("/rankings").status_code, 200, "super admins can preview")
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 2}})
        html = Client().get("/rankings").content.decode()
        self.assertIn('href="/rankings"', html, "in the menu once on")
        for team in ("Lions", "Eagles", "Owls"):
            self.assertIn(team, html, "teams from every organization")
        data = rankings.compute()
        top = [r["team"].name for r in data["teams"][:2]]
        self.assertEqual(sorted(top), ["Eagles", "Lions"], "unbeaten teams on top")
        self.assertGreater(data["teams"][0]["rating"], 1500)
        self.assertLess(data["teams"][-1]["rating"], 1500)
        self.assertAlmostEqual(sum(r["rating"] for r in data["teams"]), 1500 * 6, places=6, msg="points move, never appear")
        team_page = Client().get(f"/team/{data['teams'][0]['team'].slug}").content.decode()
        self.assertIn("Ranked #1 on the platform", team_page)
        # an organization can keep its teams out; private competitions never count
        self.b.call("patch", f"/api/orgs/{self.sb}", {"inRankings": False})
        html = Client().get("/rankings").content.decode()
        self.assertNotIn("Eagles", html)
        self.assertIn("Lions", html)
        b3, c3 = self.league(self.a, self.sa, ["Secret A", "Secret B"], visibility="private")
        self.play(self.a, b3, c3, "Secret A")
        self.assertNotIn("Secret A", Client().get("/rankings").content.decode())
        self.assertEqual(self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"min_matches": 0}}).status_code, 400)
        self.assertEqual(Client().get("/rankings/players").status_code, 200)

    def test_player_of_the_year_shows_team_and_organization(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers"])
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks"])
        homes = []
        for owner, base, cs, goals in ((self.a, b1, c1, 3), (self.b, b2, c2, 2)):
            m = Match.objects.filter(competition__slug=cs).select_related("home__team").first()
            homes.append(m.home.team.name)
            owner.call("patch", f"{base}/matches/{m.id}", {"homeScore": goals, "awayScore": 0})
            for minute in range(goals):                    # the same name in both organizations
                owner.call("post", f"{base}/matches/{m.id}/events", {"kind": "goal", "side": "home", "minute": 10 + minute, "playerName": "Kofi Mensah"})
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        data = rankings.compute()
        self.assertEqual([(p["name"], p["team"].name, p["goals"]) for p in data["year_players"]], [("Kofi Mensah", homes[0], 3), ("Kofi Mensah", homes[1], 2)])
        html = Client().get("/rankings/players").content.decode()
        self.assertIn(f"Player of the Year {data['year']}", html)
        podium = html[html.index('class="podium"'):html.index("</section>", html.index('class="podium"'))]
        self.assertLess(podium.index("Kasoa League"), podium.index("Accra League"))
        self.assertEqual(podium.count("Kofi Mensah"), 2, "two players with the same name, told apart by team and organization")
        for name in homes:
            self.assertIn(name, podium)

    def test_margin_and_penalties(self):
        self.assertEqual((rankings.margin_factor(1), rankings.margin_factor(2), rankings.margin_factor(3)), (1.0, 1.5, 1.75))

    def test_rankings_by_country_and_region(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers"])          # Kasoa League: Central, Ghana
        self.b.call("patch", f"/api/orgs/{self.sb}", {"country": "Nigeria", "region": "Lagos"})
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks"])
        self.play(self.a, b1, c1, "Lions")
        self.play(self.b, b2, c2, "Eagles")
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        world = Client().get("/rankings").content.decode()
        self.assertIn("Worldwide", world)
        self.assertTrue("Lions" in world and "Eagles" in world)
        ng = Client().get("/rankings?country=Nigeria").content.decode()
        self.assertIn("Eagles", ng)
        self.assertNotIn("Lions", ng)
        lagos = Client().get("/rankings?country=Nigeria&region=Lagos").content.decode()
        self.assertIn("Lagos, Nigeria", lagos)
        self.assertIn("Eagles", lagos)
        self.assertNotIn("Eagles", Client().get("/rankings?country=Nigeria&region=Abuja").content.decode())
