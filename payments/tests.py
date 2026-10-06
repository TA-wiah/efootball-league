"""Profile editing, and payments through PayNova (always faked here: tests never call the real API)."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from league.models import Admin
from orgs.tests import PW, Browser, Helpers

from . import paynova
from .models import Invoice, Payout

KEY = "sk_test_" + "x" * 30


class FakePayNova:
    """Stands in for PayNova's API: remembers invoices and records every call."""

    def __init__(self):
        self.calls, self.invoices, self.fail = [], {}, None

    def __call__(self, method, path, data=None, cfg=None, timeout=20):
        self.calls.append((method, path, data))
        if self.fail:
            raise paynova.PayNovaError(self.fail)
        if (method, path) == ("POST", "/invoices/"):
            code = f"INV_{len(self.invoices) + 1:04d}"
            self.invoices[code] = {"invoice_code": code, "invoice_number": len(self.invoices) + 1, "amount": data["amount"],
                                   "currency": data["currency"], "status": "pending", "pay_url": f"https://api.paynova.com/pay/{code}/"}
            return {"message": "Invoice created and sent.", "invoice": self.invoices[code]}
        if (method, path) == ("GET", "/invoices/"):
            return {"count": len(self.invoices), "next": None, "results": list(self.invoices.values())}
        if path == "/payments/balance/":
            return {"balances": [{"currency": "GHS", "available": "100.00", "frozen": "0.00"}]}
        if path == "/payments/send/":
            return {"message": "Transfer completed.", "transaction": {"reference": "TXN-1", "status": "completed"}}
        if path == "/payouts/":
            return {"reference": "PO-1", "status": "pending", "message": "Payout queued for processing."}
        raise AssertionError(path)


class ProfileTest(Helpers, TestCase):
    def test_edit_profile(self):
        kofi = self.signup("kofi")
        r = kofi.call("patch", "/api/account", {"firstName": "Kofi", "lastName": "Mensah", "phone": "+233 24 123 4567"})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual((r.json()["user"]["firstName"], r.json()["user"]["phone"]), ("Kofi", "+233241234567"))
        self.assertEqual(kofi.call("patch", "/api/account", {"phone": "call me"}).status_code, 400)
        self.assertEqual(kofi.call("patch", "/api/account", {"email": "new@example.com"}).status_code, 403, "needs the password")
        self.assertEqual(kofi.call("patch", "/api/account", {"email": "new@example.com", "password": "wrong password!"}).status_code, 403)
        self.signup("ama")
        self.assertEqual(kofi.call("patch", "/api/account", {"username": "ama", "password": PW}).status_code, 409)
        self.assertEqual(kofi.call("patch", "/api/account", {"email": "ama@example.com", "password": PW}).status_code, 409)
        r = kofi.call("patch", "/api/account", {"username": "kofi_m", "email": "new@example.com", "password": PW})
        self.assertEqual((r.status_code, r.json()["user"]["username"]), (200, "kofi_m"))
        self.assertEqual(kofi.get("/api/account").json()["user"]["email"], "new@example.com")

    def test_change_password_signs_out_other_devices(self):
        kofi, other = self.signup("kofi"), Browser()
        other.call("post", "/api/auth/login", {"user": "kofi", "password": PW})
        self.assertEqual(other.me["user"]["username"], "kofi")
        self.assertEqual(kofi.call("post", "/api/account/password", {"current": "wrong", "new": "A brand new password 2"}).status_code, 403)
        self.assertEqual(kofi.call("post", "/api/account/password", {"current": PW, "new": "short"}).status_code, 400)
        r = kofi.call("post", "/api/account/password", {"current": PW, "new": "A brand new password 2"})
        self.assertEqual(r.status_code, 200, r.json())
        kofi.csrf = r.json()["csrf"]
        self.assertEqual(kofi.get("/api/account").status_code, 200, "this device stays signed in")
        self.assertEqual(other.get("/api/account").status_code, 401, "other devices are signed out")
        self.assertEqual(Browser().call("post", "/api/auth/login", {"user": "kofi", "password": "A brand new password 2"}).status_code, 200)


