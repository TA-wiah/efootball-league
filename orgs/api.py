"""JSON API for accounts, organizations, members and invitations.

Authorization rule: every organization endpoint goes through `access()`, which loads the caller's membership of that
organization and checks the permission on the server. Non-members get 404, so private organizations stay invisible.
"""
import hashlib
import re
import secrets
from datetime import timedelta

from django.contrib.auth import logout
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.text import slugify

from league import emailer
from league.http import ApiError, base_url, body, check_login, endpoint, ms, start_session, text
from league.logic import EMAIL_RE, USER_RE, audit, hit, password_problem
from league.models import Admin

from superadmin import store
from superadmin.models import PlatformAnnouncement

from .models import ORG_KINDS, WEBSITE_DEFAULTS, Invitation, Membership, Organization, OrgEvent, OrgPage
from .permissions import (ADMIN, COACH, OWNER, PLAYER, RANK, TEAM_MANAGER, ROLE_INFO, STAFF_ROLES, TEAM_ROLES, assignable_roles, can, can_manage, clean_overrides,
                          matrix, perms_of)

INVITE_DAYS = 7
MAX_ORGS_PER_USER = 25


# ---------- helpers ----------
def log(org, actor, action, old=None, new=None):
    """The organization's own activity feed, mirrored into the platform audit log."""
    who = getattr(actor, "username", actor or "")[:150]
    OrgEvent.objects.create(org=org, actor=who, action=action[:300])
    audit(who, f"{action} ({org.name})", resource=f"organization:{org.slug}", old=old, new=new)


def access(user, slug, perm=None):
    """The organization and the caller's membership, or 404 (not a member) / 403 (role lacks `perm`)."""
    m = Membership.objects.select_related("org").filter(org__slug=slug, user=user).first() if user else None
    if not m:
        raise ApiError(404, "Organization not found.")
    if m.org.status == "suspended":
        raise ApiError(403, "This organization has been suspended by the platform. Contact support.", suspended=True)
    if perm and not can(m.role, perm, m.org):
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) doesn't allow that.")
    now = timezone.now()
    if not m.last_active or now - m.last_active > timedelta(minutes=5):
        m.last_active = now
        m.save(update_fields=["last_active"])
    return m.org, m


def hash_token(t):
    return hashlib.sha256(t.encode()).hexdigest()


def mask(email):
    name, _, domain = email.partition("@")
    return (name[:1] + "***@" + domain) if domain else ""


def role_json(role):
    return {"role": role, "roleLabel": ROLE_INFO[role][0]}


KIND_L = dict(ORG_KINDS)
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def org_logo(org):
    return f"/media/org/{org.id}/logo?v={org.logo_version}" if org.logo_data else None


def org_json(org):
    return {"id": org.id, "name": org.name, "slug": org.slug, "description": org.description, "country": org.country,
            "region": org.region, "created": ms(org.created), "status": org.status, "kind": org.kind, "kindLabel": KIND_L.get(org.kind, "Organization"),
            "timezone": org.timezone, "brandColor": org.brand_color or None, "logo": org_logo(org), "website": org.site()}


def org_card(m):
    """An organization as shown on "My organizations": counts and last activity included."""
    from competitions.models import Competition, Player, Team
    o = m.org
    last = OrgEvent.objects.filter(org=o).order_by("-id").values_list("ts", flat=True).first()
    return {**org_json(o), **role_json(m.role), "members": o.memberships.count(),
            "staff": o.memberships.filter(role__in=STAFF_ROLES).count(),
            "competitions": Competition.objects.filter(org=o).count(), "activeCompetitions": Competition.objects.filter(org=o, status="active").count(),
            "teams": Team.objects.filter(org=o).count(), "players": Player.objects.filter(team__org=o).count(), "lastActivity": ms(last),
            "perms": perms_of(m.role, o)}


