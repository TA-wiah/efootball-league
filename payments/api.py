"""Payments: invoices an organization sends (through the platform's PayNova account), its balance, and payouts.

Money only leaves the platform when a super admin approves a payout. Paid invoices are confirmed with PayNova on the
server (never trusted from a browser), and the amount PayNova reports must match the invoice.
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from competitions.models import Competition, Team
from league.http import ApiError, body, endpoint, ms, site_url, text
from league.logic import EMAIL_RE, audit, hit
from orgs.api import access, log
from superadmin.api import admin

from . import paynova
from . import notify
from .models import Invoice, OrgPaySettings, Payout

SPLIT_RE = re.compile(r"^SPL_[A-Za-z0-9_-]{4,50}$")


def split_of(org):
    s = OrgPaySettings.objects.filter(org=org).first()
    return s.split_code if s else ""

CURRENCIES = ["GHS", "NGN", "USD", "EUR", "GBP", "KES", "ZAR", "XOF", "TZS", "UGX", "RWF", "EGP", "AED"]
NETWORKS = {"mtn": "MTN", "telecel": "Telecel", "airteltigo": "AirtelTigo"}
PHONE_RE = re.compile(r"^\+?[0-9]{9,15}$")
D0 = Decimal("0.00")


# ---------- helpers ----------
def amount_of(b, key="amount", lo=Decimal("0.01"), hi=Decimal("1000000")):
    v = b.get(key)
    try:
        d = paynova.money(v) if isinstance(v, (int, float, str)) and not isinstance(v, bool) and str(v).strip() else None
    except (InvalidOperation, ValueError):
        d = None
    if d is None or not lo <= d <= hi:
        raise ApiError(400, f"Enter an amount from {lo} to {hi:,}.")
    return d


def currency_of(b, default):
    cur = (text(b, "currency", 3) or default or "GHS").upper()
    if cur not in CURRENCIES:
        raise ApiError(400, "Choose a supported currency.")
    return cur


def fee_settings(cfg=None):
    cfg = cfg or paynova.config()
    if not cfg.get("fee_enabled"):
        return D0, D0
    return Decimal(cfg.get("fee_percent") or "0"), Decimal(cfg.get("fee_fixed") or "0")


def need_ready():
    cfg = paynova.config()
    if not paynova.ready(cfg):
        raise ApiError(400, "Online payments aren't available yet. Please contact the platform administrator to turn them on.")
    return cfg


def invoice_json(i):
    return {"id": i.id, "customerName": i.customer_name, "customerEmail": i.customer_email or None, "customerPhone": i.customer_phone or None,
            "sms": i.sms or None, "remindedAt": ms(i.reminded_at), "direct": bool(i.split_code),
            "receiptUrl": f"/receipt/{i.receipt_token}" if i.status == Invoice.PAID and i.receipt_token else None,
            "description": i.description, "amount": f"{i.amount:.2f}", "currency": i.currency, "status": i.status,
            "dueDate": i.due_date.isoformat() if i.due_date else None, "payUrl": i.pay_url or None, "number": i.number, "mode": i.mode,
            "fee": f"{i.fee:.2f}", "net": f"{i.net:.2f}", "paidAt": ms(i.paid_at), "created": ms(i.created),
            "competition": {"name": i.competition.name, "slug": i.competition.slug} if i.competition else None,
            "team": {"id": i.team.id, "name": i.team.name} if i.team else None}


def payout_json(p, admin_view=False):
    d = {"id": p.id, "amount": f"{p.amount:.2f}", "currency": p.currency, "method": p.method, "methodLabel": Payout.METHODS.get(p.method, p.method),
         "destination": p.destination, "purpose": p.purpose, "status": p.status, "reference": p.reference or None, "note": p.note or None,
         "created": ms(p.created), "decidedAt": ms(p.decided_at), "requestedBy": getattr(p.requested_by, "username", None),
         "competition": {"name": p.competition.name, "slug": p.competition.slug} if p.competition else None}
    if admin_view:
        d["org"] = {"id": p.org_id, "name": p.org.name, "slug": p.org.slug}
    return d


def balances(org):
    """Per currency: what was paid in, the platform's fees, payouts sent or waiting, and what's free to pay out."""
    out = {}
    for r in Invoice.objects.filter(org=org, status=Invoice.PAID, split_code="").values("currency").annotate(paid=Sum("amount"), fees=Sum("fee"), net=Sum("net"), n=Count("id")):
        out[r["currency"]] = {"currency": r["currency"], "paid": r["paid"], "fees": r["fees"], "net": r["net"], "invoices": r["n"], "sent": D0, "waiting": D0, "direct": D0}
    for r in Invoice.objects.filter(org=org, status=Invoice.PAID).exclude(split_code="").values("currency").annotate(t=Sum("amount")):
        out.setdefault(r["currency"], {"currency": r["currency"], "paid": D0, "fees": D0, "net": D0, "invoices": 0, "sent": D0, "waiting": D0, "direct": D0})["direct"] = r["t"]
    for r in Payout.objects.filter(org=org).exclude(status=Payout.REJECTED).values("currency", "status").annotate(total=Sum("amount")):
        row = out.setdefault(r["currency"], {"currency": r["currency"], "paid": D0, "fees": D0, "net": D0, "invoices": 0, "sent": D0, "waiting": D0, "direct": D0})
        row["sent" if r["status"] == Payout.SENT else "waiting"] += r["total"]
    for row in out.values():
        row["available"] = row["net"] - row["sent"] - row["waiting"]
    return out


def money_json(rows):
    return [{k: (f"{v:.2f}" if isinstance(v, Decimal) else v) for k, v in r.items()} for r in sorted(rows.values(), key=lambda r: r["currency"])]


def mark_paid(inv, remote):
    """PayNova says it's paid: check the amount, work out the platform fee, and credit the organization."""
    try:
        paid_amount = paynova.money(remote.get("amount", inv.amount))
    except (InvalidOperation, ValueError):
        paid_amount = None
    if paid_amount != inv.amount:
        log(inv.org, None, f"payment for invoice #{inv.id} needs checking: PayNova reports {remote.get('amount')} {inv.currency}, the invoice is {inv.amount}")
        return False
    when = remote.get("paid_at")
    try:
        inv.paid_at = datetime.fromisoformat(str(when).replace("Z", "+00:00")) if when else timezone.now()
    except ValueError:
        inv.paid_at = timezone.now()
    import secrets
    inv.status = Invoice.PAID
    inv.receipt_token = inv.receipt_token or secrets.token_urlsafe(18)
    if inv.split_code:                                # PayNova paid the organization directly; the split sets the shares
        inv.fee, inv.net = D0, D0
    else:
        inv.fee = paynova.fee_for(inv.amount, inv.fee_percent, inv.fee_fixed)
        inv.net = inv.amount - inv.fee
    log(inv.org, None, f"{inv.customer_name} paid {inv.amount:.2f} {inv.currency} ({inv.description})")
    return True


