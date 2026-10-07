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
        self.assertNotIn("Secret Cup", Client().get(f"/org/{self.slug}").content.decode())
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
        self.assertIn("public right now", r.content.decode())
        self.assertEqual(r["Cache-Control"], "private, no-store")

    def test_team_and_organization_pages(self):
        self.finish_first(self.cs)
        r = Client().get("/team/kasoa-stars")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sunday League", r.content.decode())
        org = Client().get(f"/org/{self.slug}").content.decode()
        self.assertIn("Sunday League", org)
        self.assertIn("Kasoa Stars", org)

    def test_old_league_links_robots_and_sitemap(self):
        r = Client().get(f"/league/{self.cs}/table")
        self.assertEqual((r.status_code, r["Location"]), (301, f"/competition/{self.cs}/table"))
        self.assertIn("Disallow: /app", Client().get("/robots.txt").content.decode())
        sm = Client().get("/sitemap.xml").content.decode()
        self.assertIn(f"/competition/{self.cs}/table", sm)
        self.assertIn("/team/kasoa-stars", sm)

    def test_groups_knockouts_and_share_previews(self):
        cs = self.owner.call("post", self.base + "/competitions", {"name": "Robo Cup", "visibility": "public", "format": "groups_knockout",
                                                                  "qualifiersPerGroup": 2}).json()["competition"]["slug"]
        ids = [self.owner.call("post", self.base + "/teams", {"name": f"Side {i}"}).json()["team"]["id"] for i in range(4)]
        for i, tid in enumerate(ids):
            self.owner.call("post", f"{self.base}/competitions/{cs}/entries", {"teamId": tid, "group": "AB"[i // 2]})
        anon = Client()
        table = anon.get(f"/competition/{cs}/table").content.decode()
        self.assertIn('class="grp-h">Group A', table)
        self.assertIn("Top 2 go through", table)
        r = anon.get(f"/competition/{cs}/knockouts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("haven't been drawn yet", r.content.decode())
        entries = self.owner.get(f"{self.base}/competitions/{cs}").json()["entries"]
        self.owner.call("post", f"{self.base}/competitions/{cs}/draw", {"entryIds": [e["id"] for e in entries], "roundName": "Semi-finals"})
        self.assertIn("Semi-finals", anon.get(f"/competition/{cs}/knockouts").content.decode())
        self.assertEqual(anon.get(f"/competition/{self.cs}/knockouts").status_code, 404, "plain leagues have no knockouts")
        # share previews: no image until there's a logo, then the organization's logo is used
        self.assertNotIn("og:image", anon.get(f"/competition/{cs}").content.decode())
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        self.owner.call("post", f"{self.base}/logo", {"image": png})
        self.owner.call("patch", self.base, {"description": "We play every Sunday.", "website": {"tagline": ""}})
        for url in (f"/competition/{cs}", f"/org/{self.slug}"):
            html = anon.get(url).content.decode()
            self.assertIn('property="og:image" content="http://testserver/media/org/', html, url)
        self.assertIn('og:description" content="We play every Sunday."', anon.get(f"/org/{self.slug}").content.decode())

    def test_import_original_league_into_existing_competition(self):
        import json
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from league.models import League
        League.objects.update_or_create(pk=1, defaults={"data": json.dumps({"g": {"A": ["P1", "P2", "P3", "P4"], "B": ["Q1", "Q2", "Q3", "Q4"]},
                                                                         "cfg": {"a": 2}, "r": {"A0_0": [2, 1]}, "ui": {"t": "CHAMPIONS LEAGUE"}})})
        cs = self.owner.call("post", self.base + "/competitions", {"name": "Robotics Championship", "visibility": "public"}).json()["competition"]["slug"]
        call_command("import_league", self.slug, into=cs, description="Two groups of four.", stdout=__import__("io").StringIO())
        c = __import__("competitions.models", fromlist=["Competition"]).Competition.objects.get(slug=cs)
        self.assertEqual((c.format, c.qualifiers_per_group, c.entries.count(), c.matches.count(), c.description), ("groups_knockout", 2, 8, 24, "Two groups of four."))
        self.assertEqual(c.matches.filter(status="finished").count(), 1, "results come across")
        with self.assertRaises(CommandError):
            call_command("import_league", self.slug, into=cs, stdout=__import__("io").StringIO())
        self.assertEqual(c.entries.count(), 8, "running it twice changes nothing")

    def test_fonts_are_served_by_the_site(self):
        anon = Client()
        css = anon.get("/fonts/fonts.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("url(/fonts/barlow-condensed-800-latin.woff2)", css.content.decode())
        font = anon.get("/fonts/barlow-400-latin.woff2")
        self.assertEqual((font.status_code, font["Content-Type"], font.content[:4]), (200, "font/woff2", b"wOF2"))
        self.assertIn("immutable", font["Cache-Control"])
        for bad in ("../settings.py", "app.html", "nope.woff2", "x.exe"):
            self.assertEqual(anon.get(f"/fonts/{bad}").status_code, 404, bad)
        page = anon.get(f"/competition/{self.cs}")
        self.assertIn('href="/fonts/fonts.css"', page.content.decode())
        self.assertNotIn("googleapis", page.content.decode() + page["Content-Security-Policy"])

    def test_import_from_the_saved_copy_when_the_database_has_no_league(self):
        from django.core.management import call_command
        from league.models import League
        League.objects.all().delete()                                   # like production: no original league
        cs = self.owner.call("post", self.base + "/competitions", {"name": "Robotics Championship", "visibility": "public"}).json()["competition"]["slug"]
        out = __import__("io").StringIO()
        call_command("import_league", self.slug, into=cs, stdout=out)
        self.assertIn("saved copy", out.getvalue())
        c = __import__("competitions.models", fromlist=["Competition"]).Competition.objects.get(slug=cs)
        names = sorted(e.team.name for e in c.entries.select_related("team"))
        self.assertEqual((len(names), c.matches.count()), (8, 24))
        self.assertIn("Philiss", names)

    def test_import_creates_the_competition_when_it_does_not_exist(self):
        from django.core.management import call_command
        from competitions.models import Competition
        from league.models import League
        League.objects.all().delete()
        out = __import__("io").StringIO()
        call_command("import_league", self.slug, into="robotics-championship", stdout=out)
        self.assertIn("Created the competition", out.getvalue())
        c = Competition.objects.get(slug="robotics-championship")
        self.assertEqual((c.name, c.org.slug, c.format, c.qualifiers_per_group, c.visibility), ("Robotics Championship", self.slug, "groups_knockout", 2, "public"))
        groups = {g: sorted(e.team.name for e in c.entries.select_related("team") if e.group == g) for g in "AB"}
        self.assertEqual(groups["A"], sorted(["Philiss", "Kai_Heinz07", "Mz man", "xxbeta"]))
        self.assertEqual(groups["B"], sorted(["Junior_billyhill", "Atalynix", "Rockyjnr_xX", "OSTEENRBA"]))
        self.assertTrue(c.description.startswith("The Robotics Championship"))
