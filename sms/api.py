"""Text messages (SMS) from organizations: credits, sending (after the harm check), buying credits; super admin tools."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from league.http import ApiError, body, endpoint, ms, site_url, text
from league.logic import audit, hit
from orgs.api import access, log
from orgs.models import Membership, Organization
from payments import paynova
from superadmin.api import admin

from . import guard, providers, templates
from .models import SmsAccount, SmsMessage, SmsPurchase

MAX_PEOPLE = 200


def account(org, lock=False):
    acc, _ = SmsAccount.objects.get_or_create(org=org)
    return SmsAccount.objects.select_for_update().get(pk=acc.pk) if lock else acc


def price(cfg=None):
    cfg = cfg or providers.config()
    try:
        return Decimal(cfg.get("credit_price") or "0")
    except InvalidOperation:
        return Decimal("0")


def message_json(m):
    return {"id": m.id, "kind": m.kind, "body": m.body, "to": m.recipients, "people": len(m.recipients), "parts": m.parts, "credits": m.credits,
            "status": m.status, "reasons": m.reasons, "error": m.error or None, "sender": getattr(m.sender, "username", None), "created": ms(m.created)}


def purchase_json(p, admin_view=False):
    d = {"id": p.id, "credits": p.credits, "amount": f"{p.amount:.2f}", "currency": p.currency, "method": p.method, "status": p.status,
         "checkoutUrl": p.checkout_url or None if p.status == SmsPurchase.PENDING else None, "note": p.note or None,
         "requestedBy": getattr(p.requested_by, "username", None), "created": ms(p.created), "doneAt": ms(p.done_at)}
    if admin_view:
        d["org"] = {"id": p.org_id, "name": p.org.name, "slug": p.org.slug}
    return d


def send_text(org, user, numbers, body_text, kind="message", running_host=""):
    """Check, charge and send one text to these numbers. Raises ApiError (blocked, no credits, bad numbers, provider down)."""
    cfg = providers.config()
    if not providers.ready(cfg):
        raise ApiError(400, "Text messages aren't available yet. Please contact the platform administrator to turn them on.")
    body_text = (body_text or "").strip()
    if not body_text:
        raise ApiError(400, "Write the message.")
    if len(body_text) > 918:
        raise ApiError(400, "Keep the message under 918 characters (6 SMS).")
    clean = []
    for n in numbers:
        p = providers.normalize(n, cfg.get("country_code"))
        if not p:
            raise ApiError(400, f"“{n}” isn't a phone number.")
        if p not in clean:
            clean.append(p)
    if not clean or len(clean) > MAX_PEOPLE:
        raise ApiError(400, f"Send to between 1 and {MAX_PEOPLE} people at a time.")
    parts = providers.parts(body_text)
    reasons = guard.problems(body_text, running_host)
    if reasons:
        SmsMessage.objects.create(org=org, sender=user, kind=kind, body=body_text, recipients=clean, parts=parts, status=SmsMessage.BLOCKED, reasons=reasons)
        audit(getattr(user, "username", "system"), f"SMS from {org.name} blocked", "", resource=f"org:{org.slug}", new="; ".join(reasons), status="denied")
        raise ApiError(400, "This message can't be sent: it " + "; it ".join(reasons) + ".", blocked=True, reasons=reasons)
    cost = parts * len(clean)
    with transaction.atomic():
        acc = account(org, lock=True)
        if acc.credits < cost:
            raise ApiError(402, f"This needs {cost} SMS credit{'s' if cost != 1 else ''} and you have {acc.credits}. Buy more credits first.")
        acc.credits -= cost
        acc.save(update_fields=["credits"])
    msg = SmsMessage(org=org, sender=user, kind=kind, body=body_text, recipients=clean, parts=parts, credits=cost)
    try:
        msg.reference = providers.send(clean, body_text, cfg)
        msg.status = SmsMessage.SENT
    except providers.SmsError as e:
        with transaction.atomic():                          # nothing was sent: give the credits back
            acc = account(org, lock=True)
            acc.credits += cost
            acc.save(update_fields=["credits"])
        msg.status, msg.error, msg.credits = SmsMessage.FAILED, str(e)[:300], 0
        msg.save()
        raise ApiError(502, str(e)) from None
    msg.save()
    return msg


def check_purchase(p):
    """For a PayNova purchase: ask PayNova if it's paid, and add the credits once (never twice)."""
    if p.method != "paynova" or p.status != SmsPurchase.PENDING or not p.reference:
        return False
    res = paynova.verify(p.reference)
    status = str(res.get("status", "")).lower()
    with transaction.atomic():
        p = SmsPurchase.objects.select_for_update().get(pk=p.pk)
        if p.status != SmsPurchase.PENDING:
            return False
        if status == "paid":
            try:
                ok = paynova.money(res.get("amount", "0")) == p.amount
            except (InvalidOperation, ValueError):
                ok = False
            if not ok:
                p.note = f"PayNova reports {res.get('amount')} {res.get('currency', '')}; expected {p.amount}. A super admin needs to check it."
                p.save(update_fields=["note"])
                return False
            acc = account(p.org, lock=True)
            acc.credits += p.credits
            acc.save(update_fields=["credits"])
            p.status, p.done_at = SmsPurchase.PAID, timezone.now()
            p.save(update_fields=["status", "done_at"])
            log(p.org, None, f"bought {p.credits} SMS credits ({p.amount:.2f} {p.currency})")
            return True
        if status in ("expired", "cancelled", "canceled"):
            p.status, p.note = SmsPurchase.CANCELLED, f"The payment {status}."
            p.save(update_fields=["status", "note"])
    return False