def sync(org=None, force=False, base=""):
    """Ask PayNova for the status of unpaid invoices (at most once a minute unless forced). Returns how many changed."""
    cfg = paynova.config()
    if not paynova.ready(cfg):
        return 0
    now = timezone.now()
    q = Invoice.objects.exclude(code="", reference="").filter(Q(status=Invoice.PENDING) | Q(status=Invoice.CANCELLED, created__gte=now - timedelta(days=90)))
    if org:
        q = q.filter(org=org)
    if not force:
        q = q.filter(Q(checked_at__isnull=True) | Q(checked_at__lt=now - timedelta(seconds=60)))
    todo = list(q.select_related("org"))
    if not todo:
        return 0
    remote = paynova.list_invoices(cfg) if any(i.code for i in todo) else {}
    for inv in todo[:40]:
        if inv.reference and not inv.code:
            remote[f"ref:{inv.reference}"] = paynova.verify(inv.reference, cfg)
    changed, paid = 0, []
    with transaction.atomic():
        locked = {i.pk: i for i in Invoice.objects.select_for_update().select_related("org").filter(pk__in=[x.pk for x in todo])}
        todo = [locked[x.pk] for x in todo if x.pk in locked]
        for inv in todo:
            inv.checked_at = now
            r = remote.get(inv.code) if inv.code else remote.get(f"ref:{inv.reference}")
            status = str((r or {}).get("status", "")).lower()
            if r and status == "paid" and inv.status != Invoice.PAID and mark_paid(inv, r):
                changed += 1
                paid.append(inv)
            elif r and status in ("cancelled", "canceled", "void", "expired") and inv.status == Invoice.PENDING:
                inv.status = Invoice.CANCELLED
                changed += 1
            inv.save()
    from django.conf import settings
    for inv in paid:                                  # emails after the payments are safely recorded
        notify.invoice_paid(inv, base or site_url())
    return changed


