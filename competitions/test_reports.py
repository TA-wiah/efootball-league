"""Teams report their own matches: check-in, screenshots as proof, and walkovers for no-shows (Pro League)."""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from league.models import Admin
from orgs.tests import Helpers

from . import reports
from .models import Match

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class ReportsTest(Helpers, TestCase):
    def setUp(self):
        reports._last["t"] = 0
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        self.a, self.b = self.signup("boss"), self.signup("other")
        self.sa, self.sb = self.new_org(self.a, "Kasoa League"), self.new_org(self.b, "Accra League")
        self.lions = self.a.call("post", f"/api/orgs/{self.sa}/teams", {"name": "Lions"}).json()["team"]["id"]
        self.eagles = self.b.call("post", f"/api/orgs/{self.sb}/teams", {"name": "Eagles"}).json()["team"]["id"]
        # a Pro League match between teams of two organizations, run by the platform
        from proleague.logic import pro_org
        from competitions.models import Competition, Entry, Team
        self.root.call("patch", "/api/admin/settings", {"section": "proleague", "changes": {"enabled": True, "whatsapp": "+233241234567", "walkover_hours": 24}})
        from league.models import Admin as U
        pro = pro_org(U.objects.get(username="root"))
        self.pro = pro.slug
        c = Competition.objects.create(org=pro, name="Premier · Season 1", slug="premier-s1", visibility="public", status="active")
        h = Entry.objects.create(competition=c, team=Team.objects.get(id=self.lions))
        a = Entry.objects.create(competition=c, team=Team.objects.get(id=self.eagles))
        self.m = Match.objects.create(competition=c, home=h, away=a, kickoff=timezone.now() + timedelta(minutes=30), slug="lions-v-eagles")

    def test_reporting_and_proof_privacy(self):
        url = f"/api/report/{self.m.id}"
        stranger = self.signup("stranger")
        self.assertEqual(stranger.get(url).status_code, 404)
        r = self.a.get(url).json()
        self.assertEqual((r["sides"], r["staff"], r["checkinOpen"]), (["home"], False, True))
        self.assertIn("wa.me/233241234567", r["whatsapp"])
        self.assertEqual(self.a.call("post", url + "/checkin", {"side": "away"}).status_code, 403, "only their own team")
        self.assertIsNotNone(self.a.call("post", url + "/checkin", {}).json()["checkin"]["home"])
        bad = self.b.call("post", url + "/proof", {"kind": "result", "image": "data:image/svg+xml;base64,PHN2Zz4="})
        self.assertEqual(bad.status_code, 400, "no SVG")
        r = self.b.call("post", url + "/proof", {"kind": "result", "image": PNG, "homeScore": 1, "awayScore": 2, "note": "We won"})
        self.assertEqual(r.status_code, 200, r.json())
        proof = r.json()["proof"]
        self.assertEqual((proof["side"], proof["homeScore"], proof["awayScore"]), ("away", 1, 2))
        self.assertIsNotNone(r.json()["checkin"]["away"], "sending proof counts as showing up")
        # the screenshot is private to the two teams and the league
        self.assertEqual(self.a.get(proof["url"]).status_code, 200)
        self.assertEqual(self.root.get(proof["url"]).status_code, 200)
        self.assertEqual(stranger.get(proof["url"]).status_code, 404)
        self.assertEqual(Client().get(proof["url"]).status_code, 404)
        self.assertEqual(self.a.call("delete", f"{url}/proof/{proof['id']}").status_code, 403, "only who sent it")
        self.assertEqual(self.root.get(url).json()["staff"], True)
        mine = self.a.get("/api/report/mine").json()["matches"]
        self.assertEqual((mine[0]["id"], mine[0]["mine"], mine[0]["pro"]), (self.m.id, ["home"], True))
        page = Client().get(f"/match/{self.m.slug}").content.decode()
        self.assertIn(f"/app/report/{self.m.id}", page, "the public page links to reporting")

    def overdue(self):
        Match.objects.filter(id=self.m.id).update(kickoff=timezone.now() - timedelta(hours=25))
        call_command("settle_walkovers", stdout=StringIO())
        return Match.objects.get(id=self.m.id)

    def test_walkover_for_the_team_that_showed_up(self):
        Match.objects.filter(id=self.m.id).update(away_checkin=timezone.now())
        m = self.overdue()
        self.assertEqual((m.status, m.decided, m.home_score, m.away_score), ("finished", "walkover", 0, 3))
        self.assertIn("Eagles showed up, Lions didn't", m.notes)
        # the league can still change it by hand
        self.root.call("patch", f"/api/orgs/{self.pro}/matches/{m.id}", {"homeScore": 1, "awayScore": 1})
        self.assertEqual(Match.objects.get(id=m.id).decided, "")

    def test_no_show_and_both_present(self):
        m = self.overdue()
        self.assertEqual((m.status, m.decided), ("cancelled", "no_show"))
        Match.objects.filter(id=m.id).update(status="scheduled", decided="", home_checkin=timezone.now(), away_checkin=timezone.now())
        m = self.overdue()
        self.assertEqual((m.status, m.decided), ("scheduled", ""), "both showed up: the league decides")

    def test_not_due_yet_and_switched_off(self):
        call_command("settle_walkovers", stdout=StringIO())
        self.assertEqual(Match.objects.get(id=self.m.id).status, "scheduled", "not past the deadline")
        self.root.call("patch", "/api/admin/settings", {"section": "proleague", "changes": {"walkovers": False}})
        self.assertEqual(self.overdue().status, "scheduled", "walkovers switched off")

    def test_walkovers_are_settled_when_anyone_opens_the_site(self):
        Match.objects.filter(id=self.m.id).update(kickoff=timezone.now() - timedelta(hours=25), home_checkin=timezone.now())
        reports._last["t"] = 0
        Client().get("/api/auth/me")                     # a visitor, not even logged in
        m = Match.objects.get(id=self.m.id)
        self.assertEqual((m.status, m.decided, m.home_score, m.away_score), ("finished", "walkover", 3, 0))


class FlagsTest(TestCase):
    def test_flags_are_served_safely(self):
        r = Client().get("/flags/gh.svg")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/svg+xml"))
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertEqual(Client().get("/flags/gb-eng.svg").status_code, 200, "England")
        countries = Client().get("/flags/countries.json").json()
        self.assertIn({"code": "gh", "name": "Ghana"}, countries)
        self.assertGreater(len(countries), 250)
        for bad in ("../settings.py", "GH.svg", "gh.png", "x.js", "zz.svg"):
            self.assertEqual(Client().get(f"/flags/{bad}").status_code, 404, bad)
        import pathlib
        import re
        for f in pathlib.Path("public/flags").glob("*.svg"):
            self.assertIsNone(re.search(rb"<script|\son\w+\s*=|foreignObject|javascript:", f.read_bytes()), f.name)
