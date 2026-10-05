"""The JSON API used by public/index.html, plus the page itself."""
import functools
import hashlib
import json
import logging
import re
import secrets
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.hashers import make_password
from django.core.exceptions import RequestDataTooBig
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone

from . import emailer
from .logic import (EMAIL_RE, SLOT_RE, USER_RE, audit, clear, deal_groups, force_save, hit, over, password_problem,
                    read_state, record_draw, save_if_current, seed, shuffle, valid_state)
from .models import Admin, Audit, Draw, Token

log = logging.getLogger("league")
BACKEND = "django.contrib.auth.backends.ModelBackend"
GENERIC = "Wrong username or password."
CONFLICT = "Another editor changed the league at the same time. The latest version has been loaded, so please redo your last change."


# ---------- helpers ----------
class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def ip_of(request):
    if settings.TRUST_PROXY:
        fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.META.get("REMOTE_ADDR", "")


def body(request, max_len=10_000):
    if "application/json" not in request.META.get("CONTENT_TYPE", ""):
        raise ApiError(415, "json only")
    try:
        raw = request.body
    except RequestDataTooBig:
        raise ApiError(413, "too big") from None
    if len(raw) > max_len:
        raise ApiError(413, "too big")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ApiError(400, "bad json") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad json")
    return data


def text(b, key, limit):
    v = b.get(key)
    return v.strip()[:limit] if isinstance(v, str) else ""


def ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def pub(a):
    return {"id": a.id, "username": a.username, "email": a.email or None, "role": a.role, "pending": a.pending,
            "mustChange": a.must_change, "lastLogin": ms(a.last_login), "created": ms(a.date_joined), "invitedBy": a.invited_by or None}


def current(request):
    """The signed-in admin, or None. Also enforces "log out on all devices"."""
    u = request.user
    if not u.is_authenticated or not u.is_active or not u.has_usable_password():
        return None
    if request.session.get("epoch") != u.session_epoch:
        logout(request)
        return None
    return u


def start_session(request, a):
    login(request, a, backend=BACKEND)        # new session id + new CSRF token
    request.session["epoch"] = a.session_epoch


def api(method, auth=False, before_password_change=False):
    """Wrap a view: method check, optional login requirement, JSON errors."""
    def wrap(view):
        @functools.wraps(view)
        def inner(request):
            if request.method != method:
                return JsonResponse({"error": "not found"}, status=404)
            me = current(request)
            if auth and not me:
                return JsonResponse({"error": "login required"}, status=401)
            if auth and me.must_change and not before_password_change:
                return JsonResponse({"error": "Choose a new password first."}, status=403)
            try:
                result = view(request, me, ip_of(request))
            except ApiError as e:
                return JsonResponse({"error": e.message, **e.extra}, status=e.status)
            return result if isinstance(result, HttpResponse) else JsonResponse(result)
        return inner
    return wrap


def new_token(a, kind, ttl):
    t = secrets.token_hex(32)
    Token.objects.filter(admin=a, kind=kind).delete()
    Token.objects.create(hash=hashlib.sha256(t.encode()).hexdigest(), admin=a, kind=kind, expires=timezone.now() + ttl)
    return t


def find_token(request, b):
    if hit("tok:" + ip_of(request), 30, 900):
        raise ApiError(429, "Too many attempts. Try again later.")
    t = b.get("token")
    row = None
    if isinstance(t, str) and re.fullmatch(r"[0-9a-f]{64}", t):
        row = Token.objects.select_related("admin").filter(hash=hashlib.sha256(t.encode()).hexdigest()).first()
    if not row or row.expires < timezone.now():
        raise ApiError(400, "This link is invalid or has expired. Ask for a new one.")
    return row


def base_url(request):
    return settings.APP_URL or request.build_absolute_uri("/").rstrip("/")


def csrf_failure(request, reason=""):
    return JsonResponse({"error": "Session check failed. Reload the page."}, status=403)


def not_found(request, exception=None):
    return JsonResponse({"error": "not found"}, status=404)


def server_error(request):
    return JsonResponse({"error": "server error"}, status=500)


# ---------- the page ----------
_page = None


def index(request):
    global _page
    if request.method != "GET":
        return not_found(request)
    if _page is None or settings.DEBUG:
        _page = settings.INDEX_FILE.read_text(encoding="utf-8")
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
           "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return HttpResponse(_page.replace("<script>", f'<script nonce="{nonce}">'), content_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


# ---------- league ----------
@api("GET")
def state(request, me, ip):
    return read_state()