def base_of(request):
    return site_url(request)


def overview(org, base=""):
    try:
        sync(org, base=base)
        sync_error = None
    except paynova.PayNovaError as e:
        sync_error = str(e)
    cfg = paynova.config()
    pct, fixed = fee_settings(cfg)
    return {"ready": paynova.ready(cfg), "mode": paynova.mode(cfg.get("secret_key", "")) or None, "currency": cfg.get("currency") or "GHS",
            "currencies": CURRENCIES, "networks": NETWORKS, "methods": Payout.METHODS,
            "fee": {"percent": f"{pct:.2f}", "fixed": f"{fixed:.2f}"} if (pct or fixed) else None, "syncError": sync_error,
            "balances": money_json(balances(org)), "direct": bool(split_of(org)),
            "invoices": [invoice_json(i) for i in Invoice.objects.filter(org=org).select_related("competition", "team")[:300]],
            "payouts": [payout_json(p) for p in Payout.objects.filter(org=org).select_related("competition", "requested_by")[:200]]}


# ---------- organization: overview, invoices, payouts ----------
@endpoint("GET", login_required=True)
def org_payments(request, user, ip, slug):
    org, m = access(user, slug, "payments.manage")
    return overview(org, base_of(request))


@endpoint("POST", login_required=True)
def org_sync(request, user, ip, slug):
    org, m = access(user, slug, "payments.manage")
    if hit(f"paysync:{org.id}", 6, 60):
        raise ApiError(429, "Checked a moment ago. Try again in a minute.")
    try:
        changed = sync(org, force=True, base=base_of(request))
    except paynova.PayNovaError as e:
        raise ApiError(502, str(e)) from None
    return {"ok": True, "changed": changed, **overview(org, base_of(request))}


def one_invoice(request, org, user, cfg, b, competition=None, team=None):
    name, email = text(b, "name", 100), text(b, "email", 254).lower()
    if len(name) < 2:
        raise ApiError(400, "Enter who the invoice is for.")
    phone = text(b, "phone", 30).replace(" ", "")
    if not email and not phone:
        raise ApiError(400, f"Enter an email or a phone number for {name}, so they get the pay link.")
    if email and not EMAIL_RE.fullmatch(email):
        raise ApiError(400, f"Check the email for {name}.")
    if phone and not PHONE_RE.fullmatch(phone):
        raise ApiError(400, f"Check the phone number for {name}.")
    return name, email, phone


def text_pay_link(inv, user):
    """Text the pay link (even when PayNova also emails an invoice). Never blocks the invoice itself."""
    from sms import api as sms_api, providers
    if not inv.customer_phone or not inv.pay_url:
        return ""
    if not providers.ready():
        return "" if inv.customer_email else "Not texted: text messages aren't available yet. Copy the pay link and send it yourself."
    body_text = (f"Hi {inv.customer_name.split()[0]}, {inv.org.name} sent you a bill of {inv.currency} {inv.amount:.2f} for "
                 f"{inv.description}. Pay securely here: {inv.pay_url}")
    try:
        sms_api.send_text(inv.org, user, [inv.customer_phone], body_text, kind="invoice")
        return "Texted"
    except ApiError as e:
        return ("Not texted: " + e.message)[:120]