def member_json(m, me):
    staff = can(me.role, "members.manage", me.org)
    return {"id": m.id, "username": m.user.username, "email": m.user.email if (staff or m.user_id == me.user_id) else None,
            **role_json(m.role), "status": "active", "joined": ms(m.joined), "lastActive": ms(m.last_active),
            "you": m.user_id == me.user_id, "canManage": m.user_id != me.user_id and can_manage(me.role, m.role, me.org),
            "staff": m.role in STAFF_ROLES, "teams": [{"id": t.id, "name": t.name} for t in m.teams.all()] if m.role in TEAM_ROLES or m.role == PLAYER else []}


def invitation_json(i):
    return {"id": i.id, "kind": "email" if i.email else "link", "email": i.email or None, **role_json(i.role), "status": i.state,
            "created": ms(i.created), "expires": ms(i.expires), "invitedBy": getattr(i.invited_by, "username", None),
            "acceptedBy": getattr(i.accepted_by, "username", None), "acceptedAt": ms(i.accepted_at),
            "teams": [{"id": t.id, "name": t.name} for t in i.teams.all()]}


def team_invite_roles(m):
    """Roles this member may invite into a team: staff with members.invite (incl. team managers), otherwise team staff
    with team.invite may bring in coaches and players. Always strictly below their own role."""
    if can(m.role, "members.invite", m.org):
        pool = [TEAM_MANAGER, COACH, PLAYER]
    elif can(m.role, "team.invite", m.org):
        pool = [COACH, PLAYER]
    else:
        return []
    allowed = assignable_roles(m.role)
    return [r for r in pool if r in allowed]


def create_invitation(request, org, user, role, email, teams=()):
    if email:
        if not EMAIL_RE.fullmatch(email):
            raise ApiError(400, "Enter a valid email address, or leave it empty to create an invitation link.")
        if org.memberships.filter(user__email__iexact=email).exists():
            raise ApiError(409, "Someone with that email is already a member.")
        if org.invitations.filter(email__iexact=email, status=Invitation.PENDING, expires__gt=timezone.now()).exists():
            raise ApiError(409, "That email already has a pending invitation. Resend or revoke it below.")
    if hit(f"orginvite:{org.id}", 50, 3600):
        raise ApiError(429, "Too many invitations. Try again later.")
    with transaction.atomic():
        inv = Invitation(org=org, email=email, role=role, invited_by=user)
        token = new_invitation_token(inv)
        inv.save()
        if teams:
            inv.teams.set(teams)
    link = f"{base_url(request)}/invite/{token}"
    emailed, note = send_invitation_email(request, inv, link)
    where = f" to {', '.join(t.name for t in teams)}" if teams else ""
    log(org, user, f"invited {email or 'someone with a link'} as {ROLE_INFO[role][0]}{where}")
    return {"ok": True, "invitation": invitation_json(inv), "link": link, "emailed": emailed,
            "note": note or ("" if emailed else "Copy the link and send it yourself (WhatsApp, Telegram, email…).")}


def account_json(user):
    if not user:
        return None
    return {"id": user.id, "username": user.username, "email": user.email or None, "leagueAccess": user.league_access,
            "superAdmin": user.is_superuser, "firstName": user.first_name, "lastName": user.last_name, "phone": user.phone or None}


def unique_slug(name):
    base = slugify(name)[:50].strip("-") or "organization"
    slug, n = base, 2
    while Organization.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def my_pending_invitations(user):
    if not user or not user.email:
        return Invitation.objects.none()
    return (Invitation.objects.select_related("org", "invited_by")
            .filter(email__iexact=user.email, status=Invitation.PENDING, expires__gt=timezone.now())
            .exclude(org__memberships__user=user))


def send_invitation_email(request, inv, link):
    if not inv.email or not emailer.ready():
        return False, None
    label = ROLE_INFO[inv.role][0]
    by = getattr(inv.invited_by, "username", "Someone")
    txt, html = emailer.body(f"Join {inv.org.name}", [f"{by} invited you to join {inv.org.name} as {label}.",
                             f"As {label}: {ROLE_INFO[inv.role][1]}", f"The invitation expires in {INVITE_DAYS} days."],
                             link, "Accept invitation", "If you weren't expecting this, you can ignore this email.")
    try:
        emailer.send(inv.email, f"You're invited to join {inv.org.name}", txt, html)
        return True, None
    except emailer.MailError as e:
        return False, f"The email could not be sent ({e}). Share the link instead."


