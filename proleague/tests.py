"""The Pro League: seasons built from the rankings, invitations, divisions, results, promotion and relegation."""
from unittest import mock

from django.test import Client, TestCase

from competitions import rankings
from competitions.models import Competition, Match
from league.models import Admin
from orgs.tests import Helpers
from payments import paynova
from sms.tests import FakePayNova2

from .models import Season, SeasonTeam


class ProLeagueTest(Helpers, TestCase):
    def setUp(self):
        rankings._cache.update(key=None, data=None)
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        self.a, self.b = self.signup("boss"), self.signup("other")
        self.sa, self.sb = self.new_org(self.a, "Kasoa League"), self.new_org(self.b, "Accra League")
        # two local leagues decide the rankings: earlier names win more
        self.play_local(self.a, self.sa, ["A1", "A2", "A3", "A4"])
        self.play_local(self.b, self.sb, ["B1", "B2", "B3", "B4"])
        self.root.call("patch", "/api/admin/settings", {"section": "rankings", "changes": {"enabled": True, "min_matches": 1}})

    def play_local(self, owner, slug, names):
        base = f"/api/orgs/{slug}"
        cs = owner.call("post", base + "/competitions", {"name": f"{slug} cup", "visibility": "public"}).json()["competition"]["slug"]
        ids = [owner.call("post", base + "/teams", {"name": n}).json()["team"]["id"] for n in names]
        owner.call("post", f"{base}/competitions/{cs}/entries", {"teamIds": ids})
        owner.call("post", f"{base}/competitions/{cs}/generate", {"start": "2026-01-01T15:00:00Z", "legs": 1})
        for m in Match.objects.filter(competition__slug=cs).select_related("home__team", "away__team"):
            h, a = names.index(m.home.team.name), names.index(m.away.team.name)
            owner.call("patch", f"{base}/matches/{m.id}", {"homeScore": 3 if h < a else 0, "awayScore": 0 if h < a else 3})

    def settings(self, **extra):
        r = self.root.call("patch", "/api/admin/settings", {"section": "proleague", "changes": {
            "enabled": True, "divisions": "Premier, Championship", "size": 4, "move": 1, "legs": 2, "whatsapp": "+233 24 123 4567", **extra}})
        self.assertEqual(r.status_code, 200, r.json())

    def test_a_full_season_and_the_next_one(self):
        for bad in ({"size": 3}, {"size": 7}, {"move": 5}, {"divisions": ""}, {"whatsapp": "12"}, {"fee": "-1"}):
            self.assertEqual(self.root.call("patch", "/api/admin/settings", {"section": "proleague", "changes": bad}).status_code, 400, bad)
        self.assertEqual(self.root.call("post", "/api/admin/proleague/season").status_code, 400, "switched off")
        self.settings()
        self.assertEqual(self.boss_cannot(), 403)
        season = self.root.call("post", "/api/admin/proleague/season").json()["season"]
        self.assertEqual(len(season["teams"]), 8, "2 divisions × 4")
        self.assertEqual({t["team"]["name"] for t in season["teams"][:2]}, {"A1", "B1"}, "the best-ranked first")
        self.assertEqual(self.root.call("post", "/api/admin/proleague/season").status_code, 400, "one season at a time")
        sid = season["id"]
        self.root.call("post", f"/api/admin/proleague/season/{sid}/invite")
        # each organization answers for its own teams only
        inv_a = self.a.get(f"/api/orgs/{self.sa}/proleague").json()["invitations"]
        self.assertEqual(sorted(i["team"]["name"] for i in inv_a), ["A1", "A2", "A3", "A4"])
        b_ids = [i["id"] for i in self.b.get(f"/api/orgs/{self.sb}/proleague").json()["invitations"]]
        self.assertEqual(self.a.call("post", f"/api/orgs/{self.sa}/proleague/{b_ids[0]}/accept").status_code, 404, "not their team")
        for i in inv_a:
            self.assertEqual(self.a.call("post", f"/api/orgs/{self.sa}/proleague/{i['id']}/accept").json()["status"], "confirmed", "no fee: confirmed")
        for i in b_ids[:3]:
            self.b.call("post", f"/api/orgs/{self.sb}/proleague/{i}/accept")
        self.b.call("post", f"/api/orgs/{self.sb}/proleague/{b_ids[3]}/decline")
        self.assertEqual(self.root.call("post", f"/api/admin/proleague/season/{sid}/add", {"count": 1}).status_code, 400, "no more ranked teams")
        season = self.root.get("/api/admin/proleague").json()["seasons"][0]
        self.assertEqual(season["plan"], [4], "7 confirmed: one division of 4 (3 teams can't make a division)")
        self.root.call("post", f"/api/admin/proleague/team/{b_ids[3]}/confirm")      # e.g. they changed their mind
        r = self.root.call("post", f"/api/admin/proleague/season/{sid}/start")
        self.assertEqual(r.status_code, 200, r.json())
        comps = r.json()["season"]["competitions"]
        self.assertEqual([c["division"] for c in comps], ["Premier", "Championship"])
        prem = Competition.objects.get(slug=comps[0]["slug"])
        champ = Competition.objects.get(slug=comps[1]["slug"])
        self.assertEqual((prem.entries.count(), champ.entries.count()), (4, 4))
        self.assertEqual(prem.matches.count(), 12, "home and away")
        self.assertEqual({e.team.org.slug for e in prem.entries.select_related("team__org")}, {self.sa, self.sb}, "teams from different organizations")
        page = Client().get(f"/match/{prem.matches.first().slug}").content.decode()
        self.assertIn("https://wa.me/233241234567?text=", page)
        # the league's results are entered by the platform (the Pro League organization's owner)
        pro = prem.org.slug
        order = {}
        for c in (prem, champ):
            names = [st.team.name for st in SeasonTeam.objects.filter(competition=c).order_by("-seed")]   # weakest wins: tables flip
            order[c.id] = names
            for m in c.matches.select_related("home__team", "away__team"):
                h, a = names.index(m.home.team.name), names.index(m.away.team.name)
                self.assertEqual(self.root.call("patch", f"/api/orgs/{pro}/matches/{m.id}", {"homeScore": 2 if h < a else 0, "awayScore": 0 if h < a else 2}).status_code, 200)
        self.assertEqual(self.root.call("post", f"/api/admin/proleague/season/{sid}/finish").status_code, 200)
        # next season: bottom of the Premier goes down, top of the Championship comes up
        nxt = self.root.call("post", "/api/admin/proleague/season").json()["season"]
        reasons = {t["team"]["name"]: t["reason"] for t in nxt["teams"]}
        self.assertEqual(reasons[order[prem.id][-1]], "Relegated from Premier")
        self.assertEqual(reasons[order[champ.id][0]], "Promoted from Championship")
        self.assertIn("Stayed in Premier", reasons[order[prem.id][0]])
        seeds = [t["team"]["name"] for t in nxt["teams"]]
        self.assertLess(seeds.index(order[champ.id][0]), seeds.index(order[prem.id][-1]), "the promoted team is placed above the relegated one")

    def boss_cannot(self):
        return self.a.call("post", "/api/admin/proleague/season").status_code

    def test_entry_fee(self):
        fake = FakePayNova2()
        p = mock.patch.object(paynova, "call", fake)
        p.start()
        self.addCleanup(p.stop)
        self.root.call("patch", "/api/admin/settings", {"section": "payments", "changes": {"enabled": True, "secret_key": "sk_test_" + "x" * 30}})
        self.settings(fee="10")
        sid = self.root.call("post", "/api/admin/proleague/season").json()["season"]["id"]
        self.root.call("post", f"/api/admin/proleague/season/{sid}/invite")
        inv = self.a.get(f"/api/orgs/{self.sa}/proleague").json()["invitations"][0]
        r = self.a.call("post", f"/api/orgs/{self.sa}/proleague/{inv['id']}/accept").json()
        self.assertEqual((r["status"], r["checkoutUrl"]), ("accepted", "https://api.paynova.com/pay/PAY-0001/"))
        self.assertEqual(fake.calls[-1][2]["amount"], "10.00")
        self.assertFalse(self.a.call("post", f"/api/orgs/{self.sa}/proleague/{inv['id']}/check").json()["confirmed"])
        fake.payments["PAY-0001"]["status"] = "paid"
        self.assertTrue(self.a.call("post", f"/api/orgs/{self.sa}/proleague/{inv['id']}/check").json()["confirmed"])
        self.assertEqual(SeasonTeam.objects.get(id=inv["id"]).status, "confirmed")
        # the super admin can confirm a team that paid another way
        other = self.a.get(f"/api/orgs/{self.sa}/proleague").json()["invitations"][1]
        self.assertEqual(self.root.call("post", f"/api/admin/proleague/team/{other['id']}/confirm").status_code, 200)
        self.assertEqual(SeasonTeam.objects.get(id=other["id"]).status, "confirmed")
        self.assertEqual(Season.objects.get(id=sid).fee, 10)


class DivisionSizesTest(TestCase):
    def test_splitting_teams_into_divisions(self):
        from .logic import division_sizes
        names = ["A", "B", "C"]
        self.assertEqual(division_sizes(7, names[:2], 4), [4])
        self.assertEqual(division_sizes(8, names[:2], 4), [4, 4])
        self.assertEqual(division_sizes(11, names, 6), [6, 5])
        self.assertEqual(division_sizes(13, names, 4), [4, 4, 4], "the 13th team waits")
        self.assertEqual(division_sizes(18, names, 6), [6, 6, 6])
        self.assertEqual(division_sizes(3, names, 6), [])