@endpoint("POST", login_required=True)
def org_invoices(request, user, ip, slug):
    """One invoice ({name, email, …}) or one per team ({recipients: [{teamId, name, email, phone}], …}), sent by PayNova."""
    org, m = access(user, slug, "payments.manage")
    cfg = need_ready()
    b = body(request, 60_000)
    amount = amount_of(b)
    currency = currency_of(b, cfg.get("currency"))
    description = text(b, "description", 200)
    if len(description) < 3:
        raise ApiError(400, "Say what the invoice is for, e.g. “Entry fee: Robotics Championship”.")
    due = None
    if b.get("dueDate"):
        try:
            due = date.fromisoformat(str(b["dueDate"])[:10])
        except ValueError:
            raise ApiError(400, "The due date must look like 2026-10-31.") from None
        if due < timezone.localdate():
            raise ApiError(400, "The due date has already passed.")
    comp = None
    if b.get("competition"):
        comp = Competition.objects.filter(org=org, slug=str(b["competition"])).first()
        if not comp:
            raise ApiError(400, "Choose a competition from this organization.")
    rows = b.get("recipients") if isinstance(b.get("recipients"), list) else [b]
    if not rows or len(rows) > 64:
        raise ApiError(400, "Send between 1 and 64 invoices at a time.")
    checked = []
    for r in rows:
        if not isinstance(r, dict):
            raise ApiError(400, "Bad recipient.")
        team = None
        if r.get("teamId") is not None:
            team = Team.objects.filter(org=org, id=r.get("teamId")).first()
            if not team:
                raise ApiError(400, "Choose teams from this organization.")
        checked.append((one_invoice(request, org, user, cfg, r), team))
    if hit(f"payinv:{org.id}", 200, 3600):
        raise ApiError(429, "Too many invoices this hour. Try again later.")
    pct, fixed = fee_settings(cfg)
    split = split_of(org)
    made, failed = [], []
    for (name, email, phone), team in checked:
        inv = Invoice(org=org, competition=comp, team=team, customer_name=name, customer_email=email, customer_phone=phone,
                      description=description, amount=amount, currency=currency, due_date=due, fee_percent=pct, fee_fixed=fixed,
                      mode=paynova.mode(cfg["secret_key"]), created_by=user, split_code=split)
        try:
            if email:                                     # PayNova emails (and texts) its own invoice
                remote = paynova.create_invoice(name, email, amount, currency, description, phone, due, cfg, split_code=split)
                inv.code, inv.pay_url = str(remote.get("invoice_code", ""))[:64], str(remote.get("pay_url", ""))[:500]
                inv.number = remote.get("invoice_number") if isinstance(remote.get("invoice_number"), int) else None
            else:                                         # phone only: a payment link that we text to them
                remote = paynova.initialize_payment(amount, currency, f"{description} ({org.name})",
                                                    metadata={"kind": "invoice", "org": org.slug, "for": name}, split_code=split, cfg=cfg)
                inv.reference, inv.pay_url = remote["reference"], remote["checkout_url"]
        except paynova.PayNovaError as e:
            failed.append({"name": name, "error": str(e)})
            continue
        inv.save()
        inv.sms = text_pay_link(inv, user)
        if inv.sms:
            inv.save(update_fields=["sms"])
        made.append(inv)
    if made:
        log(org, user, f"sent {len(made)} invoice{'s' if len(made) != 1 else ''} of {amount:.2f} {currency} ({description})")
        audit(user.username, f"sent {len(made)} invoices for {org.name}", ip, resource=f"org:{org.slug}", new=f"{amount:.2f} {currency} × {len(made)}")
    if not made and failed:
        raise ApiError(502, failed[0]["error"])
    return {"ok": True, "sent": len(made), "failed": failed, "invoices": [invoice_json(i) for i in made]}