def new_invitation_token(inv):
    token = secrets.token_urlsafe(32)
    inv.token_hash = hash_token(token)
    inv.expires = timezone.now() + timedelta(days=INVITE_DAYS)
    inv.status = Invitation.PENDING
    return token


def join(user, inv):
    """Turn a pending invitation into a membership (all checks included)."""
    with transaction.atomic():
        inv = Invitation.objects.select_for_update().select_related("org").get(pk=inv.pk)
        if inv.state != Invitation.PENDING:
            raise ApiError(410, {"accepted": "This invitation has already been used.", "revoked": "This invitation was cancelled.",
                                 "expired": "This invitation has expired. Ask for a new one."}[inv.state])
        if inv.email and (user.email or "").lower() != inv.email.lower():
            raise ApiError(403, f"This invitation was sent to {mask(inv.email)}. Log in with the account that uses that email to accept it.")
        if Membership.objects.filter(org=inv.org, user=user).exists():
            raise ApiError(409, f"You're already a member of {inv.org.name}.")
        mem = Membership.objects.create(org=inv.org, user=user, role=inv.role, last_active=timezone.now())
        teams = list(inv.teams.filter(org=inv.org))
        if teams:
            mem.teams.add(*teams)
        inv.status, inv.accepted_by, inv.accepted_at = Invitation.ACCEPTED, user, timezone.now()
        inv.save(update_fields=["status", "accepted_by", "accepted_at"])
    log(inv.org, user, f"joined as {ROLE_INFO[inv.role][0]}")
    return inv.org


# ---------- accounts ----------
@endpoint("GET")
def me(request, user, ip):
    orgs = []
    if user:
        for m in Membership.objects.select_related("org").filter(user=user).order_by("org__name"):
            orgs.append(org_card(m))
    site = store.site()
    news = [{"id": a.id, "title": a.title, "body": a.body, "level": a.level} for a in PlatformAnnouncement.objects.filter(active=True).order_by("-created")[:3]] if user else []
    return {"user": account_json(user), "csrf": get_token(request), "orgs": orgs, "site": {"name": site["name"], "signups": site["allow_signups"],
            "notice": site["notice"], "supportEmail": site["support_email"]}, "announcements": news,
            "invitations": my_pending_invitations(user).count() if user else 0}


PHONE_RE = re.compile(r"^\+?[0-9 ]{9,20}$")


@endpoint("GET", "PATCH", login_required=True)
def account(request, user, ip):
    """Your own profile. Changing the username or email needs your current password."""
    if request.method == "GET":
        return {"user": account_json(user), "joined": ms(user.date_joined),
                "orgs": [{"name": m.org.name, "slug": m.org.slug, **role_json(m.role)} for m in Membership.objects.select_related("org").filter(user=user).order_by("org__name")]}
    b = body(request)
    old = {"username": user.username, "email": user.email}
    if "firstName" in b:
        user.first_name = text(b, "firstName", 60)
    if "lastName" in b:
        user.last_name = text(b, "lastName", 60)
    if "phone" in b:
        phone = text(b, "phone", 30)
        if phone and not PHONE_RE.fullmatch(phone):
            raise ApiError(400, "Enter the phone number with digits only, e.g. +233241234567.")
        user.phone = phone.replace(" ", "")
    username, email = text(b, "username", 64) if "username" in b else user.username, text(b, "email", 254).lower() if "email" in b else user.email
    if username != user.username or email != (user.email or ""):
        pw = b.get("password") if isinstance(b.get("password"), str) else ""
        if not pw or not user.check_password(pw):
            if hit(f"pwcheck:{user.id}", 5, 900):
                raise ApiError(429, "Too many wrong passwords. Try again in 15 minutes.")
            raise ApiError(403, "Enter your current password to change your username or email.")
        if not USER_RE.fullmatch(username):
            raise ApiError(400, "Username: 3–32 letters, numbers, dot, dash or underscore.")
        if not EMAIL_RE.fullmatch(email):
            raise ApiError(400, "Enter a valid email address.")
        if Admin.objects.exclude(id=user.id).filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
            raise ApiError(409, "That username is taken.")
        if Admin.objects.exclude(id=user.id).filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
            raise ApiError(409, "An account with that email already exists.")
        user.username, user.email = username, email
    try:
        user.save()
    except IntegrityError:
        raise ApiError(409, "That username or email is taken.") from None
    new = {"username": user.username, "email": user.email}
    audit(user.username, "updated their profile", ip, resource=f"user:{user.username}", old=old if old != new else None, new=new if old != new else None)
    return {"ok": True, "user": account_json(user)}


