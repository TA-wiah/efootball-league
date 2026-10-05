"""Public pages: anyone can open public and unlisted competitions; private ones stay hidden."""
from django.test import Client, TestCase

from orgs.tests import Helpers

from .models import Match


class PublicPagesTest(Helpers, TestCase):
    def setUp(self):
        self.owner = self.signup("boss")
        self.slug = self.new_org(self.owner, "Kasoa Community League")
        self.base = f"/api/orgs/{self.slug}"
        self.cs = self.make_comp("Sunday League", "public", ["Kasoa Stars", "Winneba <script>alert(1)</script>", "Awutu Rangers"])

    def make_comp(self, name, visibility, teams):
        cs = self.owner.call("post", self.base + "/competitions", {"name": name, "visibility": visibility, "season": "2026/27",
                                                                  "description": f"About {name}"}).json()["competition"]["slug"]
        ids = [self.owner.call("post", self.base + "/teams", {"name": t, "venue": "Town Park"}).json()["team"]["id"] for t in teams]
        self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": ids})
        self.owner.call("post", f"{self.base}/competitions/{cs}/generate", {"start": "2026-10-11T15:00:00Z"})
        return cs

    def finish_first(self, cs):
        m = Match.objects.filter(competition__slug=cs).order_by("round", "id").first()
        self.owner.call("patch", f"{self.base}/matches/{m.id}", {"homeScore": 3, "awayScore": 1, "referee": "A. Mensah"})
        self.owner.call("post", f"{self.base}/matches/{m.id}/events", {"kind": "goal", "side": "home", "minute": 9, "playerName": "Kwame"})
        return Match.objects.get(id=m.id)

    def test_public_competition_pages_without_login(self):
        m = self.finish_first(self.cs)
        anon = Client()
        for tab in ("", "/table", "/fixtures", "/results", "/teams"):
            r = anon.get(f"/competition/{self.cs}{tab}")
            self.assertEqual(r.status_code, 200, tab)
            self.assertIn("public", r["Cache-Control"])
        page = anon.get(f"/competition/{self.cs}/table").content.decode()
        for col in ("Played", "Won", "Drawn", "Lost", "Goals for", "Goals against", "Goal difference", "Points"):
            self.assertIn(col, page)
        self.assertIn(__import__("html").escape(m.home.team.name, quote=False), page)
        self.assertIn('property="og:title"', page)
        self.assertNotIn('name="robots" content="noindex"', page)
        self.assertIn("Copy public link", page)
        resp = anon.get(f"/competition/{self.cs}")
        nonce = resp["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">', resp.content.decode(), "the page script carries this response's CSP nonce")
        self.assertNotIn("<script>alert(1)</script>", page, "team names are escaped")
        self.assertIn("&lt;script&gt;", anon.get(f"/competition/{self.cs}/teams").content.decode())
        self.assertEqual(anon.get(f"/competition/{self.cs}/nope").status_code, 404)

    def test_match_page(self):
        m = self.finish_first(self.cs)
        r = Client().get(f"/match/{m.slug}")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        for text in ("3 : 1", "Full time", "Kwame", "A. Mensah", "Town Park", "Sunday League", "Share"):
            self.assertIn(text, html)
        self.assertIn("3–1", html, "the score is in the link preview")
        self.assertEqual(Client().get(f"/match/{m.id}")["Location"], f"/match/{m.slug}", "numeric ids redirect to the readable address")
        self.assertIn("vs", m.slug)

    def test_unlisted_works_by_link_but_is_not_indexed_or_listed(self):
        cs = self.make_comp("Secret Cup", "unlisted", ["Alpha", "Beta"])
        r = Client().get(f"/competition/{cs}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('content="noindex"', r.content.decode())
        self.assertEqual(r["X-Robots-Tag"], "noindex")
        self.assertNotIn("Secret Cup", Client().get(f"/organization/{self.slug}").content.decode())
        self.assertNotIn(cs, Client().get("/sitemap.xml").content.decode())
        self.assertEqual(Client().get("/team/alpha").status_code, 404, "teams only in unlisted competitions have no public page")

    def test_private_is_hidden_except_for_members(self):
        cs = self.make_comp("Staff Cup", "private", ["Gamma", "Delta"])
        m = Match.objects.filter(competition__slug=cs).first()
        stranger = self.signup("stranger")
        for client in (Client(), stranger.c):
            self.assertEqual(client.get(f"/competition/{cs}").status_code, 404)
            self.assertEqual(client.get(f"/match/{m.slug}").status_code, 404)
        r = self.owner.c.get(f"/competition/{cs}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Private preview", r.content.decode())
        self.assertEqual(r["Cache-Control"], "private, no-store")

    def test_team_and_organization_pages(self):
        self.finish_first(self.cs)
        r = Client().get("/team/kasoa-stars")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sunday League", r.content.decode())
        org = Client().get(f"/organization/{self.slug}").content.decode()
        self.assertIn("Sunday League", org)
        self.assertIn("Kasoa Stars", org)

    def test_old_league_links_robots_and_sitemap(self):
        r = Client().get(f"/league/{self.cs}/table")
        self.assertEqual((r.status_code, r["Location"]), (301, f"/competition/{self.cs}/table"))
        self.assertIn("Disallow: /app", Client().get("/robots.txt").content.decode())
        sm = Client().get("/sitemap.xml").content.decode()
        self.assertIn(f"/competition/{self.cs}/table", sm)
        self.assertIn("/team/kasoa-stars", sm)
