"""Text messages: the harm check, credits, buying credits, and invoices texted to people we only have a phone number for.
PayNova and the SMS provider are always faked here."""
from unittest import mock

from django.test import TestCase

from league.models import Admin
from orgs.tests import Helpers
from payments import paynova
from payments.models import Invoice
from payments.tests import KEY, FakePayNova

from . import guard, providers
from .models import SmsAccount, SmsMessage, SmsPurchase


class FakePayNova2(FakePayNova):
    """Adds payment links (no email) and verifying them."""

    def __init__(self):
        super().__init__()
        self.payments = {}

    def __call__(self, method, path, data=None, cfg=None, timeout=20):
        if (method, path) == ("POST", "/payments/initialize/"):
            self.calls.append((method, path, data))
            ref = f"PAY-{len(self.payments) + 1:04d}"
            self.payments[ref] = {"reference": ref, "status": "pending", "amount": data["amount"], "currency": data["currency"]}
            return {"payment": {"reference": ref, "status": "pending"}, "checkout_url": f"https://api.paynova.com/pay/{ref}/"}
        if method == "GET" and path.startswith("/payments/") and path.endswith("/verify/"):
            self.calls.append((method, path, data))
            return self.payments[path.split("/")[2]]
        return super().__call__(method, path, data, cfg, timeout)


class GuardTest(TestCase):
    def test_blocks_harmful_texts(self):
        bad = ["Please send me your MoMo PIN to confirm the payment",
               "Reply with the OTP we sent you",
               "Congratulations, you have won GHS 5000 in our promo! Pay the processing fee of 50 to claim",
               "Send the money to 0241234567 now",
               "I sent 200 cedis to you by mistake, please reverse it",
               "We will kill you after the match",
               "Pay here: https://bit.ly/abc123",
               "Visit http://free-prizes.xyz/claim for tickets"]
        for t in bad:
            self.assertTrue(guard.problems(t), t)

    def test_allows_normal_texts(self):
        ok = ["Kickoff moved to 6pm on Saturday. See you at the park!",
              "Hi Kofi, Robotics sent you a bill of GHS 50.00 for Entry fee: Robotics Championship. Pay securely here: https://api.paynova.com/pay/PAY-1/",
              "Training is cancelled today because of the rain.",
              "The semi-final draw is out: Lions v Tigers on Friday."]
        for t in ok:
            self.assertEqual(guard.problems(t), [], t)

    def test_parts_and_numbers(self):
        self.assertEqual((providers.parts("a" * 160), providers.parts("a" * 161), providers.parts("hi ⚽")), (1, 2, 1))
        self.assertEqual(providers.parts("⚽" * 71), 2)
        self.assertEqual((providers.normalize("0241234567"), providers.normalize("+233 24 123 4567"), providers.normalize("hello")),
                         ("233241234567", "233241234567", None))