@endpoint("POST", login_required=True)
def account_password(request, user, ip):
    """Change your password. Other devices are signed out; this one stays signed in."""
    b = body(request)
    current = b.get("current") if isinstance(b.get("current"), str) else ""
    new = b.get("new") if isinstance(b.get("new"), str) else ""
    if hit(f"pwchange:{user.id}", 5, 900):
        raise ApiError(429, "Too many attempts. Try again in 15 minutes.")
    if not user.check_password(current):
        audit(user.username, "password change: wrong current password", ip, resource=f"user:{user.username}", status="denied")
        raise ApiError(403, "Your current password isn't right.")
    problem = password_problem(new, user)
    if problem:
        raise ApiError(400, problem)
    if user.check_password(new):
        raise ApiError(400, "Choose a password you haven't been using.")
    user.set_password(new)
    user.session_epoch += 1
    user.must_change = False
    user.save(update_fields=["password", "session_epoch", "must_change"])
    start_session(request, user)
    audit(user.username, "changed their password", ip, resource=f"user:{user.username}")
    return {"ok": True, "csrf": get_token(request)}


@endpoint("GET")
def roles(request, user, ip):
    return {**matrix(), "kinds": [{"key": k, "label": v} for k, v in ORG_KINDS]}


SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,48}[a-z0-9])$")


@endpoint("GET", login_required=True)
def slug_check(request, user, ip):
    """For the setup wizard: is this public address free?"""
    s = (request.GET.get("slug") or "").strip().lower()
    ok = bool(SLUG_RE.match(s)) and not Organization.objects.filter(slug=s).exists()
    return {"slug": s, "valid": bool(SLUG_RE.match(s)), "available": ok,
            "suggestion": s if ok else unique_slug(request.GET.get("name") or s or "organization")}


@endpoint("POST")
def signup(request, user, ip):
    if not store.site()["allow_signups"]:
        raise ApiError(403, "New sign-ups are closed at the moment. Ask an organizer for an invitation.")
    b = body(request)
    username, email = text(b, "username", 64), text(b, "email", 254)
    password = b.get("password") if isinstance(b.get("password"), str) else ""
    if not USER_RE.fullmatch(username):
        raise ApiError(400, "Username: 3–32 letters, numbers, dot, dash or underscore.")
    if not EMAIL_RE.fullmatch(email):
        raise ApiError(400, "Enter a valid email address.")
    if Admin.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
        raise ApiError(409, "That username is taken.")
    if Admin.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
        raise ApiError(409, "An account with that email already exists. Log in instead.")
    problem = password_problem(password, Admin(username=username, email=email))
    if problem:
        raise ApiError(400, problem)
    if hit("signup:" + ip, 20, 3600):     # generous: a whole team may sign up from one Wi-Fi
        raise ApiError(429, "Too many new accounts from here. Try again later.")
    a = Admin(username=username, email=email, role=Admin.ADMIN, league_access=False)
    a.set_password(password)
    try:
        a.save()
    except IntegrityError:
        raise ApiError(409, "That username or email is taken.") from None
    start_session(request, a)
    audit(a.username, "signed up", ip, resource=f"user:{a.username}", device=request.META.get("HTTP_USER_AGENT", ""))
    return {"ok": True, "user": account_json(a)}


@endpoint("POST")
def login_view(request, user, ip):
    b = body(request)
    password = b["password"][:200] if isinstance(b.get("password"), str) else ""
    a = check_login(text(b, "user", 254), password, ip)
    start_session(request, a)
    audit(a.username, "logged in", ip, resource=f"user:{a.username}", device=request.META.get("HTTP_USER_AGENT", ""))
    return {"ok": True, "user": account_json(a)}