@api("PUT", auth=True)
def save_state(request, me, ip):
    s = body(request, 1_000_000)
    s.pop("rev", None)
    if not valid_state(s):
        raise ApiError(400, "bad state")
    prev = read_state()
    if str(request.headers.get("X-Rev")) != str(prev["rev"]):
        raise ApiError(409, CONFLICT, conflict=True)
    if s.get("kd") is not None and json.dumps(s["kd"]) != json.dumps(prev.get("kd")):
        raise ApiError(409, 'The knockout draw can only be made with the "Draw the knockouts" button.')
    if prev.get("dl"):
        s["dl"] = prev["dl"]          # the latest-draw marker is set by the server only
    else:
        s.pop("dl", None)
    rev = save_if_current(s, prev["rev"])
    if rev is None:
        raise ApiError(409, CONFLICT, conflict=True)
    if not hit(f"save:{me.id}", 1, 300):  # at most one log line per 5 minutes
        audit(me.username, "edited the league", ip)
    return {"ok": True, "rev": rev}


@api("GET", auth=True)
def seed_view(request, me, ip):
    return seed()


# ---------- login ----------
@api("GET")
def me_view(request, me, ip):
    csrf = get_token(request)
    if me:
        return {"admin": True, "user": pub(me), "csrf": csrf, "smtp": emailer.ready()}
    return {"admin": False, "resetEnabled": emailer.ready() and bool(settings.APP_URL), "csrf": csrf}


@api("POST")
def login_view(request, me, ip):
    if over("ip:" + ip, 10, 900):
        raise ApiError(429, "Too many failed attempts. Try again in 15 minutes.")
    b = body(request)
    ident = text(b, "user", 254)
    pw = b["password"][:200] if isinstance(b.get("password"), str) else ""
    a = Admin.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first() if ident else None
    acct = f"acct:{a.id if a else ident.lower()}"
    if over(acct, 5, 900):
        raise ApiError(429, "This account is locked for 15 minutes after too many failed attempts.")
    if a is None or not a.has_usable_password():
        make_password(pw)                     # same amount of work, so timing doesn't reveal unknown users
        good = False
    else:
        good = a.check_password(pw)
    if not good:
        hit("ip:" + ip, 10, 900)
        hit(acct, 5, 900)
        audit(ident or "?", "failed login", ip)
        raise ApiError(401, GENERIC)
    clear(acct)
    start_session(request, a)
    audit(a.username, "logged in", ip)
    return {"ok": True, "user": pub(a)}


@api("POST")
def logout_view(request, me, ip):
    logout(request)
    return {"ok": True}


@api("POST", auth=True, before_password_change=True)
def logout_all(request, me, ip):
    me.session_epoch += 1
    me.save(update_fields=["session_epoch"])
    audit(me.username, "logged out everywhere", ip)
    logout(request)
    return {"ok": True}


@api("POST", auth=True, before_password_change=True)
def change_password(request, me, ip):
    if hit(f"pwchg:{me.id}", 10, 900):
        raise ApiError(429, "Too many attempts. Try again later.")
    b = body(request)
    cur = b.get("current") if isinstance(b.get("current"), str) else ""
    if not me.check_password(cur[:200]):
        raise ApiError(400, "Current password is wrong.")
    new = b.get("password")
    problem = password_problem(new, me)
    if problem:
        raise ApiError(400, problem)
    if new == cur:
        raise ApiError(400, "Pick a password different from the current one.")
    me.set_password(new)
    me.must_change = False
    me.save()
    Token.objects.filter(admin=me).delete()
    update_session_auth_hash(request, me)     # keeps this device signed in; every other device is logged out
    audit(me.username, "changed password", ip)
    return {"ok": True}


@api("POST")
def forgot(request, me, ip):
    b = body(request)
    ident = text(b, "user", 254)
    answer = {"ok": True, "message": "If that account has an email address, a reset link is on its way."}
    if not emailer.ready() or not settings.APP_URL:
        raise ApiError(400, "Password reset by email is not set up. Ask the owner to help.")
    if hit("forgot:" + ip, 5, 3600):
        return answer
    a = Admin.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first() if ident else None
    if a and a.email and a.has_usable_password() and not hit(f"forgot:{a.id}", 3, 3600):
        link = f"{settings.APP_URL}/#reset={new_token(a, 'reset', timedelta(hours=1))}"
        txt, html = emailer.body("Reset your password", [f"Hi {a.username},", "Someone (hopefully you) asked to reset your admin password. The link works once and expires in 1 hour."],
                                 link, "Choose a new password", "If you didn't ask for this you can ignore this email; your password stays the same.")
        audit(a.username, "reset email requested", ip)

        def deliver(to=a.email, user=a.username):   # in the background, so response time doesn't reveal which accounts exist
            try:
                emailer.send(to, f"Reset your {emailer.SITE} password", txt, html)
            except emailer.MailError as e:
                log.error("Reset email to %s failed: %s", user, e)
        threading.Thread(target=deliver, daemon=True).start()
    return answer