class SmsTest(Helpers, TestCase):
    def setUp(self):
        self.fake = FakePayNova2()
        self.sent = []
        for target, fn in ((paynova, "call", ), ):
            pass
        p1 = mock.patch.object(paynova, "call", self.fake)
        p2 = mock.patch.object(providers, "send", lambda numbers, text, cfg=None: self.sent.append((list(numbers), text)) or "REF1")
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.root = self.signup("root")
        Admin.objects.filter(username="root").update(is_superuser=True, is_staff=True)
        self.boss = self.signup("boss")
        self.slug = self.new_org(self.boss)
        self.base = f"/api/orgs/{self.slug}"
        r = self.root.call("patch", "/api/admin/settings", {"section": "sms", "changes": {"enabled": True, "provider": "arkesel", "api_key": "ark-key-123456",
                                                                                         "sender": "Robotics", "credit_price": "0.05", "min_credits": 100}})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertNotIn("ark-key-123456", str(r.json()))

    def credits(self):
        return SmsAccount.objects.get(org__slug=self.slug).credits

    def test_settings_validation(self):
        for bad in ({"provider": "pigeon"}, {"sender": "A very long sender name"}, {"country_code": "abc"}, {"credit_price": "-1"}, {"min_credits": 0}):
            self.assertEqual(self.root.call("patch", "/api/admin/settings", {"section": "sms", "changes": bad}).status_code, 400, bad)
        self.assertEqual(self.boss.call("patch", "/api/admin/settings", {"section": "sms", "changes": {"enabled": False}}).status_code, 403)

    def test_sending_costs_credits_and_harmful_texts_are_blocked(self):
        r = self.boss.call("post", self.base + "/sms/send", {"numbers": ["0241234567"], "body": "Kickoff at 6pm"})
        self.assertEqual(r.status_code, 402, "no credits yet")
        self.root.call("post", "/api/admin/sms/credit", {"org": self.boss.get(self.base).json()["org"]["id"], "credits": 10})
        r = self.boss.call("post", self.base + "/sms/send", {"numbers": ["0241234567", "+233 20 000 0000", "0241234567"], "body": "Kickoff at 6pm"})
        self.assertEqual((r.status_code, r.json()["credits"]), (200, 8), r.json())
        self.assertEqual(self.sent[-1], (["233241234567", "233200000000"], "Kickoff at 6pm"))
        r = self.boss.call("post", self.base + "/sms/send", {"numbers": ["0241234567"], "body": "Send me your MoMo PIN to get your prize"})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["blocked"])
        self.assertEqual((self.credits(), len(self.sent)), (8, 1), "blocked: nothing sent, nothing charged")
        self.assertEqual(SmsMessage.objects.filter(status="blocked").count(), 1)
        self.assertEqual(len(self.root.get("/api/admin/sms").json()["blocked"]), 1, "the super admin sees it")
        check = self.boss.call("post", self.base + "/sms/check", {"body": "Visit https://bit.ly/x"}).json()
        self.assertTrue(check["problems"])
        with mock.patch.object(providers, "send", side_effect=providers.SmsError("SMS provider: down (500)")):
            self.assertEqual(self.boss.call("post", self.base + "/sms/send", {"numbers": ["0241234567"], "body": "Hello"}).status_code, 502)
        self.assertEqual(self.credits(), 8, "failed sends are refunded")
        viewer = self.signup("viewer1")
        self.invite_and_join(self.boss, self.slug, viewer, "viewer")
        self.assertEqual(viewer.call("post", self.base + "/sms/send", {"numbers": ["0241234567"], "body": "Hi"}).status_code, 403)

    def test_buying_credits(self):
        r = self.boss.call("post", self.base + "/sms/buy", {"credits": 500, "method": "paynova"})
        self.assertEqual(r.status_code, 400, "PayNova isn't connected yet")
        self.assertEqual(self.boss.call("post", self.base + "/sms/buy", {"credits": 10, "method": "request"}).status_code, 400, "below the minimum")
        req = self.boss.call("post", self.base + "/sms/buy", {"credits": 200, "method": "request"}).json()["purchase"]
        self.assertEqual(self.root.call("post", f"/api/admin/sms/purchases/{req['id']}/grant").status_code, 200)
        self.assertEqual(self.root.call("post", f"/api/admin/sms/purchases/{req['id']}/grant").status_code, 409, "only once")
        self.assertEqual(self.credits(), 200)
        self.root.call("patch", "/api/admin/settings", {"section": "payments", "changes": {"enabled": True, "secret_key": KEY}})
        buy = self.boss.call("post", self.base + "/sms/buy", {"credits": 500, "method": "paynova"}).json()["purchase"]
        self.assertEqual((buy["amount"], buy["checkoutUrl"]), ("25.00", "https://api.paynova.com/pay/PAY-0001/"))
        url = f"{self.base}/sms/purchases/{buy['id']}/check"
        self.assertFalse(self.boss.call("post", url).json()["added"], "not paid yet")
        self.fake.payments["PAY-0001"]["status"] = "paid"
        self.assertTrue(self.boss.call("post", url).json()["added"])
        self.assertFalse(self.boss.call("post", url).json()["added"], "never added twice")
        self.assertEqual(self.credits(), 700)
        self.assertEqual(SmsPurchase.objects.get(id=buy["id"]).status, "paid")

    def test_phone_only_invoices_are_texted_and_confirmed(self):
        self.root.call("patch", "/api/admin/settings", {"section": "payments", "changes": {"enabled": True, "secret_key": KEY}})
        self.root.call("post", "/api/admin/sms/credit", {"org": self.boss.get(self.base).json()["org"]["id"], "credits": 20})
        self.assertEqual(self.boss.call("post", self.base + "/invoices", {"amount": "50", "description": "Entry fee",
                                                                          "recipients": [{"name": "Kofi"}]}).status_code, 400, "needs an email or a phone")
        r = self.boss.call("post", self.base + "/invoices", {"amount": "50", "description": "Entry fee",
                                                              "recipients": [{"name": "Kofi Mensah", "phone": "0241234567"},
                                                                             {"name": "Ama", "email": "ama@example.com", "phone": "0201234567"}]})
        self.assertEqual((r.status_code, r.json()["sent"]), (200, 2), r.json())
        kofi, ama = r.json()["invoices"]
        self.assertEqual((kofi["payUrl"], kofi["sms"], kofi["customerEmail"]), ("https://api.paynova.com/pay/PAY-0001/", "Texted", None))
        self.assertEqual(ama["sms"], "Texted", "texted as well, even though PayNova emails her an invoice")
        self.assertIn("https://api.paynova.com/pay/PAY-0001/", self.sent[0][1])
        self.assertEqual(self.sent[0][0], ["233241234567"])
        self.assertEqual(self.credits(), 20 - 2 * providers.parts(self.sent[0][1]))
        self.fake.payments["PAY-0001"]["status"] = "paid"
        data = self.boss.call("post", self.base + "/payments/sync").json()
        self.assertEqual(Invoice.objects.get(reference="PAY-0001").status, "paid")
        self.assertEqual(data["balances"][0]["paid"], "50.00")

    def test_ready_made_messages(self):
        org_id = self.boss.get(self.base).json()["org"]["id"]
        self.root.call("post", "/api/admin/sms/credit", {"org": org_id, "credits": 50})
        cs = self.boss.call("post", self.base + "/competitions", {"name": "Robo Cup", "visibility": "public"}).json()["competition"]["slug"]
        ids = [self.boss.call("post", self.base + "/teams", {"name": n}).json()["team"]["id"] for n in ("Lions", "Tigers")]
        self.boss.call("post", f"{self.base}/competitions/{cs}/entries", {"teamIds": ids})
        self.boss.call("post", f"{self.base}/competitions/{cs}/generate", {"start": "2099-10-11T18:00:00Z"})
        match = self.boss.get(f"{self.base}/competitions/{cs}/matches").json()["matches"][0]
        # a player of one team, with a phone, is chosen by default
        kofi = self.signup("kofi9")
        kofi.call("patch", "/api/account", {"phone": "0241234567"})
        self.invite_and_join(self.boss, self.slug, kofi, "player")
        mid = self.member_id(self.boss, self.slug, "kofi9")
        self.boss.call("patch", f"{self.base}/members/{mid}", {"teams": [match["home"]["id"] if "id" in match["home"] else ids[0]]})
        from orgs.models import Membership
        Membership.objects.get(id=mid).teams.set([ids[0]])
        p = self.boss.call("post", self.base + "/sms/check", {"template": "match_reminder", "match": match["id"]}).json()
        self.assertTrue(p["text"].startswith("Reminder from Kasoa Community League:"), p["text"])
        self.assertIn(f"/match/{match['slug']}", p["text"])
        self.assertEqual((p["problems"], p["memberIds"]), ([], [mid]))
        self.assertEqual(self.boss.call("post", self.base + "/sms/check", {"template": "result", "match": match["id"]}).status_code, 400, "no result yet")
        r = self.boss.call("post", self.base + "/sms/send", {"template": "fixtures_out", "competition": cs, "memberIds": [mid]})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertIn(f"/competition/{cs}/fixtures", self.sent[-1][1])
        self.assertEqual(self.sent[-1][0], ["233241234567"])

    def test_payment_reminders(self):
        self.root.call("patch", "/api/admin/settings", {"section": "payments", "changes": {"enabled": True, "secret_key": KEY}})
        self.root.call("post", "/api/admin/sms/credit", {"org": self.boss.get(self.base).json()["org"]["id"], "credits": 20})
        inv = self.boss.call("post", self.base + "/invoices", {"amount": "50", "description": "Entry fee", "dueDate": "2099-01-31",
                                                                "recipients": [{"name": "Kofi Mensah", "phone": "0241234567"}]}).json()["invoices"][0]
        self.assertEqual(self.boss.call("post", self.base + "/invoices/remind", {}).status_code, 400, "nothing chosen")
        r = self.boss.call("post", self.base + "/invoices/remind", {"all": True}).json()
        self.assertEqual((r["sent"], r["skipped"]), (1, 0))
        self.assertIn("is still unpaid, due 31 Jan. Pay securely here: https://api.paynova.com/pay/", self.sent[-1][1])
        r = self.boss.call("post", self.base + "/invoices/remind", {"ids": [inv["id"]]}).json()
        self.assertEqual((r["sent"], r["skipped"]), (0, 1), "at most once every 12 hours")

    def test_moolre_request_format(self):
        calls = []
        with mock.patch.object(providers, "_post", lambda url, data, headers, form=False, timeout=20: calls.append((url, data, headers)) or {"status": 1, "code": "SMS01"}):
            mock.patch.stopall()
            ref = providers.send(["233241234567"], "Hello", {"enabled": True, "provider": "moolre", "api_key": "vas-key", "sender": "Robotics"})
        self.assertEqual(ref, "SMS01")
        url, data, headers = calls[0]
        self.assertEqual((url, headers), ("https://api.moolre.com/open/sms/send", {"X-API-VASKEY": "vas-key"}))
        self.assertEqual(data, {"type": 1, "senderid": "Robotics", "messages": [{"recipient": "233241234567", "message": "Hello"}]})
        with mock.patch.object(providers, "_post", lambda *a, **k: {"status": 0, "code": "ASMS07", "message": "Sender ID is not approved"}):
            with self.assertRaises(providers.SmsError):
                providers.send(["233241234567"], "Hello", {"enabled": True, "provider": "moolre", "api_key": "k", "sender": "Robotics"})

    def test_mnotify_request_format(self):
        calls = []
        mock.patch.stopall()
        ok = {"status": "success", "code": "2000", "message": "messages sent successfully", "summary": {"_id": "A59C-1", "total_sent": 1}}
        with mock.patch.object(providers, "_post", lambda url, data, headers, form=False, timeout=20: calls.append((url, data, headers)) or ok):
            ref = providers.send(["233241234567"], "Hello", {"enabled": True, "provider": "mnotify", "api_key": "mn key&x", "sender": "Robotics"})
        self.assertEqual(ref, "A59C-1")
        url, data, headers = calls[0]
        self.assertEqual(url, "https://api.mnotify.com/api/sms/quick?key=mn+key%26x")
        self.assertEqual(data, {"recipient": ["0241234567"], "sender": "Robotics", "message": "Hello", "is_schedule": False, "schedule_date": ""})
        with mock.patch.object(providers, "_post", lambda *a, **k: {"status": "error", "code": "1002", "message": "Invalid key"}):
            with self.assertRaises(providers.SmsError):
                providers.send(["233241234567"], "Hello", {"enabled": True, "provider": "mnotify", "api_key": "k", "sender": "Robotics"})