@endpoint("POST")
def logout_view(request, user, ip):
    logout(request)
    return {"ok": True}


# ---------- organizations ----------
@endpoint("GET", "POST", login_required=True)
def orgs(request, user, ip):
    if request.method == "GET":
        return {"orgs": [{**org_json(m.org), **role_json(m.role)} for m in
                         Membership.objects.select_related("org").filter(user=user).order_by("org__name")]}
    b = body(request)
    name = text(b, "name", 80)
    if len(name) < 2:
        raise ApiError(400, "Give the organization a name (at least 2 characters).")
    site = store.site()
    if Membership.objects.filter(user=user, role=OWNER).count() >= site["max_orgs_per_user"] and not user.is_superuser:
        raise ApiError(400, f"You can own at most {site['max_orgs_per_user']} organizations.")
    if hit(f"neworg:{user.id}", 10, 3600):
        raise ApiError(429, "Too many new organizations. Try again later.")
    kind = b.get("kind") if b.get("kind") in KIND_L else "league"
    slug = text(b, "slug", 50).lower()
    if slug:
        if not SLUG_RE.match(slug):
            raise ApiError(400, "The web address can use lowercase letters, numbers and dashes (3–50 characters).")
        if Organization.objects.filter(slug=slug).exists():
            raise ApiError(409, "That web address is taken. Try another.")
    color = text(b, "brandColor", 7)
    if color and not HEX_RE.match(color):
        raise ApiError(400, "Choose a colour like #2f7bff.")
    tz = clean_tz(text(b, "timezone", 60))
    website = clean_website(b.get("website") or {})
    with transaction.atomic():
        org = Organization.objects.create(name=name, slug=slug or unique_slug(name), description=text(b, "description", 2000),
                                          country=text(b, "country", 60), region=text(b, "region", 60), created_by=user,
                                          kind=kind, brand_color=color.lower(), timezone=tz, website=website,
                                          status="pending" if site["require_org_approval"] and not user.is_superuser else "active")
        Membership.objects.create(org=org, user=user, role=OWNER, last_active=timezone.now())
    log(org, user, "created the organization" + (" (waiting for approval)" if org.status == "pending" else ""))
    return {"ok": True, "org": org_json(org), "pending": org.status == "pending"}


@endpoint("GET", "PATCH", "DELETE", login_required=True)
def org_detail(request, user, ip, slug):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        now = timezone.now()
        pending = org.invitations.filter(status=Invitation.PENDING, expires__gt=now).count()
        return {"org": org_json(org), "me": {**role_json(m.role), "perms": perms_of(m.role, org), "assignable": assignable_roles(m.role),
                                             "teams": list(m.teams.values_list("id", flat=True))},
                "stats": {"members": org.memberships.count(), "pendingInvitations": pending},
                "events": [{"ts": ms(e.ts), "actor": e.actor, "action": e.action} for e in org.events.order_by("-id")[:15]]}
    if request.method == "PATCH":
        org, m = access(user, slug, "org.settings")
        b = body(request)
        if "name" in b:
            name = text(b, "name", 80)
            if len(name) < 2:
                raise ApiError(400, "The name needs at least 2 characters.")
            org.name = name
        for f, limit in (("description", 2000), ("country", 60), ("region", 60)):
            if f in b:
                setattr(org, f, text(b, f, limit))
        if "kind" in b:
            if b["kind"] not in KIND_L:
                raise ApiError(400, "Unknown organization type.")
            org.kind = b["kind"]
        if "brandColor" in b:
            color = text(b, "brandColor", 7)
            if color and not HEX_RE.match(color):
                raise ApiError(400, "Choose a colour like #2f7bff.")
            org.brand_color = color.lower()
        if "timezone" in b:
            org.timezone = clean_tz(text(b, "timezone", 60))
        if "website" in b:
            org.website = {**org.site(), **clean_website(b["website"])}
        org.save()
        log(org, user, "updated the organization settings")
        return {"ok": True, "org": org_json(org)}
    org, m = access(user, slug, "org.delete")
    if text(body(request), "confirm", 80) != org.name:
        raise ApiError(400, "Type the organization's exact name to confirm.")
    audit(user.username, f"deleted organization {org.name} ({org.slug})", ip)
    org.delete()
    return {"ok": True}