class PaymentsTest(Helpers, TestCase):
    def setUp(self):
        self.fake = FakePayNova()
        patcher = mock.patch.object(paynova, "call", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        self.base = f"/api/orgs/{self.slug}"

    def connect(self, **extra):
        r = self.root.call("patch", "/api/admin/settings", {"section": "payments", "changes": {"enabled": True, "secret_key": KEY, "currency": "GHS", **extra}})
        self.assertEqual(r.status_code, 200, r.json())
        return r.json()

    def test_super_admin_connects_paynova_and_the_key_stays_secret(self):
        for bad in ({"secret_key": "pk_test_abcdefghijkl"}, {"secret_key": "hello"}, {"fee_percent": "80"}, {"fee_fixed": "-1"},
                    {"currency": "XYZ"}, {"wallet_id": "not a wallet!"}):
            self.assertEqual(self.root.call("patch", "/api/admin/settings", {"section": "payments", "changes": bad}).status_code, 400, bad)
        self.assertEqual(self.boss.call("patch", "/api/admin/settings", {"section": "payments", "changes": {"enabled": True}}).status_code, 403)
        data = self.connect(fee_enabled=True, fee_percent="5", fee_fixed="1")
        self.assertNotIn(KEY, str(data))
        self.assertTrue(data["payments"]["secret_key_set"])
        self.assertEqual(data["paymentStatus"], {"ready": True, "mode": "test", "source": "admin panel"})
        r = self.root.call("post", "/api/admin/payments/test")
        self.assertEqual((r.status_code, r.json()["balances"][0]["currency"]), (200, "GHS"))
        self.assertEqual(self.fake.calls[-1][1], "/payments/balance/")

    def test_invoices_payments_fees_and_payouts(self):
        self.assertEqual(self.boss.call("post", self.base + "/invoices", {"name": "Lions", "email": "lions@example.com", "amount": "50",
                                                                          "description": "Entry fee"}).status_code, 400, "not connected yet")
        self.connect(fee_enabled=True, fee_percent="5", fee_fixed="1")
        viewer = self.signup("viewer1")
        self.invite_and_join(self.boss, self.slug, viewer, "viewer")
        self.assertEqual(viewer.get(self.base + "/payments").status_code, 403)
        # a competition and two teams: invoice every team its entry fee at once
        cs = self.boss.call("post", self.base + "/competitions", {"name": "Robo Cup"}).json()["competition"]["slug"]
        t1, t2 = [self.boss.call("post", self.base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Lions", "Tigers")]
        other = self.signup("xavier")
        foreign = other.call("post", f"/api/orgs/{self.new_org(other, 'Other')}/teams", {"name": "Foreign"}).json()["team"]["id"]
        bad = self.boss.call("post", self.base + "/invoices", {"amount": "50", "description": "Entry fee", "competition": cs,
                                                                "recipients": [{"teamId": foreign, "name": "X", "email": "x@example.com"}]})
        self.assertEqual(bad.status_code, 400)
        for b in ({"amount": "0"}, {"amount": "abc"}, {"currency": "XYZ"}, {"description": "x"}, {"dueDate": "2001-01-01"},
                  {"recipients": [{"name": "A", "email": "not-an-email"}]}):
            body = {"amount": "50", "description": "Entry fee", "recipients": [{"name": "Lions", "email": "l@example.com"}], **b}
            self.assertEqual(self.boss.call("post", self.base + "/invoices", body).status_code, 400, b)
        r = self.boss.call("post", self.base + "/invoices", {"amount": "50", "description": "Entry fee: Robo Cup", "competition": cs,
                                                              "recipients": [{"teamId": t1, "name": "Lions", "email": "lions@example.com"},
                                                                             {"teamId": t2, "name": "Tigers", "email": "tigers@example.com", "phone": "+233241234567"}]})
        self.assertEqual((r.status_code, r.json()["sent"]), (200, 2), r.json())
        sent = [c for c in self.fake.calls if c[1] == "/invoices/" and c[0] == "POST"]
        self.assertEqual((sent[0][2]["amount"], sent[0][2]["currency"], sent[1][2]["customer_phone"]), ("50.00", "GHS", "+233241234567"))
        # nothing is paid until PayNova says so
        self.assertEqual(self.boss.get(self.base + "/payments").json()["balances"], [])
        self.fake.invoices["INV_0001"]["status"] = "paid"
        self.fake.invoices["INV_0002"].update(status="paid", amount="5.00")          # paid amount doesn't match: not credited
        data = self.boss.call("post", self.base + "/payments/sync").json()
        bal = data["balances"][0]
        self.assertEqual((bal["paid"], bal["fees"], bal["net"], bal["available"]), ("50.00", "3.50", "46.50", "46.50"))
        self.assertEqual(Invoice.objects.get(code="INV_0002").status, "pending")
        # payouts: can't take more than the balance; a super admin approves before money moves
        prize = {"amount": "40", "currency": "GHS", "method": "paynova", "destination": {"email": "winner@example.com"}, "purpose": "Champion prize"}
        self.assertEqual(self.boss.call("post", self.base + "/payouts", {**prize, "amount": "46.51"}).status_code, 400)
        self.assertEqual(self.boss.call("post", self.base + "/payouts", {**prize, "method": "mobile_money", "destination": {"network": "mtn"}}).status_code, 400)
        p = self.boss.call("post", self.base + "/payouts", prize).json()["payout"]
        self.assertEqual(self.boss.get(self.base + "/payments").json()["balances"][0]["available"], "6.50", "held while waiting")
        self.assertFalse(any(c[1] == "/payments/send/" for c in self.fake.calls), "nothing sent before approval")
        self.assertEqual(self.boss.call("post", f"/api/admin/payouts/{p['id']}/approve").status_code, 403)
        self.fake.fail = "PayNova: insufficient funds (400)"
        r = self.root.call("post", f"/api/admin/payouts/{p['id']}/approve")
        self.assertEqual((r.status_code, Payout.objects.get(id=p["id"]).status), (502, "requested"), "a failure can be retried")
        self.fake.fail = None
        r = self.root.call("post", f"/api/admin/payouts/{p['id']}/approve")
        self.assertEqual((r.status_code, r.json()["payout"]["status"], r.json()["payout"]["reference"]), (200, "sent", "TXN-1"))
        self.assertEqual(self.fake.calls[-1][2]["recipient_email"], "winner@example.com")
        self.assertEqual(self.root.call("post", f"/api/admin/payouts/{p['id']}/approve").status_code, 409, "never sent twice")
        # mobile money needs the platform wallet; a rejected payout gives the money back
        mm = self.boss.call("post", self.base + "/payouts", {**prize, "amount": "6.50", "method": "mobile_money",
                                                              "destination": {"network": "mtn", "phone_number": "0241234567"}}).json()["payout"]
        self.assertEqual(self.root.call("post", f"/api/admin/payouts/{mm['id']}/approve").status_code, 502)
        self.assertEqual(self.root.call("post", f"/api/admin/payouts/{mm['id']}/reject", {"note": "Wrong number"}).status_code, 200)
        self.assertEqual(self.boss.get(self.base + "/payments").json()["balances"][0]["available"], "6.50")
        overview = self.root.get("/api/admin/payments").json()
        self.assertEqual((overview["totals"][0]["fees"], overview["totals"][0]["paidOut"]), ("3.50", "40.00"))

    def test_competition_prizes(self):
        cs = self.boss.call("post", self.base + "/competitions", {"name": "Robo Cup"}).json()["competition"]["slug"]
        url = f"{self.base}/competitions/{cs}"
        for bad in ({"currency": "XYZ"}, {"entryFee": "-5"}, {"entryFee": "1.234"}, {"prizes": [{"label": "", "amount": "5"}]}, {"prizes": "lots"}):
            self.assertEqual(self.boss.call("patch", url, {"money": bad}).status_code, 400, bad)
        r = self.boss.call("patch", url, {"money": {"currency": "GHS", "entryFee": "50", "prizes": [{"label": "Champion", "amount": "300"},
                                                                                                     {"label": "Runner-up", "amount": "100.5"}]}})
        self.assertEqual(r.json()["competition"]["money"]["prizes"][1], {"label": "Runner-up", "amount": "100.50"})
        self.boss.call("patch", url, {"visibility": "public"})
        html = self.client.get(f"/competition/{cs}").content.decode()
        self.assertIn("Champion", html)
        self.assertIn("300.00", html)

    def test_fee_maths(self):
        self.assertEqual(paynova.fee_for(Decimal("50"), Decimal("5"), Decimal("1")), Decimal("3.50"))
        self.assertEqual(paynova.fee_for(Decimal("1"), Decimal("50"), Decimal("10")), Decimal("1.00"), "never more than the amount")
        self.assertEqual(paynova.mode("sk_live_abc"), "live")
        self.assertEqual(paynova.mode("pk_live_abc"), "")

    def test_receipts_emails_and_split_payments(self):
        from league import emailer
        self.connect(fee_enabled=True, fee_percent="10")
        mails = []
        p1 = mock.patch.object(emailer, "ready", lambda cfg=None: True)
        p2 = mock.patch.object(emailer, "send", lambda to, subject, txt, html: mails.append((to, subject, txt)))
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.boss.call("post", self.base + "/invoices", {"amount": "40", "description": "Entry fee", "recipients": [{"name": "Lions FC", "email": "lions@example.com"}]})
        self.fake.invoices["INV_0001"]["status"] = "paid"
        inv = self.boss.call("post", self.base + "/payments/sync").json()["invoices"][0]
        self.assertTrue(inv["receiptUrl"].startswith("/receipt/"))
        page = self.client.get(inv["receiptUrl"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("GHS 40.00", page.content.decode())
        self.assertIn("no-store", page["Cache-Control"])
        self.assertEqual(self.client.get("/receipt/not-a-real-receipt-token-xx").status_code, 404)
        to = [m[0] for m in mails]
        self.assertIn("lions@example.com", to, "the payer gets the receipt")
        self.assertIn("boss@example.com", to, "the organization is told")
        self.assertIn(inv["receiptUrl"], next(m[2] for m in mails if m[0] == "lions@example.com"))
        # payout decisions are emailed to whoever asked
        p = self.boss.call("post", self.base + "/payouts", {"amount": "10", "currency": "GHS", "method": "paynova", "destination": {"email": "w@example.com"},
                                                            "purpose": "Prize"}).json()["payout"]
        self.root.call("post", f"/api/admin/payouts/{p['id']}/reject", {"note": "Not yet"})
        self.assertTrue(any(m[0] == "boss@example.com" and "Payout not approved" in m[1] for m in mails))
        # a split code: invoices go straight to the organization's own PayNova account
        org_id = self.boss.get(self.base).json()["org"]["id"]
        self.assertEqual(self.root.call("post", "/api/admin/payments/split", {"org": org_id, "splitCode": "nope"}).status_code, 400)
        self.assertEqual(self.boss.call("post", "/api/admin/payments/split", {"org": org_id, "splitCode": "SPL_abc12345"}).status_code, 403)
        self.assertEqual(self.root.call("post", "/api/admin/payments/split", {"org": org_id, "splitCode": "SPL_abc12345"}).status_code, 200)
        self.boss.call("post", self.base + "/invoices", {"amount": "60", "description": "Entry fee", "recipients": [{"name": "Tigers", "email": "t@example.com"}]})
        self.assertEqual([c for c in self.fake.calls if c[1] == "/invoices/" and c[0] == "POST"][-1][2]["split_code"], "SPL_abc12345")
        self.fake.invoices["INV_0002"]["status"] = "paid"
        bal = self.boss.call("post", self.base + "/payments/sync").json()["balances"][0]
        self.assertEqual((bal["paid"], bal["fees"], bal["available"], bal["direct"]), ("40.00", "4.00", "36.00", "60.00"), "split money isn't held by the platform")
