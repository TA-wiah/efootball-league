"""PayNova (paynova.com): the platform's payment account, set up by a super admin.

The secret key lives only on the server (platform settings, or the PAYNOVA_SECRET_KEY environment variable) and is sent
only to PayNova's fixed API address, never to a browser. Every call has a timeout and turns failures into PayNovaError.
"""
import json
import os
import urllib.error
import urllib.request
from decimal import ROUND_HALF_UP, Decimal

API = "https://api.paynova.com/api/v1"           # fixed on purpose: the secret key is never sent anywhere else


class PayNovaError(Exception):
    pass


def config():
    from superadmin import store
    s = store.get("payments")
    key = s.get("secret_key") or os.environ.get("PAYNOVA_SECRET_KEY", "")
    return {**s, "secret_key": key, "source": "admin panel" if s.get("secret_key") else ("environment" if key else "")}


def mode(key):
    return "live" if key.startswith("sk_live_") else "test" if key.startswith("sk_test_") else ""


def ready(cfg=None):
    cfg = cfg or config()
    return bool(cfg.get("enabled") and mode(cfg.get("secret_key", "")))


def money(v):
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fee_for(amount, percent, fixed):
    """The platform's fee on a paid amount (never more than the amount)."""
    fee = money(Decimal(amount) * Decimal(percent) / 100 + Decimal(fixed))
    return min(max(fee, Decimal("0.00")), money(amount))


def call(method, path, data=None, cfg=None, timeout=20):
    cfg = cfg or config()
    key = cfg.get("secret_key", "")
    if not mode(key):
        raise PayNovaError("Online payments aren't available yet. Please contact the platform administrator to turn them on.")
    req = urllib.request.Request(API + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "CompetitionManager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:          # noqa: S310 (fixed https address)
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        msg = body.get("message") or body.get("detail") or body.get("error") if isinstance(body, dict) else None
        if not msg and isinstance(body, dict) and body:
            msg = "; ".join(f"{k}: {v[0] if isinstance(v, list) else v}" for k, v in list(body.items())[:3])
        if e.code in (401, 403):
            msg = "PayNova refused the secret key. Check it in the platform settings."
        raise PayNovaError(f"PayNova: {msg or 'request failed'} ({e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PayNovaError("Couldn't reach PayNova. Check the internet connection and try again.") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise PayNovaError("PayNova sent an answer that couldn't be read.") from None


def balance(cfg=None):
    return call("GET", "/payments/balance/", cfg=cfg).get("balances", [])


def create_invoice(name, email, amount, currency, description, phone="", due_date=None, cfg=None, split_code=""):
    data = {"customer_name": name, "customer_email": email, "amount": f"{money(amount):.2f}", "currency": currency,
            "description": description}
    if phone:
        data["customer_phone"] = phone
    if due_date:
        data["due_date"] = due_date.isoformat()
    if split_code:
        data["split_code"] = split_code
    inv = call("POST", "/invoices/", data, cfg=cfg).get("invoice") or {}
    if not inv.get("invoice_code"):
        raise PayNovaError("PayNova didn't return an invoice.")
    return inv


def list_invoices(cfg=None):
    """All invoices PayNova knows for the account, as {invoice_code: invoice}. Follows pagination (up to 20 pages)."""
    out, path = {}, "/invoices/"
    for _ in range(20):
        data = call("GET", path, cfg=cfg)
        rows = data if isinstance(data, list) else data.get("results") or data.get("invoices") or []
        for row in rows:
            if isinstance(row, dict) and row.get("invoice_code"):
                out[row["invoice_code"]] = row
        nxt = data.get("next") if isinstance(data, dict) else None
        if not nxt or not str(nxt).startswith(API):
            break
        path = str(nxt)[len(API):]
    return out


def send(email, amount, currency, note, cfg=None):
    return call("POST", "/payments/send/", {"recipient_email": email, "amount": f"{money(amount):.2f}", "currency": currency,
                                             "note": note[:140]}, cfg=cfg)


def payout(wallet_id, amount, method, destination, cfg=None):
    return call("POST", "/payouts/", {"wallet_id": wallet_id, "amount": f"{money(amount):.2f}", "method": method,
                                      "destination": destination}, cfg=cfg)


def initialize_payment(amount, currency, description, success_url="", cancel_url="", metadata=None, email="", split_code="", cfg=None):
    """A payment link for someone without an email address (we text them the link). Returns {reference, checkout_url}."""
    data = {"amount": f"{money(amount):.2f}", "currency": currency, "description": description[:200]}
    for k, v in (("customer_email", email), ("success_url", success_url), ("cancel_url", cancel_url), ("split_code", split_code)):
        if v:
            data[k] = v
    if metadata:
        data["metadata"] = metadata
    res = call("POST", "/payments/initialize/", data, cfg=cfg)
    pay = res.get("payment") or {}
    ref, url = pay.get("reference") or res.get("reference"), res.get("checkout_url") or res.get("payment_url")
    if not ref or not url:
        raise PayNovaError("PayNova didn't return a payment link.")
    return {"reference": str(ref)[:80], "checkout_url": str(url)[:500]}


def verify(reference, cfg=None):
    """{status: pending|paid|expired|cancelled, amount, currency, paid_at} for one payment, checked with PayNova."""
    import urllib.parse
    return call("GET", f"/payments/{urllib.parse.quote(reference, safe='')}/verify/", cfg=cfg)