@endpoint("POST", login_required=True)
def org_invoice_remind(request, user, ip, slug):
    """Text a ready-made payment reminder (with the pay link) to unpaid invoices: {ids: [...]} or {all: true}.
    Each person gets at most one reminder every 12 hours."""
    from sms import api as sms_api, templates
    org, m = access(user, slug, "payments.manage")
    b = body(request)
    q = Invoice.objects.select_related("org").filter(org=org, status=Invoice.PENDING).exclude(customer_phone="").exclude(pay_url="")
    if b.get("all") is not True:
        ids = b.get("ids") if isinstance(b.get("ids"), list) else []
        q = q.filter(id__in=[i for i in ids if isinstance(i, int)])
    now = timezone.now()
    todo = list(q[:200])
    if not todo:
        raise ApiError(400, "There are no unpaid invoices with a phone number to remind.")
    sent, skipped, problem = 0, 0, None
    for inv in todo:
        if inv.reminded_at and inv.reminded_at > now - timedelta(hours=12):
            skipped += 1
            continue
        try:
            sms_api.send_text(org, user, [inv.customer_phone], templates.payment_reminder(inv), kind="reminder")
        except ApiError as e:
            problem = e.message
            if e.status in (402, 502) or "set up" in e.message:
                break                                      # out of credits or provider down: stop here
            continue
        inv.reminded_at = now
        inv.save(update_fields=["reminded_at"])
        sent += 1
    if sent:
        log(org, user, f"texted {sent} payment reminder{'s' if sent != 1 else ''}")
    if not sent and problem:
        raise ApiError(400, problem)
    return {"ok": True, "sent": sent, "skipped": skipped, "problem": problem}


@endpoint("POST", login_required=True)
def org_invoice_cancel(request, user, ip, slug, inv_id):
    org, m = access(user, slug, "payments.manage")
    inv = Invoice.objects.filter(org=org, id=inv_id).first()
    if not inv:
        raise ApiError(404, "Invoice not found.")
    if inv.status != Invoice.PENDING:
        raise ApiError(400, "Only unpaid invoices can be cancelled.")
    inv.status = Invoice.CANCELLED
    inv.save(update_fields=["status"])
    log(org, user, f"cancelled the invoice to {inv.customer_name} ({inv.amount:.2f} {inv.currency})")
    return {"ok": True, "invoice": invoice_json(inv)}


def clean_destination(method, d):
    if not isinstance(d, dict):
        raise ApiError(400, "Say where the money should go.")
    if method == "paynova":
        email = str(d.get("email", "")).strip().lower()[:254]
        if not EMAIL_RE.fullmatch(email):
            raise ApiError(400, "Enter the PayNova account email of whoever receives the money.")
        return {"email": email, "name": str(d.get("name", "")).strip()[:100]}
    if method == "mobile_money":
        network, phone = str(d.get("network", "")).lower(), str(d.get("phone_number", "")).replace(" ", "")
        if network not in NETWORKS or not PHONE_RE.fullmatch(phone):
            raise ApiError(400, "Choose the network and enter the mobile money number.")
        return {"network": network, "phone_number": phone, "name": str(d.get("name", "")).strip()[:100]}
    if method == "bank":
        out = {k: str(d.get(k, "")).strip()[:100] for k in ("bank_name", "account_number", "account_name")}
        if not all(out.values()):
            raise ApiError(400, "Enter the bank, account number and account name.")
        return out
    raise ApiError(400, "Choose how the money is paid out.")


@endpoint("POST", login_required=True)
def org_payouts(request, user, ip, slug):
    """Ask for money to be paid out of the organization's balance (a prize to a winner, or a withdrawal)."""
    org, m = access(user, slug, "payments.manage")
    b = body(request)
    amount = amount_of(b)
    currency = currency_of(b, paynova.config().get("currency"))
    method = text(b, "method", 15)
    if method not in Payout.METHODS:
        raise ApiError(400, "Choose how the money is paid out.")
    dest = clean_destination(method, b.get("destination"))
    purpose = text(b, "purpose", 200)
    if len(purpose) < 3:
        raise ApiError(400, "Say what the payout is for, e.g. “Champion prize”.")
    comp = Competition.objects.filter(org=org, slug=str(b.get("competition") or "")).first() if b.get("competition") else None
    with transaction.atomic():
        type(org).objects.select_for_update().get(pk=org.pk)                    # one payout request at a time per organization
        row = balances(org).get(currency)
        available = row["available"] if row else D0
        if amount > available:
            raise ApiError(400, f"The balance available to pay out is {available:.2f} {currency}.")
        p = Payout.objects.create(org=org, competition=comp, amount=amount, currency=currency, method=method, destination=dest,
                                  purpose=purpose, requested_by=user)
    log(org, user, f"asked to pay out {amount:.2f} {currency}: {purpose}")
    audit(user.username, f"requested a payout for {org.name}", ip, resource=f"org:{org.slug}", new=f"{amount:.2f} {currency} via {method}")
    return {"ok": True, "payout": payout_json(p)}


