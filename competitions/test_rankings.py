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
        self.assertIn("Ranked #1 in the world", team_page)
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

    def test_awards_with_names_set_by_the_super_admin(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers"])
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks"])
        self.play(self.a, b1, c1, "Lions")
        self.play(self.b, b2, c2, "Eagles")
        m = Match.objects.filter(competition__slug=c1).select_related("home__team").first()
        self.a.call("post", f"{b1}/matches/{m.id}/events", {"kind": "goal", "side": "home", "minute": 5, "playerName": "Kofi Mensah", "assistName": "Ama Owusu"})
        self.a.call("post", f"{b1}/matches/{m.id}/events", {"kind": "red", "side": "home", "minute": 50, "playerName": "Yaw"})
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1, "award_poty": "Golden Ball"}})
        self.assertEqual(self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"award_boot": "x"}}).status_code, 400)
        html = Client().get("/rankings/awards").content.decode()
        for text in ("Golden Ball", "Golden Boot", "Best Playmaker", "Best Defence", "Team of the Year", "Fair Play Award", "Kofi Mensah", "Ama Owusu", "Kasoa League"):
            self.assertIn(text, html)
        self.assertIn("Golden Ball", Client().get("/rankings/players").content.decode(), "the podium uses the same name")
        a = {x["key"]: x for x in rankings.awards(lambda o: True, 1)["awards"]}
        self.assertEqual((a["poty"]["who"], a["assists"]["who"], a["boot"]["team"].name), ("Kofi Mensah", "Ama Owusu", m.home.team.name))
        self.assertNotEqual(a["fairplay"]["team"].name, m.home.team.name, "the team with the red card doesn't win fair play")

    def test_world_country_and_region_places_on_the_dashboard(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers"])          # Kasoa League: Central, Ghana
        self.b.call("patch", f"/api/orgs/{self.sb}", {"country": "Nigeria", "region": "Lagos"})
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks"])
        self.play(self.a, b1, c1, "Lions")
        self.play(self.b, b2, c2, "Eagles")
        self.assertEqual(self.a.get(f"/api/orgs/{self.sa}/rankings").json(), {"enabled": False})
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        rows = {r["name"]: r for r in self.a.get(f"/api/orgs/{self.sa}/rankings").json()["teams"]}
        self.assertEqual(set(rows), {"Lions", "Tigers"}, "only this organization's teams")
        lions = rows["Lions"]
        self.assertIn(lions["world"], (1, 2))
        self.assertEqual((lions["inCountry"], lions["country"], lions["inRegion"], lions["region"]), (1, "Ghana", 1, "Central"))
        self.assertEqual(rows["Tigers"]["inCountry"], 2)
        page = Client().get(f"/team/{Match.objects.filter(competition__slug=c1).first().competition.entries.get(team__name='Lions').team.slug}").content.decode()
        self.assertIn("in the world · #1 in Ghana · #1 in Central", page)
        self.assertIn("How the rankings work", Client().get("/rankings").content.decode())
        m = Match.objects.filter(competition__slug=c1).select_related("home__team").first()
        self.a.call("post", f"{b1}/matches/{m.id}/events", {"kind": "goal", "side": "home", "minute": 9, "playerName": "Kofi Mensah"})
        mine = self.a.get(f"/api/orgs/{self.sa}/rankings").json()["players"]
        self.assertEqual([(p["name"], p["team"]["name"], p["goals"], p["world"], p["inCountry"]) for p in mine], [("Kofi Mensah", m.home.team.name, 1, 1, 1)])
        self.assertEqual(self.b.get(f"/api/orgs/{self.sb}/rankings").json()["players"], [], "only your own organization's players")

    def test_team_awards_follow_the_rankings(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers", "Bears"])
        self.play(self.a, b1, c1, "Lions")
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        a = {x["key"]: x for x in rankings.awards(lambda o: True, 1)["awards"]}
        top = rankings.compute()["teams"][0]["team"].name
        self.assertEqual((a["team"]["who"], top), ("Lions", "Lions"), "Team of the Year is #1 in the team rankings")
        self.assertNotIn("fairplay", a, "no cards recorded: no Fair Play award")
        m = Match.objects.filter(competition__slug=c1).first()
        self.a.call("post", f"{b1}/matches/{m.id}/events", {"kind": "yellow", "side": "home", "minute": 30, "playerName": "Yaw"})
        self.assertIn("fairplay", {x["key"] for x in rankings.awards(lambda o: True, 1)["awards"]}, "with cards recorded it appears")
        # a single team with enough matches doesn't win Best Defence by default
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks"])
        a = {x["key"]: x for x in rankings.awards(lambda o: o.slug == self.sb, 99)["awards"]}
        self.assertNotIn("defence", a)

    def test_fair_play_counts_cards_correctly(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers", "Bears"])
        self.play(self.a, b1, c1, "Lions")
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        ms = list(Match.objects.filter(competition__slug=c1).select_related("home__team", "away__team"))
        card = lambda m, side, kind: self.a.call("post", f"{b1}/matches/{m.id}/events", {"kind": kind, "side": side, "minute": 50})
        # Lions: a red card (3); Tigers: two yellows (2); Bears: one yellow (1) -> Bears win Fair Play
        for m in ms:
            names = {"home": m.home.team.name, "away": m.away.team.name}
            for side, team in names.items():
                if team == "Lions" and not getattr(self, "_red", False):
                    card(m, side, "red"); self._red = True
                if team == "Tigers":
                    card(m, side, "yellow")
        bears = next(m for m in ms if "Bears" in (m.home.team.name, m.away.team.name))
        card(bears, "home" if bears.home.team.name == "Bears" else "away", "yellow")
        a = {x["key"]: x for x in rankings.awards(lambda o: True, 1)["awards"]}
        self.assertEqual(a["fairplay"]["who"], "Bears", a["fairplay"])
        self.assertIn("1 card point", a["fairplay"]["stat"])
        self.assertNotIn("matchs", " ".join(x["stat"] for x in a.values()), "plural of match is matches")

    def test_friendlies_have_their_own_ranking_and_awards_are_worldwide(self):
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers"])
        self.play(self.a, b1, c1, "Lions")
        fs = self.a.call("post", f"{b1}/competitions", {"name": "Fun", "kind": "friendly", "visibility": "public"}).json()["competition"]["slug"]
        ids = [self.a.call("post", f"{b1}/teams", {"name": n}).json()["team"]["id"] for n in ("Owls", "Bats")]
        self.a.call("post", f"{b1}/competitions/{fs}/entries", {"teamIds": ids})
        self.a.call("post", f"{b1}/competitions/{fs}/generate", {"start": "2026-01-01T15:00:00Z"})
        for m in Match.objects.filter(competition__slug=fs):
            self.a.call("patch", f"{b1}/matches/{m.id}", {"homeScore": 2, "awayScore": 0})
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        main = Client().get("/rankings").content.decode()
        self.assertIn("Lions", main)
        self.assertNotIn("Owls", main, "friendlies don't mix with the main ranking")
        friendly = Client().get("/rankings?view=friendlies").content.decode()
        self.assertIn("Owls", friendly)
        self.assertNotIn("Tigers", friendly)
        awards = Client().get("/rankings/awards?country=Nowhere").content.decode()
        self.assertIn("Awards 2026 · worldwide", awards)
        self.assertIn("Lions", awards, "the country filter doesn't apply to awards")

    def test_team_of_the_season_and_organization_awards(self):
        from competitions.models import Player, Team
        b1, c1 = self.league(self.a, self.sa, ["Lions", "Tigers"])
        b2, c2 = self.league(self.b, self.sb, ["Eagles", "Hawks"])
        lions = Team.objects.get(name="Lions")
        Player.objects.create(team=lions, name="Ama Keeper", position="GK")
        Player.objects.create(team=lions, name="Kojo Back", position="DF")
        striker = Player.objects.create(team=lions, name="Kofi Striker", position="FW")
        for owner, base, cs in ((self.a, b1, c1), (self.b, b2, c2)):
            for m in Match.objects.filter(competition__slug=cs).select_related("home__team"):
                owner.call("patch", f"{base}/matches/{m.id}", {"homeScore": 2, "awayScore": 0})
                if cs == c1:                           # the Lions striker scores for the Lions, whichever side they're on
                    ev = {"side": "home" if m.home.team_id == lions.id else "away", "playerId": striker.id}
                else:
                    ev = {"side": "home", "playerName": "Eli Hawk"}
                owner.call("post", f"{base}/matches/{m.id}/events", {"kind": "goal", "minute": 5, **ev})
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})
        xi = rankings.team_of_the_season()["xi"]
        self.assertIn("Kofi Striker", [p["name"] for p in xi["FW"]])
        self.assertEqual([p["name"] for p in xi["GK"]], ["Ama Keeper"], "the goalkeeper of a team that kept clean sheets")
        self.assertEqual([p["name"] for p in xi["DF"]], ["Kojo Back"])
        page = Client().get("/rankings/awards").content.decode()
        self.assertIn("Team of the Season", page)
        self.assertIn("Kofi Striker", page)
        mine = self.a.get(f"/api/orgs/{self.sa}/awards").json()
        names = [p["name"] for k in mine["xi"] for p in mine["xi"][k]]
        self.assertIn("Kofi Striker", names)
        self.assertNotIn("Eli Hawk", names, "only this organization's players")

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