@endpoint("GET", login_required=True)
def activity(request, user, ip, slug):
    """The organization's full activity log, newest first, 50 at a time (?before=<id> for older)."""
    org, m = access(user, slug, "org.view")
    rows = org.events.order_by("-id")
    before = request.GET.get("before", "")
    if before.isdigit():
        rows = rows.filter(id__lt=int(before))
    rows = list(rows[:50])
    return {"events": [{"id": e.id, "ts": ms(e.ts), "actor": e.actor, "action": e.action} for e in rows],
            "more": len(rows) == 50}


@endpoint("POST", login_required=True)
def transfer(request, user, ip, slug):
    org, m = access(user, slug, "org.transfer")
    target = org.memberships.select_related("user").filter(id=body(request).get("memberId")).exclude(user=user).first()
    if not target:
        raise ApiError(400, "Choose a current member to become the owner.")
    with transaction.atomic():
        m.role = ADMIN
        m.save(update_fields=["role"])            # the old owner becomes an Administrator (only one owner at a time)
        target.role = OWNER
        target.save(update_fields=["role"])
    log(org, user, f"transferred ownership to {target.user.username}")
    return {"ok": True}


@endpoint("POST", login_required=True)
def leave(request, user, ip, slug):
    org, m = access(user, slug)
    if m.role == OWNER:
        raise ApiError(400, "The owner can't leave. Transfer ownership to someone else first.")
    m.delete()
    log(org, user, "left the organization")
    return {"ok": True}


# ---------- members ----------
@endpoint("GET", login_required=True)
def members(request, user, ip, slug):
    org, m = access(user, slug, "members.view")
    rows = org.memberships.select_related("user").all()
    rows = sorted(rows, key=lambda x: (-RANK[x.role], x.user.username.lower()))
    return {"members": [member_json(x, m) for x in rows]}


@endpoint("PATCH", "DELETE", login_required=True)
def member_detail(request, user, ip, slug, member_id):
    org, m = access(user, slug, "members.manage")
    target = org.memberships.select_related("user").filter(id=member_id).first()
    if not target:
        raise ApiError(404, "Member not found.")
    if target.user_id == user.id:
        raise ApiError(400, "You can't change or remove yourself here. Use “Leave organization” instead.")
    if not can_manage(m.role, target.role, org):
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) can't manage a {ROLE_INFO[target.role][0]}.")
    if request.method == "DELETE":
        target.delete()
        log(org, user, f"removed {target.user.username}")
        return {"ok": True}
    b = body(request)
    if "role" in b:
        role = text(b, "role", 20)
        if role not in assignable_roles(m.role):
            raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) can't give the {ROLE_INFO.get(role, ('that',))[0]} role.")
        old = target.role
        target.role = role
        target.save(update_fields=["role"])
        log(org, user, f"changed {target.user.username}'s role", old=ROLE_INFO[old][0], new=ROLE_INFO[role][0])
    if "teams" in b:
        ids = b["teams"]
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ApiError(400, "Send a list of team ids.")
        from competitions.models import Team
        teams = list(Team.objects.filter(org=org, id__in=ids))
        if len(teams) != len(set(ids)):
            raise ApiError(400, "Choose teams from this organization.")
        target.teams.set(teams)
        log(org, user, f"set team access for {target.user.username}", new=[t.name for t in teams])
    return {"ok": True}


@endpoint("GET", "PATCH", login_required=True)
def org_permissions(request, user, ip, slug):
    """Roles & permissions: everyone can read it; only the owner can change it."""
    if request.method == "GET":
        org, m = access(user, slug, "members.view")
        return {**matrix(org), "canEdit": can(m.role, "roles.manage", org)}
    org, m = access(user, slug, "roles.manage")
    b = body(request)
    if b.get("reset") is True:
        old, org.permissions = org.permissions, {}
    else:
        try:
            changes = clean_overrides(b.get("permissions"))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        old = org.permissions
        org.permissions = {**(org.permissions or {}), **changes}
    org.save(update_fields=["permissions"])
    log(org, user, "changed role permissions", old=old, new=org.permissions)
    return {**matrix(org), "canEdit": True}