def overview(org, m):
    cfg = providers.config()
    for p in SmsPurchase.objects.filter(org=org, status=SmsPurchase.PENDING, method="paynova", created__gte=timezone.now() - timedelta(days=2))[:5]:
        try:
            check_purchase(p)
        except paynova.PayNovaError:
            break
    from orgs.permissions import can
    people = [{"id": x.id, "username": x.user.username, "name": " ".join(filter(None, [x.user.first_name, x.user.last_name])) or x.user.username,
               "phone": x.user.phone, "role": x.role} for x in Membership.objects.select_related("user").filter(org=org).exclude(user__phone="")]
    return {"ready": providers.ready(cfg), "credits": account(org).credits, "price": f"{price(cfg):.4f}".rstrip("0").rstrip("."), "currency": cfg.get("currency") or "GHS",
            "minCredits": cfg.get("min_credits") or 1, "canPay": paynova.ready(), "canBuy": can(m.role, "payments.manage", org),
            "canSend": can(m.role, "messages.send", org), "people": people, "templates": templates.TEMPLATES,
            "messages": [message_json(x) for x in SmsMessage.objects.filter(org=org).select_related("sender")[:100]],
            "purchases": [purchase_json(x) for x in SmsPurchase.objects.filter(org=org).select_related("requested_by")[:50]]}