@endpoint("POST", login_required=True)
def org_payout_cancel(request, user, ip, slug, payout_id):
    org, m = access(user, slug, "payments.manage")
    with transaction.atomic():
        p = Payout.objects.select_for_update().filter(org=org, id=payout_id).first()
        if not p:
            raise ApiError(404, "Payout not found.")
        if p.status != Payout.REQUESTED:
            raise ApiError(400, "Only payouts still waiting for approval can be cancelled.")
        p.status, p.note, p.decided_at = Payout.REJECTED, f"Cancelled by {user.username}", timezone.now()
        p.save(update_fields=["status", "note", "decided_at"])
    log(org, user, f"cancelled the payout request of {p.amount:.2f} {p.currency}")
    return {"ok": True}


# ---------- super admin ----------
@admin("GET")
def admin_payments(request, user, ip):
    try:
        sync(base=base_of(request))
        err = None
    except paynova.PayNovaError as e:
        err = str(e)
    totals = list(Invoice.objects.filter(status=Invoice.PAID).values("currency").annotate(paid=Sum("amount"), fees=Sum("fee"), n=Count("id")).order_by("currency"))
    sent = {r["currency"]: r["t"] for r in Payout.objects.filter(status=Payout.SENT).values("currency").annotate(t=Sum("amount"))}
    cfg = paynova.config()
    return {"ready": paynova.ready(cfg), "mode": paynova.mode(cfg.get("secret_key", "")) or None, "syncError": err,
            "totals": [{"currency": r["currency"], "paid": f"{r['paid']:.2f}", "fees": f"{r['fees']:.2f}", "invoices": r["n"],
                        "paidOut": f"{sent.get(r['currency'], D0):.2f}"} for r in totals],
            "pendingInvoices": Invoice.objects.filter(status=Invoice.PENDING).count(),
            "waiting": [payout_json(p, True) for p in Payout.objects.filter(status__in=[Payout.REQUESTED, Payout.PROCESSING]).select_related("org", "competition", "requested_by").order_by("id")],
            "recentPayouts": [payout_json(p, True) for p in Payout.objects.exclude(status__in=[Payout.REQUESTED, Payout.PROCESSING]).select_related("org", "competition", "requested_by")[:50]],
            "recentInvoices": [{**invoice_json(i), "org": {"name": i.org.name, "slug": i.org.slug}} for i in Invoice.objects.select_related("org", "competition", "team")[:50]],
            "splits": [{"org": {"id": s.org_id, "name": s.org.name}, "splitCode": s.split_code} for s in OrgPaySettings.objects.select_related("org").exclude(split_code="")],
            "orgs": [{"id": o.id, "name": o.name} for o in __import__("orgs.models", fromlist=["Organization"]).Organization.objects.order_by("name")[:500]]}


@admin("POST")
def admin_split(request, user, ip):
    """Give an organization a PayNova split code (or remove it with an empty code)."""
    from orgs.models import Organization
    b = body(request)
    org = Organization.objects.filter(id=b.get("org")).first()
    code = text(b, "splitCode", 60)
    if not org:
        raise ApiError(400, "Choose an organization.")
    if code and not SPLIT_RE.fullmatch(code):
        raise ApiError(400, "A split code looks like SPL_a1b2c3d4e5 (from PayNova → Transaction Splits).")
    s, _ = OrgPaySettings.objects.get_or_create(org=org)
    old, s.split_code = s.split_code, code
    s.save()
    audit(user.username, f"{'set' if code else 'removed'} the PayNova split for {org.name}", ip, resource=f"org:{org.slug}", old=old or None, new=code or None)
    return {"ok": True}