@endpoint("POST", "DELETE", login_required=True)
def org_logo_upload(request, user, ip, slug):
    org, m = access(user, slug, "org.settings")
    if request.method == "DELETE":
        org.logo_data, org.logo_type = None, ""
    else:
        from competitions.engine import decode_logo
        try:
            org.logo_data, org.logo_type = decode_logo(body(request, 400_000).get("image"))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
    org.logo_version += 1
    org.save(update_fields=["logo_data", "logo_type", "logo_version"])
    log(org, user, "updated the logo")
    return {"ok": True, "logo": org_logo(org)}


def clean_tz(tz):
    if not tz:
        return ""
    import zoneinfo
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ApiError(400, "Unknown time zone.") from None
    return tz


def clean_website(w):
    if not isinstance(w, dict):
        raise ApiError(400, "Bad website settings.")
    out = {}
    for k, v in w.items():
        if k not in WEBSITE_DEFAULTS:
            raise ApiError(400, f"Unknown website setting: {k}")
        if isinstance(WEBSITE_DEFAULTS[k], bool):
            if not isinstance(v, bool):
                raise ApiError(400, f"{k} must be on or off.")
            out[k] = v
        else:
            if not isinstance(v, str):
                raise ApiError(400, f"{k} must be text.")
            out[k] = v.strip()[:160 if k == "tagline" else 3000]
    return out


# ---------- invitations (managed by the organization) ----------
@endpoint("GET", "POST", login_required=True)
def invitations(request, user, ip, slug):
    org, m = access(user, slug, "members.invite")
    if request.method == "GET":
        rows = org.invitations.select_related("invited_by", "accepted_by").prefetch_related("teams").order_by("-id")[:200]
        return {"invitations": [invitation_json(i) for i in rows], "assignable": assignable_roles(m.role)}
    b = body(request)
    role, email = text(b, "role", 20), text(b, "email", 254).lower()
    if role not in assignable_roles(m.role):
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) can't invite people as {ROLE_INFO.get(role, ('that role',))[0]}.")
    return create_invitation(request, org, user, role, email)


@endpoint("POST", login_required=True)
def invitation_action(request, user, ip, slug, inv_id, action):
    org, m = access(user, slug, "org.view")
    inv = org.invitations.select_related("invited_by").filter(id=inv_id).first()
    if not can(m.role, "members.invite", org):
        # team staff may only touch invitations to their own teams
        mine = inv and inv.role in team_invite_roles(m) and inv.teams.exists() and not inv.teams.exclude(id__in=m.teams.values("id")).exists()
        if not mine:
            raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) doesn't allow that.")
    if not inv:
        raise ApiError(404, "Invitation not found.")
    if RANK[inv.role] >= RANK[m.role]:
        raise ApiError(403, "You can't manage an invitation for a role at or above your own.")
    if action == "revoke":
        if inv.state != Invitation.PENDING:
            raise ApiError(400, "Only pending invitations can be revoked.")
        inv.status = Invitation.REVOKED
        inv.save(update_fields=["status"])
        log(org, user, f"revoked the invitation for {inv.email or 'a link'}")
        return {"ok": True, "invitation": invitation_json(inv)}
    # resend: a fresh link and expiry date (the old link stops working)
    if inv.state not in (Invitation.PENDING, Invitation.EXPIRED):
        raise ApiError(400, "Only pending or expired invitations can be sent again.")
    token = new_invitation_token(inv)
    inv.save(update_fields=["token_hash", "expires", "status"])
    link = f"{base_url(request)}/invite/{token}"
    emailed, note = send_invitation_email(request, inv, link)
    log(org, user, f"renewed the invitation for {inv.email or 'a link'}")
    return {"ok": True, "invitation": invitation_json(inv), "link": link, "emailed": emailed, "note": note or ""}