# ---------- organization ----------
@endpoint("GET", login_required=True)
def org_sms(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    from orgs.permissions import can
    if not (can(m.role, "messages.send", org) or can(m.role, "payments.manage", org)):
        raise ApiError(403, "Your role doesn't include text messages.")
    return overview(org, m)


def base_of(request):
    return site_url(request)


@endpoint("POST", login_required=True)
def org_sms_check(request, user, ip, slug):
    """Preview: the ready-made text (or your own), how many SMS it takes, who it goes to, and whether it would be blocked."""
    org, m = access(user, slug, "messages.send")
    b = body(request, 5000)
    t, people = templates.build(org, b.get("template") or "notice", base_of(request), b)
    return {"text": t, "parts": providers.parts(t), "problems": guard.problems(t, request.get_host()) if t else [], "memberIds": people}


@endpoint("POST", login_required=True)
def org_sms_send(request, user, ip, slug):
    org, m = access(user, slug, "messages.send")
    b = body(request, 30_000)
    numbers = [str(n) for n in b.get("numbers", []) if str(n).strip()] if isinstance(b.get("numbers"), list) else []
    ids = b.get("memberIds") if isinstance(b.get("memberIds"), list) else []
    if ids:
        found = list(Membership.objects.select_related("user").filter(org=org, id__in=[i for i in ids if isinstance(i, int)]).exclude(user__phone=""))
        numbers += [x.user.phone for x in found]
    if hit(f"smssend:{org.id}", 30, 3600):
        raise ApiError(429, "Too many messages this hour. Try again later.")
    body_text, _ = templates.build(org, b.get("template") or "notice", base_of(request), b)
    msg = send_text(org, user, numbers, body_text, running_host=request.get_host())
    log(org, user, f"sent a text message to {len(msg.recipients)} {'person' if len(msg.recipients) == 1 else 'people'}")
    return {"ok": True, "message": message_json(msg), "credits": account(org).credits}


@endpoint("POST", login_required=True)
def org_sms_buy(request, user, ip, slug):
    """Buy credits: pay now through PayNova, or ask the platform to add them (e.g. paid in cash)."""
    org, m = access(user, slug, "payments.manage")
    cfg = providers.config()
    b = body(request)
    n = b.get("credits")
    lo = cfg.get("min_credits") or 1
    if isinstance(n, bool) or not isinstance(n, int) or not lo <= n <= 1_000_000:
        raise ApiError(400, f"Buy from {lo:,} to 1,000,000 credits.")
    each = price(cfg)
    amount = paynova.money(each * n)
    currency = cfg.get("currency") or "GHS"
    method = b.get("method") if b.get("method") in ("paynova", "request") else "paynova"
    if method == "paynova" and (amount <= 0 or not paynova.ready()):
        raise ApiError(400, "Paying online isn't available yet. Use “Ask the platform to add them” instead, or contact the platform administrator.")
    if SmsPurchase.objects.filter(org=org, status=SmsPurchase.PENDING).count() >= 5:
        raise ApiError(429, "You already have 5 open credit requests. Finish or cancel one first.")
    p = SmsPurchase.objects.create(org=org, credits=n, amount=amount, currency=currency, method=method, requested_by=user)
    if method == "paynova":
        base = site_url(request)
        back = f"{base}/app/org/{org.slug}/messages?purchase={p.id}"
        try:
            pay = paynova.initialize_payment(amount, currency, f"{n:,} SMS credits for {org.name}", success_url=back, cancel_url=back,
                                             metadata={"kind": "sms_credits", "purchase": p.id, "org": org.slug}, email=user.email or "")
        except paynova.PayNovaError as e:
            p.delete()
            raise ApiError(502, str(e)) from None
        p.reference, p.checkout_url = pay["reference"], pay["checkout_url"]
        p.save(update_fields=["reference", "checkout_url"])
    log(org, user, f"{'started buying' if method == 'paynova' else 'asked the platform for'} {n:,} SMS credits")
    return {"ok": True, "purchase": purchase_json(p)}


@endpoint("POST", login_required=True)
def org_sms_purchase(request, user, ip, slug, purchase_id, action):
    org, m = access(user, slug, "payments.manage")
    p = SmsPurchase.objects.filter(org=org, id=purchase_id).first()
    if not p:
        raise ApiError(404, "Purchase not found.")
    if action == "cancel":
        if p.status != SmsPurchase.PENDING:
            raise ApiError(400, "Only open requests can be cancelled.")
        p.status = SmsPurchase.CANCELLED
        p.save(update_fields=["status"])
        return {"ok": True}
    if hit(f"smscheck:{org.id}", 10, 60):
        raise ApiError(429, "Checked a moment ago. Try again in a minute.")
    try:
        added = check_purchase(p)
    except paynova.PayNovaError as e:
        raise ApiError(502, str(e)) from None
    p.refresh_from_db()
    return {"ok": True, "added": added, "purchase": purchase_json(p), "credits": account(org).credits}


# ---------- super admin ----------
@admin("GET")
def admin_sms(request, user, ip):
    cfg = providers.config()
    return {"ready": providers.ready(cfg), "provider": cfg.get("provider") or None,
            "accounts": [{"org": {"id": a.org_id, "name": a.org.name, "slug": a.org.slug}, "credits": a.credits}
                         for a in SmsAccount.objects.select_related("org").order_by("-credits")[:200]],
            "orgs": [{"id": o.id, "name": o.name} for o in Organization.objects.order_by("name")[:500]],
            "requests": [purchase_json(p, True) for p in SmsPurchase.objects.filter(status=SmsPurchase.PENDING).select_related("org", "requested_by")],
            "recent": [purchase_json(p, True) for p in SmsPurchase.objects.exclude(status=SmsPurchase.PENDING).select_related("org", "requested_by")[:50]],
            "blocked": [{**message_json(x), "org": {"name": x.org.name, "slug": x.org.slug}} for x in SmsMessage.objects.filter(status=SmsMessage.BLOCKED).select_related("org", "sender")[:100]],
            "sentToday": SmsMessage.objects.filter(status=SmsMessage.SENT, created__gte=timezone.now() - timedelta(days=1)).count()}


@admin("POST")
def admin_sms_purchase(request, user, ip, purchase_id, action):
    """grant: add the credits (e.g. paid in cash). reject: close the request. PayNova purchases add credits by themselves."""
    with transaction.atomic():
        p = SmsPurchase.objects.select_for_update().select_related("org").filter(id=purchase_id).first()
        if not p:
            raise ApiError(404, "Request not found.")
        if p.status != SmsPurchase.PENDING:
            raise ApiError(409, "This request has already been handled.")
        p.decided_by, p.done_at = user, timezone.now()
        if action == "grant":
            acc = account(p.org, lock=True)
            acc.credits += p.credits
            acc.save(update_fields=["credits"])
            p.status = SmsPurchase.GRANTED
        else:
            p.status, p.note = SmsPurchase.REJECTED, text(body(request), "note", 300) or "Rejected by the platform"
        p.save()
    audit(user.username, f"{'granted' if action == 'grant' else 'rejected'} {p.credits} SMS credits for {p.org.name}", ip, resource=f"org:{p.org.slug}")
    log(p.org, None, f"the platform {'added' if action == 'grant' else 'turned down'} {p.credits:,} SMS credits")
    return {"ok": True}


@admin("POST")
def admin_sms_credit(request, user, ip):
    """Add (or take away) credits for an organization directly."""
    b = body(request)
    org = Organization.objects.filter(id=b.get("org")).first()
    n = b.get("credits")
    if not org or isinstance(n, bool) or not isinstance(n, int) or not -1_000_000 <= n <= 1_000_000 or n == 0:
        raise ApiError(400, "Choose an organization and a number of credits.")
    with transaction.atomic():
        acc = account(org, lock=True)
        if acc.credits + n < 0:
            raise ApiError(400, f"{org.name} only has {acc.credits} credits.")
        acc.credits += n
        acc.save(update_fields=["credits"])
    audit(user.username, f"{'added' if n > 0 else 'removed'} {abs(n)} SMS credits for {org.name}", ip, resource=f"org:{org.slug}", new=text(b, "note", 200))
    return {"ok": True, "credits": acc.credits}


@admin("POST")
def admin_sms_test(request, user, ip):
    to = providers.normalize(text(body(request), "to", 30), providers.config().get("country_code"))
    if not to:
        raise ApiError(400, "Enter the phone number to send the test to.")
    try:
        providers.send([to], f"{site_url(request)}: SMS works.")
    except providers.SmsError as e:
        raise ApiError(502, str(e)) from None
    audit(user.username, "sent a test SMS", ip, resource="settings:sms")
    return {"ok": True}