@api("POST")
def token_check(request, me, ip):
    row = find_token(request, body(request))
    return {"kind": row.kind, "username": row.admin.username}


@api("POST")
def token_use(request, me, ip):
    b = body(request)
    row = find_token(request, b)
    a = row.admin
    problem = password_problem(b.get("password"), a)
    if problem:
        raise ApiError(400, problem)
    a.set_password(b["password"])
    a.must_change = False
    a.save()
    Token.objects.filter(admin=a).delete()   # every link for this admin is now used up
    audit(a.username, "accepted invite" if row.kind == "invite" else "reset password by email", ip)
    start_session(request, a)
    return {"ok": True}


# ---------- draws ----------
@api("GET")
def draws(request, me, ip):
    """Only the current draws: the latest group draw, plus the knockout draw made after it."""
    g = Draw.objects.filter(kind="groups").order_by("-id").first()
    k = Draw.objects.filter(kind="knockout", id__gt=g.id if g else 0).order_by("-id").first()
    out = []
    for d in (k, g):
        if d:
            role = d.role or getattr(Admin.objects.filter(username=d.by).first(), "role", "admin")
            out.append({"id": d.id, "ts": d.ts, "by": d.by, "role": role, "kind": d.kind, "result": json.loads(d.result)})
    return {"draws": out}


@api("POST", auth=True)
def draw_groups(request, me, ip):
    b = body(request, 20_000)
    n = b.get("groups")
    players = b.get("players") if isinstance(b.get("players"), list) else []
    names = [str(x).strip() for x in players if str(x).strip()]
    if not (isinstance(n, int) and not isinstance(n, bool) and 2 <= n <= 8):
        raise ApiError(400, "Choose 2 to 8 groups.")
    seen = set()
    for name in names:
        if len(name) > 40:
            raise ApiError(400, f"Name too long: {name[:20]}…")
        if name.lower() in seen:
            raise ApiError(400, f'"{name}" is on the list twice.')
        seen.add(name.lower())
    if len(names) < n * 2:
        raise ApiError(400, f"You need at least {n * 2} players for {n} groups (2 per group).")
    if len(names) > n * 6:
        raise ApiError(400, f"Too many players: at most {n * 6} for {n} groups (6 per group).")
    g, order = deal_groups(names, n, bool(b.get("pots")))
    with transaction.atomic():
        prev = read_state()
        dl = record_draw(me, "groups", {"groups": g, "order": order, "pots": bool(b.get("pots"))})
        s = {"g": g, "r": {}, "k": {}, "st": {}, "ev": {}, "cfg": prev.get("cfg") or {}, "ui": prev.get("ui") or {},
             "aw": {"bd": "", "c": [{"t": c.get("t", ""), "n": ""} for c in (prev.get("aw") or {}).get("c", []) if isinstance(c, dict)]}, "dl": dl}
        force_save(s)
    audit(me.username, f"ran the group draw (#{dl['id']})", ip)
    return {"state": s, "draw": dl, "result": {"groups": g, "order": order, "pots": bool(b.get("pots"))}}


@api("POST", auth=True)
def draw_knockout(request, me, ip):
    b = body(request)
    with transaction.atomic():
        prev = read_state()
        if prev.get("kd"):
            raise ApiError(409, "The knockouts have already been drawn.")
        pots = b.get("pots")
        ok = isinstance(pots, list) and len(pots) == 3 and all(
            isinstance(p, list) and len(p) <= 8 and all(
                isinstance(x, dict) and isinstance(x.get("k"), str) and SLOT_RE.fullmatch(x["k"]) and x["k"][0] in prev.get("g", {})
                and isinstance(x.get("n"), str) and len(x["n"]) <= 40 for x in p) for p in pots)
        keys = [x["k"] for p in pots for x in p] if ok else []
        if not ok or len(keys) < 2 or len(set(keys)) != len(keys):
            raise ApiError(400, "bad draw request")
        drawn = [shuffle([{"k": x["k"], "n": x["n"]} for x in p]) for p in pots]
        prev["kd"] = [x["k"] for p in drawn for x in p]
        prev["k"] = {}
        prev["ev"] = {k: v for k, v in (prev.get("ev") or {}).items() if not k.startswith("k.")}
        prev["dl"] = record_draw(me, "knockout", {"pots": drawn})
        force_save(prev)
    audit(me.username, f"ran the knockout draw (#{prev['dl']['id']})", ip)
    return {"state": prev, "draw": prev["dl"], "result": {"pots": drawn}}