# ---------- invitations (the person invited) ----------
def find_invitation(ip, token):
    if hit("invtok:" + ip, 60, 900):
        raise ApiError(429, "Too many attempts. Try again later.")
    inv = None
    if re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token or ""):
        inv = Invitation.objects.select_related("org", "invited_by").filter(token_hash=hash_token(token)).first()
    if not inv:
        raise ApiError(404, "This invitation link isn't valid. Check you copied all of it.")
    return inv


@endpoint("GET")
def invitation_public(request, user, ip, token):
    inv = find_invitation(ip, token)
    return {"org": {"name": inv.org.name, "slug": inv.org.slug}, **role_json(inv.role), "roleDescription": ROLE_INFO[inv.role][1],
            "invitedBy": getattr(inv.invited_by, "username", None), "email": mask(inv.email) if inv.email else None,
            "kind": "email" if inv.email else "link", "status": inv.state,
            "alreadyMember": bool(user and Membership.objects.filter(org=inv.org, user=user).exists())}


@endpoint("POST", login_required=True)
def invitation_accept(request, user, ip, token):
    org = join(user, find_invitation(ip, token))
    return {"ok": True, "org": org_json(org)}


@endpoint("GET", login_required=True)
def my_invitations(request, user, ip):
    return {"invitations": [{"id": i.id, "org": {"name": i.org.name, "slug": i.org.slug}, **role_json(i.role),
                             "invitedBy": getattr(i.invited_by, "username", None), "expires": ms(i.expires)}
                            for i in my_pending_invitations(user)]}


@endpoint("POST", login_required=True)
def my_invitation_accept(request, user, ip, inv_id):
    inv = my_pending_invitations(user).filter(id=inv_id).first()
    if not inv:
        raise ApiError(404, "Invitation not found.")
    org = join(user, inv)
    return {"ok": True, "org": org_json(org)}




# ---------- the organization's own website pages ----------
RESERVED_PAGES = {"admin", "api", "app", "new", "edit"}


def page_json(pg):
    return {"id": pg.id, "slug": pg.slug, "title": pg.title, "body": pg.body, "published": pg.published, "position": pg.position,
            "updated": ms(pg.updated), "url": f"/org/{pg.org.slug}/p/{pg.slug}"}


def apply_page(org, pg, b):
    if "title" in b:
        title = text(b, "title", 80)
        if len(title) < 2:
            raise ApiError(400, "Give the page a title.")
        pg.title = title
    if "body" in b:
        if not isinstance(b["body"], str) or len(b["body"]) > 20000:
            raise ApiError(400, "Keep the page under 20,000 characters.")
        pg.body = b["body"].replace("\r\n", "\n").strip()
    if "published" in b:
        pg.published = b["published"] is True
    if "position" in b and isinstance(b["position"], int) and 0 <= b["position"] <= 99:
        pg.position = b["position"]
    if not pg.slug:
        base = slugify(pg.title)[:50].strip("-") or "page"
        base = base + "-page" if base in RESERVED_PAGES else base
        slug, n = base, 2
        while org.pages.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        pg.slug = slug


@endpoint("GET", "POST", login_required=True)
def org_pages(request, user, ip, slug):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        return {"pages": [page_json(p) for p in org.pages.all()]}
    org, m = access(user, slug, "org.settings")
    if org.pages.count() >= 30:
        raise ApiError(400, "An organization can have up to 30 pages.")
    pg = OrgPage(org=org, position=org.pages.count())
    apply_page(org, pg, {"title": "", **body(request, 30_000)})
    pg.save()
    log(org, user, f"added the website page “{pg.title}”")
    return {"ok": True, "page": page_json(pg)}


@endpoint("PATCH", "DELETE", login_required=True)
def org_page_detail(request, user, ip, slug, page_id):
    org, m = access(user, slug, "org.settings")
    pg = org.pages.filter(id=page_id).first()
    if not pg:
        raise ApiError(404, "Page not found.")
    if request.method == "DELETE":
        pg.delete()
        log(org, user, f"deleted the website page “{pg.title}”")
        return {"ok": True}
    apply_page(org, pg, body(request, 30_000))
    pg.save()
    return {"ok": True, "page": page_json(pg)}