@admin("POST")
def admin_payments_test(request, user, ip):
    try:
        rows = paynova.balance()
    except paynova.PayNovaError as e:
        audit(user.username, "PayNova connection test failed", ip, resource="settings:payments", new=str(e), status="failed")
        raise ApiError(502, str(e)) from None
    audit(user.username, "tested the PayNova connection", ip, resource="settings:payments")
    return {"ok": True, "balances": [{"currency": r.get("currency"), "available": r.get("available"), "frozen": r.get("frozen")} for r in rows if isinstance(r, dict)]}


@admin("POST")
def admin_payout_act(request, user, ip, payout_id, action):
    """approve: send the money through PayNova now. reject: give the amount back to the organization's balance."""
    b = body(request)
    with transaction.atomic():
        p = Payout.objects.select_for_update().select_related("org").filter(id=payout_id).first()
        if not p:
            raise ApiError(404, "Payout not found.")
        if p.status != Payout.REQUESTED:
            raise ApiError(409, "This payout has already been handled.")
        if action == "reject":
            p.status, p.note = Payout.REJECTED, text(b, "note", 300) or "Rejected by the platform"
            p.decided_by, p.decided_at = user, timezone.now()
            p.save()
            audit(user.username, f"rejected a payout for {p.org.name}", ip, resource=f"org:{p.org.slug}", new=f"{p.amount:.2f} {p.currency}")
            log(p.org, None, f"the platform rejected the payout of {p.amount:.2f} {p.currency}: {p.note}")
            notify.payout_decided(p, base_of(request))
            return {"ok": True, "payout": payout_json(p, True)}
        p.status = Payout.PROCESSING                       # stops a double click from sending twice
        p.save(update_fields=["status"])
    cfg = paynova.config()
    try:
        if not paynova.ready(cfg):
            raise paynova.PayNovaError("PayNova isn't connected. Add the secret key in the platform settings first.")
        if p.method == "paynova":
            res = paynova.send(p.destination["email"], p.amount, p.currency, f"{p.org.name}: {p.purpose}", cfg)
            ref = (res.get("transaction") or {}).get("reference") or res.get("reference") or ""
        else:
            wallet = (cfg.get("wallet_id") or "").strip()
            if not wallet:
                raise paynova.PayNovaError("Add the PayNova wallet ID for payouts in the platform settings first.")
            dest = {k: v for k, v in p.destination.items() if k != "name"}
            res = paynova.payout(wallet, p.amount, p.method, dest, cfg)
            ref = res.get("reference") or ""
    except paynova.PayNovaError as e:
        Payout.objects.filter(id=p.id).update(status=Payout.REQUESTED, note=str(e)[:300])
        audit(user.username, f"payout for {p.org.name} failed", ip, resource=f"org:{p.org.slug}", new=str(e), status="failed")
        raise ApiError(502, str(e)) from None
    p.status, p.reference, p.decided_by, p.decided_at = Payout.SENT, str(ref)[:80], user, timezone.now()
    p.note = str(res.get("message") or "")[:300]
    p.save()
    audit(user.username, f"approved and sent a payout for {p.org.name}", ip, resource=f"org:{p.org.slug}", new=f"{p.amount:.2f} {p.currency} ({p.reference})")
    notify.payout_decided(p, base_of(request))
    log(p.org, None, f"the platform sent the payout of {p.amount:.2f} {p.currency}: {p.purpose}")
    return {"ok": True, "payout": payout_json(p, True)}



def receipt(request, token):
    """The payer's receipt: a private, printable page (the link is only in their receipt email and the organizer's list)."""
    from django.http import Http404
    from competitions.public import org_logo, page
    inv = Invoice.objects.select_related("org", "competition").filter(receipt_token=token, status=Invoice.PAID).first() if len(token) >= 20 else None
    if not inv:
        raise Http404
    return page(request, "receipt.html", {"inv": inv, "o": inv.org, "org_logo": org_logo(inv.org), "number": f"R-{inv.created:%Y}-{inv.id:05d}",
                                          "title": f"Receipt {inv.currency} {inv.amount:.2f}", "description": f"Payment receipt from {inv.org.name}."},
                index=False, private=True, banner=False)