# ---------- admins ----------
def send_invite(request, me, a, ip):
    link = f"{base_url(request)}/#invite={new_token(a, 'invite', timedelta(hours=48))}"
    if not emailer.ready():
        return {"ok": True, "emailed": False, "link": link, "note": "Email is not set up, so send this link yourself. It expires in 48 hours."}
    txt, html = emailer.body("You're invited as an admin",
                             [f"Hi {a.username},", f"{me.username} added you as an admin of {emailer.SITE}. Choose your password to get started. The link works once and expires in 48 hours.", f"Your username: {a.username}"],
                             link, "Set my password", "If you weren't expecting this, ignore the email.")
    try:
        emailer.send(a.email, f"You're invited to run {emailer.SITE}", txt, html)
        return {"ok": True, "emailed": True}
    except emailer.MailError as e:
        log.error("Invite email failed: %s", e)
        audit(me.username, f"invite email to {a.username} FAILED", ip)
        return {"ok": True, "emailed": False, "link": link, "note": f"The email could not be sent ({e}). Share this link instead. It expires in 48 hours."}


@api("GET", auth=True)
def admins(request, me, ip):
    people = sorted(Admin.objects.all(), key=lambda a: (a.role != Admin.OWNER, a.date_joined))
    logrows = [{"ts": ms(r.ts), "actor": r.actor, "action": r.action, "ip": r.ip} for r in Audit.objects.order_by("-id")[:40]] if me.role == Admin.OWNER else []
    return {"admins": [pub(a) for a in people], "log": logrows, "smtp": emailer.ready()}


@api("POST", auth=True)
def invite(request, me, ip):
    if hit(f"invite:{me.id}", 20, 3600):
        raise ApiError(429, "Too many invites. Try again later.")
    b = body(request)
    username, email = text(b, "username", 64), text(b, "email", 254)
    if not USER_RE.fullmatch(username):
        raise ApiError(400, "Username: 3–32 letters, numbers, dot, dash or underscore.")
    if not EMAIL_RE.fullmatch(email):
        raise ApiError(400, "Enter a valid email address.")
    if Admin.objects.filter(Q(username__iexact=username) | Q(email__iexact=email) | Q(username__iexact=email) | Q(email__iexact=username)).exists():
        raise ApiError(409, "An admin with that username or email already exists.")
    a = Admin(username=username, email=email, role=Admin.ADMIN, invited_by=me.username)
    a.set_unusable_password()
    a.save()
    audit(me.username, f"invited {username} <{email}>", ip)
    return send_invite(request, me, a, ip)


@api("POST", auth=True)
def resend(request, me, ip):
    if hit(f"invite:{me.id}", 20, 3600):
        raise ApiError(429, "Too many invites. Try again later.")
    b = body(request)
    a = Admin.objects.filter(id=b.get("id")).first() if isinstance(b.get("id"), int) else None
    if not a or not a.pending:
        raise ApiError(400, "That admin has already joined.")
    audit(me.username, f"re-sent invite to {a.username}", ip)
    return send_invite(request, me, a, ip)


@api("POST", auth=True)
def remove(request, me, ip):
    b = body(request)
    a = Admin.objects.filter(id=b.get("id")).first() if isinstance(b.get("id"), int) else None
    if not a:
        raise ApiError(404, "Not found")
    if a.role == Admin.OWNER:
        raise ApiError(403, "The owner can't be removed.")
    if me.role != Admin.OWNER and a.id != me.id and not (a.pending and a.invited_by == me.username):
        raise ApiError(403, "Only the owner can remove other admins.")
    leaving = a.id == me.id
    name = a.username
    a.delete()
    audit(me.username, "left the admin team" if leaving else f"removed admin {name}", ip)
    return {"ok": True}


@api("POST", auth=True)
def set_email(request, me, ip):
    email = text(body(request), "email", 254)
    if not EMAIL_RE.fullmatch(email):
        raise ApiError(400, "Enter a valid email address.")
    if Admin.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exclude(id=me.id).exists():
        raise ApiError(409, "That email is already used.")
    me.email = email
    me.save(update_fields=["email"])
    audit(me.username, "changed email", ip)
    return {"ok": True}


@api("POST", auth=True)
def test_email(request, me, ip):
    if not me.email:
        raise ApiError(400, "Add your own email address first.")
    if hit(f"smtptest:{me.id}", 5, 3600):
        raise ApiError(429, "Too many test emails. Try again later.")
    txt, html = emailer.body("Email works ✔", [f"Hi {me.username},", "Your mail settings are working."], base_url(request), "Open the league", "Sent from the admin panel.")
    try:
        emailer.send(me.email, f"{emailer.SITE}: test email", txt, html)
    except emailer.MailError as e:
        raise ApiError(502, f"Sending failed: {e}") from None
    return {"ok": True}
