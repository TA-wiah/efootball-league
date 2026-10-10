"""Super admin API: the whole platform, for platform owners only.

Every endpoint is wrapped in `admin()`, which requires a logged-in user with is_superuser on the server.
Anyone else gets 403 (and the attempt is written to the audit log), whatever URL they type.
"""
import re
import platform as py_platform
from email.utils import parseaddr
from datetime import date, datetime, timedelta

import django
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek
from django.utils import timezone

from competitions import engine
from competitions.api import comp_json, table_payload
from competitions.models import Announcement, Competition, Entry, Match, MatchEvent, Player, Team
from league import emailer
from league.http import ApiError, body, endpoint, ms, site_url, text
from league.logic import EMAIL_RE, audit
from league.models import Audit
from orgs.models import Invitation, Membership, Organization, OrgEvent
from orgs.permissions import ROLE_INFO, matrix

from . import store
from .models import PlatformAnnouncement, SupportTicket

User = get_user_model()
PER_PAGE = 25
STAFF = ("owner", "organizer", "admin")
GOALS = ("goal", "penalty_goal")


# ---------- access & helpers ----------
def admin(*methods):
    """Super admins only, checked on the server for every request."""
    def wrap(view):
        def guarded(request, user, ip, **kw):
            if not user.is_superuser:
                audit(user.username, f"blocked from {request.method} {request.path}", ip, resource="admin", status="denied")
                raise ApiError(403, "Super admins only.")
            return view(request, user, ip, **kw)
        guarded.__name__ = view.__name__
        return endpoint(*methods, login_required=True)(guarded)
    return wrap


def paged(request, qs, row, per=PER_PAGE):
    p = Paginator(qs, per).get_page(request.GET.get("page"))
    return {"items": [row(x) for x in p.object_list], "page": p.number, "pages": p.paginator.num_pages, "total": p.paginator.count}


def q_of(request):
    return (request.GET.get("q") or "").strip()[:80]


def day_of(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        raise ApiError(400, "Dates must look like 2026-10-05.") from None


def who(u):
    return {"id": u.id, "username": u.username} if u else None


def user_status(u):
    if not u.is_active:
        return "suspended"
    if not u.has_usable_password():
        return "pending"
    return "active"


def user_row(u):
    return {"id": u.id, "username": u.username, "email": u.email or None, "status": user_status(u), "superAdmin": u.is_superuser,
            "orgs": getattr(u, "org_count", None), "joined": ms(u.date_joined), "lastSeen": ms(u.last_seen or u.last_login)}


def org_row(o):
    owner = Membership.objects.filter(org=o, role="owner").select_related("user").first()
    return {"id": o.id, "name": o.name, "slug": o.slug, "status": o.status, "owner": who(owner.user) if owner else None,
            "members": getattr(o, "member_count", None), "competitions": getattr(o, "comp_count", None), "teams": getattr(o, "team_count", None),
            "matches": getattr(o, "match_count", None), "created": ms(o.created), "where": ", ".join(x for x in (o.region, o.country) if x)}


def comp_row(c):
    return {"id": c.id, "name": c.name, "slug": c.slug, "org": {"name": c.org.name, "slug": c.org.slug, "id": c.org_id},
            "organizer": who(c.created_by), "season": c.season, "teams": getattr(c, "team_count", None),
            "matches": getattr(c, "match_count", None), "played": getattr(c, "played_count", None), "status": c.status,
            "visibility": c.visibility, "suspended": c.suspended, "featured": c.featured, "format": c.format, "created": ms(c.created)}


def team_brief(t):
    return {"id": t.id, "name": t.name, "slug": t.slug} if t else None


def match_row(m):
    return {"id": m.id, "slug": m.slug, "competition": {"id": m.competition_id, "name": m.competition.name, "slug": m.competition.slug},
            "org": m.competition.org.name, "home": team_brief(m.home.team if m.home else None), "away": team_brief(m.away.team if m.away else None),
            "kickoff": ms(m.kickoff), "venue": m.venue, "status": m.status, "homeScore": m.home_score, "awayScore": m.away_score,
            "round": m.round_name or f"Round {m.round}", "group": m.group, "stage": m.stage}


def audit_row(a):
    return {"id": a.id, "ts": ms(a.ts), "actor": a.actor, "action": a.action, "resource": a.resource, "status": a.status,
            "ip": a.ip, "device": a.device, "old": a.old, "new": a.new}


MATCHES = Match.objects.select_related("competition__org", "home__team", "away__team")
COMPS = Competition.objects.select_related("org", "created_by")


def counts():
    now, today = timezone.now(), timezone.localdate()
    d30 = now - timedelta(days=30)
    m = Match.objects
    return {
        "totalUsers": User.objects.count(),
        "activeUsers": User.objects.filter(is_active=True).filter(Q(last_seen__gte=d30) | Q(last_login__gte=d30)).count(),
        "newUsers": User.objects.filter(date_joined__gte=d30).count(),
        "suspendedUsers": User.objects.filter(is_active=False).count(),
        "totalOrganizations": Organization.objects.count(),
        "activeOrganizations": Organization.objects.filter(status="active").count(),
        "totalCompetitions": Competition.objects.count(),
        "activeCompetitions": Competition.objects.filter(status="active", suspended=False).count(),
        "upcomingCompetitions": Competition.objects.filter(Q(status="draft") | Q(start_date__gt=today)).exclude(status="completed").count(),
        "completedCompetitions": Competition.objects.filter(status="completed").count(),
        "totalTeams": Team.objects.count(),
        "totalPlayers": Player.objects.count(),
        "totalMatches": m.count(),
        "upcomingMatches": m.filter(status="scheduled").filter(Q(kickoff__gte=now) | Q(kickoff__isnull=True)).count(),
        "completedMatches": m.filter(status="finished").count(),
        "liveMatches": m.filter(status="live").count(),
        "pendingResults": m.filter(status="scheduled", kickoff__lt=now).count(),
        "totalGroups": Entry.objects.exclude(group="").values("competition_id", "group").distinct().count(),
        "pendingInvitations": Invitation.objects.filter(status="pending", expires__gt=now).count(),
        "pendingApprovals": Organization.objects.filter(status="pending").count(),
        "supportTickets": SupportTicket.objects.exclude(status="closed").count(),
    }


def health():
    import time
    t0 = time.perf_counter()
    db_ok = True
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        db_ok = False
    from django.db.migrations.executor import MigrationExecutor
    try:
        ex = MigrationExecutor(connection)
        pending = len(ex.migration_plan(ex.loader.graph.leaf_nodes()))
    except Exception:
        pending = None
    cfg = emailer.config()
    day = timezone.now() - timedelta(days=1)
    return {"database": {"ok": db_ok, "engine": connection.vendor, "ms": round((time.perf_counter() - t0) * 1000, 1),
                         "persistent": bool(settings.DATABASES["default"]["ENGINE"].endswith("postgresql"))},
            "migrationsPending": pending, "email": {"ready": emailer.ready(cfg), "provider": cfg["provider"] or None, "source": cfg["source"]},
            "failedLogins24h": Audit.objects.filter(ts__gte=day, action="failed login").count(),
            "blockedAdminAttempts24h": Audit.objects.filter(ts__gte=day, resource="admin", status="denied").count(),
            "secretKeyFromEnv": bool(__import__("os").environ.get("SECRET_KEY")), "appUrl": site_url() or None}


# ---------- dashboard ----------
@admin("GET")
def overview(request, user, ip):
    return {"stats": counts(), "health": health(), "activity": [audit_row(a) for a in Audit.objects.order_by("-id")[:15]],
            "pendingOrganizations": [org_row(o) for o in Organization.objects.filter(status="pending").order_by("-created")[:5]],
            "openTickets": SupportTicket.objects.exclude(status="closed").count()}


RANGES = {"today": 0, "7d": 7, "30d": 30, "3m": 91, "6m": 183}


@admin("GET")
def analytics(request, user, ip):
    g = request.GET
    today = timezone.localdate()
    r = g.get("range", "30d")
    if r == "custom":
        start, end = day_of(g.get("from")), day_of(g.get("to"))
        if not start or not end or end < start:
            raise ApiError(400, "Choose a start and end date.")
        if (end - start).days > 3660:
            raise ApiError(400, "Pick a range of 10 years or less.")
    elif r == "year":
        start, end = date(today.year, 1, 1), today
    else:
        start, end = today - timedelta(days=RANGES.get(r, 30)), today
    span = (end - start).days
    unit, trunc = ("day", TruncDay) if span <= 92 else ("week", TruncWeek) if span <= 400 else ("month", TruncMonth)
    tz = timezone.get_current_timezone()
    lo = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz)

    def buckets():
        out, d = [], start
        if unit == "week":
            d = start - timedelta(days=start.weekday())
        elif unit == "month":
            d = start.replace(day=1)
        while d <= end:
            out.append(d)
            d = d + timedelta(days=1 if unit == "day" else 7) if unit != "month" else (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        return out

    keys = buckets()

    def series(qs, field):
        rows = (qs.filter(**{f"{field}__gte": lo, f"{field}__lt": hi}).annotate(b=trunc(field)).values("b").annotate(n=Count("id")).order_by("b"))
        found = {(x["b"].date() if hasattr(x["b"], "date") else x["b"]): x["n"] for x in rows}
        values = [found.get(k, 0) for k in keys]
        return {"values": values, "total": sum(values)}

    data = {"users": ("New users", series(User.objects.all(), "date_joined")),
            "organizations": ("New organizations", series(Organization.objects.all(), "created")),
            "competitions": ("Competitions created", series(Competition.objects.all(), "created")),
            "teams": ("Teams registered", series(Team.objects.all(), "created")),
            "matchesCreated": ("Matches created", series(Match.objects.all(), "created")),
            "matchesCompleted": ("Matches completed", series(Match.objects.filter(status="finished"), "finished_at")),
            "activity": ("Platform activity (audit events)", series(Audit.objects.all(), "ts"))}
    return {"range": r, "from": start.isoformat(), "to": end.isoformat(), "unit": unit, "labels": [k.isoformat() for k in keys],
            "series": [{"key": k, "label": label, **v} for k, (label, v) in data.items()],
            "activeCompetitions": Competition.objects.filter(status="active", suspended=False).count()}


@admin("GET")
def search(request, user, ip):
    q = q_of(request)
    if len(q) < 2:
        return {"q": q, "results": {}}
    users = User.objects.filter(Q(username__icontains=q) | Q(email__icontains=q)).order_by("username")[:8]
    orgs = Organization.objects.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(region__icontains=q)).order_by("name")[:8]
    comps = COMPS.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(season__icontains=q) | Q(org__name__icontains=q)).order_by("name")[:8]
    teams = Team.objects.select_related("org").filter(Q(name__icontains=q) | Q(city__icontains=q)).order_by("name")[:8]
    players = Player.objects.select_related("team").filter(name__icontains=q).order_by("name")[:8]
    matches = MATCHES.filter(Q(home__team__name__icontains=q) | Q(away__team__name__icontains=q) | Q(venue__icontains=q)).order_by(F("kickoff").desc(nulls_last=True))[:8]
    groups = (Entry.objects.exclude(group="").filter(Q(competition__name__icontains=q) | Q(group__iexact=q.replace("Group ", "").strip()))
              .values("competition_id", "competition__name", "group").annotate(n=Count("id")).order_by("competition__name", "group")[:8])
    return {"q": q, "results": {
        "users": [user_row(u) for u in users], "organizations": [org_row(o) for o in orgs], "competitions": [comp_row(c) for c in comps],
        "teams": [{"id": t.id, "name": t.name, "org": t.org.name, "suspended": t.suspended} for t in teams],
        "players": [{"id": p.id, "name": p.name, "team": p.team.name, "teamId": p.team_id} for p in players],
        "matches": [match_row(m) for m in matches],
        "groups": [{"competitionId": x["competition_id"], "competition": x["competition__name"], "group": x["group"], "teams": x["n"]} for x in groups]}}


# ---------- users ----------
@admin("GET")
def users(request, user, ip):
    st, q = request.GET.get("status", "all"), q_of(request)
    qs = User.objects.annotate(org_count=Count("memberships", distinct=True))
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q))
    if st == "active":
        qs = qs.filter(is_active=True).exclude(password__startswith="!")
    elif st == "suspended":
        qs = qs.filter(is_active=False)
    elif st == "pending":
        qs = qs.filter(password__startswith="!")      # invited, hasn't chosen a password yet
    elif st == "superadmin":
        qs = qs.filter(is_superuser=True)
    elif st == "recent":
        qs = qs.filter(Q(last_seen__gte=timezone.now() - timedelta(days=30)))
    return paged(request, qs.order_by("-date_joined"), user_row)


def user_of(uid):
    u = User.objects.filter(id=uid).first()
    if not u:
        raise ApiError(404, "User not found.")
    return u


@admin("GET", "DELETE")
def user_detail(request, user, ip, uid):
    u = user_of(uid)
    if request.method == "DELETE":
        if u.id == user.id:
            raise ApiError(400, "You can't delete your own account here.")
        if text(body(request), "confirm", 150) != u.username:
            raise ApiError(400, "Type the exact username to confirm.")
        if Membership.objects.filter(user=u, role="owner").exists():
            raise ApiError(409, f"{u.username} owns organizations. Transfer or delete those first.")
        if u.is_superuser and User.objects.filter(is_superuser=True, is_active=True).exclude(id=u.id).count() == 0:
            raise ApiError(409, "That's the last super admin.")
        name = u.username
        u.delete()
        audit(user.username, "deleted user", ip, resource=f"user:{name}", old=name)
        return {"ok": True}
    ms_ = Membership.objects.select_related("org").filter(user=u).order_by("org__name")
    staff_orgs = [m.org_id for m in ms_ if m.role in STAFF]
    comps = COMPS.filter(org_id__in=staff_orgs).annotate(team_count=Count("entries", distinct=True), match_count=Count("matches", distinct=True),
                                                         played_count=Count("matches", filter=Q(matches__status="finished"), distinct=True))
    return {"user": {**user_row(u), "lastLogin": ms(u.last_login), "leagueAccess": u.league_access},
            "memberships": [{"org": {"id": m.org_id, "name": m.org.name, "slug": m.org.slug, "status": m.org.status}, "role": m.role,
                             "roleLabel": ROLE_INFO[m.role][0], "joined": ms(m.joined), "lastActive": ms(m.last_active)} for m in ms_],
            "competitions": [comp_row(c) for c in comps[:50]],
            "teams": Team.objects.filter(org_id__in=[m.org_id for m in ms_]).count(),
            "activity": [audit_row(a) for a in Audit.objects.filter(Q(actor=u.username) | Q(resource=f"user:{u.username}")).order_by("-id")[:30]],
            "tickets": [{"id": t.id, "subject": t.subject, "status": t.status, "created": ms(t.created)} for t in u.tickets.order_by("-id")[:10]]}


def user_action(u, action, actor, ip):
    if action in ("suspend", "revoke_superadmin") and u.id == actor.id:
        raise ApiError(400, "You can't do that to your own account.")
    if action in ("suspend", "revoke_superadmin") and u.is_superuser and \
            User.objects.filter(is_superuser=True, is_active=True).exclude(id=u.id).count() == 0:
        raise ApiError(409, "That's the last super admin.")
    old = {"status": user_status(u), "superAdmin": u.is_superuser}
    if action == "suspend":
        u.is_active = False
        u.session_epoch += 1                      # logs them out everywhere straight away
    elif action == "reactivate":
        u.is_active = True
    elif action == "grant_superadmin":
        u.is_superuser = u.is_staff = True
    elif action == "revoke_superadmin":
        u.is_superuser = u.is_staff = False
    elif action == "logout":
        u.session_epoch += 1
    else:
        raise ApiError(400, "Unknown action.")
    u.save()
    audit(actor.username, f"user {action.replace('_', ' ')}", ip, resource=f"user:{u.username}", old=old,
          new={"status": user_status(u), "superAdmin": u.is_superuser})


@admin("POST")
def user_act(request, user, ip, uid, action):
    u = user_of(uid)
    user_action(u, action, user, ip)
    return {"ok": True, "user": user_row(u)}


def bulk(request, user, ip, model, act, allowed):
    b = body(request)
    ids, action = b.get("ids"), b.get("action")
    if action not in allowed or not isinstance(ids, list) or not ids or len(ids) > 200 or not all(isinstance(i, int) for i in ids):
        raise ApiError(400, "Choose items and an action.")
    done, skipped = 0, []
    for obj in model.objects.filter(id__in=ids):
        try:
            act(obj, action, user, ip)
            done += 1
        except ApiError as e:
            skipped.append(f"{getattr(obj, 'username', getattr(obj, 'name', obj.id))}: {e.message}")
    return {"ok": True, "done": done, "skipped": skipped}


@admin("POST")
def users_bulk(request, user, ip):
    return bulk(request, user, ip, User, user_action, ("suspend", "reactivate", "logout"))


# ---------- organizations ----------
@admin("GET")
def organizations(request, user, ip):
    st, q = request.GET.get("status", "all"), q_of(request)
    qs = Organization.objects.annotate(member_count=Count("memberships", distinct=True), comp_count=Count("competitions", distinct=True),
                                       team_count=Count("teams", distinct=True), match_count=Count("competitions__matches", distinct=True))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(country__icontains=q) | Q(region__icontains=q))
    if st in ("active", "pending", "suspended"):
        qs = qs.filter(status=st)
    return paged(request, qs.order_by("-created"), org_row)


def org_of(oid):
    o = Organization.objects.filter(id=oid).first()
    if not o:
        raise ApiError(404, "Organization not found.")
    return o


@admin("GET", "DELETE")
def organization_detail(request, user, ip, oid):
    o = org_of(oid)
    if request.method == "DELETE":
        if text(body(request), "confirm", 80) != o.name:
            raise ApiError(400, "Type the exact organization name to confirm.")
        audit(user.username, "deleted organization", ip, resource=f"organization:{o.slug}", old=o.name)
        o.delete()
        return {"ok": True}
    comps = o.competitions.annotate(team_count=Count("entries", distinct=True), match_count=Count("matches", distinct=True),
                                    played_count=Count("matches", filter=Q(matches__status="finished"), distinct=True)).select_related("org", "created_by")
    o2 = Organization.objects.annotate(member_count=Count("memberships", distinct=True), comp_count=Count("competitions", distinct=True),
                                       team_count=Count("teams", distinct=True), match_count=Count("competitions__matches", distinct=True)).get(id=o.id)
    return {"org": {**org_row(o2), "description": o.description},
            "members": [{"id": m.id, "user": who(m.user), "role": m.role, "roleLabel": ROLE_INFO[m.role][0], "joined": ms(m.joined),
                         "lastActive": ms(m.last_active)} for m in o.memberships.select_related("user").order_by("-joined")],
            "competitions": [comp_row(c) for c in comps.order_by("-created")],
            "teams": [{"id": t.id, "name": t.name, "suspended": t.suspended, "players": t.players.count()} for t in o.teams.order_by("name")[:200]],
            "invitations": Invitation.objects.filter(org=o, status="pending", expires__gt=timezone.now()).count(),
            "activity": [{"ts": ms(e.ts), "actor": e.actor, "action": e.action} for e in OrgEvent.objects.filter(org=o).order_by("-id")[:40]]}


def org_action(o, action, actor, ip):
    old = o.status
    new = {"approve": "active", "reactivate": "active", "suspend": "suspended"}.get(action)
    if not new:
        raise ApiError(400, "Unknown action.")
    if action == "approve" and old != "pending":
        raise ApiError(400, "Only pending organizations need approval.")
    o.status = new
    o.save(update_fields=["status"])
    audit(actor.username, f"organization {action}", ip, resource=f"organization:{o.slug}", old=old, new=new)
    OrgEvent.objects.create(org=o, actor="platform", action=f"organization {'approved' if action == 'approve' else action + 'd' if action != 'suspend' else 'suspended'} by the platform")


@admin("POST")
def organization_act(request, user, ip, oid, action):
    o = org_of(oid)
    org_action(o, action, user, ip)
    return {"ok": True}


@admin("POST")
def organizations_bulk(request, user, ip):
    return bulk(request, user, ip, Organization, org_action, ("approve", "suspend", "reactivate"))


# ---------- competitions ----------
def comp_qs():
    return COMPS.annotate(team_count=Count("entries", distinct=True), match_count=Count("matches", distinct=True),
                          played_count=Count("matches", filter=Q(matches__status="finished"), distinct=True))


@admin("GET")
def competitions(request, user, ip):
    st, q, today = request.GET.get("status", "all"), q_of(request), timezone.localdate()
    qs = comp_qs()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(org__name__icontains=q) | Q(season__icontains=q))
    if request.GET.get("org"):
        qs = qs.filter(org__slug=request.GET["org"])
    if request.GET.get("tables"):                        # only competitions that have a league table
        qs = qs.exclude(format="knockout").exclude(kind="friendly")
    if st in ("active", "completed", "draft"):
        qs = qs.filter(status=st)
    elif st == "upcoming":
        qs = qs.filter(Q(status="draft") | Q(start_date__gt=today)).exclude(status="completed")
    elif st == "suspended":
        qs = qs.filter(suspended=True)
    elif st == "featured":
        qs = qs.filter(featured=True)
    return paged(request, qs.order_by("-created"), comp_row)


def comp_of(cid):
    c = comp_qs().filter(id=cid).first()
    if not c:
        raise ApiError(404, "Competition not found.")
    return c


@admin("GET")
def competition_detail(request, user, ip, cid):
    c = comp_of(cid)
    entries = list(c.entries.select_related("team").order_by("group", "team__name"))
    groups = {}
    for e in entries:
        groups.setdefault(e.group, []).append(e.team.name)
    events = MatchEvent.objects.select_related("player", "assist", "match__home__team", "match__away__team").filter(match__competition=c)
    return {"competition": {**comp_row(c), **comp_json(c, extra=True)},
            "teams": [{"id": e.team_id, "name": e.team.name, "group": e.group, "pointsAdjustment": e.points_adjustment,
                       "players": e.team.players.count(), "suspended": e.team.suspended} for e in entries],
            "groups": [{"name": g or None, "teams": t} for g, t in sorted(groups.items())],
            "matches": [match_row(m) for m in MATCHES.filter(competition=c).order_by("stage", "round", "kickoff", "id")],
            "standings": table_payload(c) if c.format != "knockout" and c.kind != "friendly" else None,
            "scorers": [{**s, "team": s["team"].name if s["team"] else None} for s in engine.scorers(events)[:20]],
            "staff": [{"user": who(m.user), "role": m.role, "roleLabel": ROLE_INFO[m.role][0]} for m in
                      Membership.objects.select_related("user").filter(org=c.org, role__in=STAFF + ("editor", "moderator"))],
            "activity": [audit_row(a) for a in Audit.objects.filter(Q(action__icontains=c.name) | Q(resource=f"competition:{c.slug}")).order_by("-id")[:30]]}


def comp_action(c, action, actor, ip):
    field, value = {"suspend": ("suspended", True), "unsuspend": ("suspended", False), "feature": ("featured", True),
                    "unfeature": ("featured", False)}.get(action, (None, None))
    if not field:
        raise ApiError(400, "Unknown action.")
    old = getattr(c, field)
    Competition.objects.filter(id=c.id).update(**{field: value})
    audit(actor.username, f"competition {action}", ip, resource=f"competition:{c.slug}", old={field: old}, new={field: value})


@admin("POST")
def competition_act(request, user, ip, cid, action):
    comp_action(comp_of(cid), action, user, ip)
    return {"ok": True}


@admin("POST")
def competitions_bulk(request, user, ip):
    return bulk(request, user, ip, Competition, comp_action, ("suspend", "unsuspend", "feature", "unfeature"))


# ---------- teams & players ----------
@admin("GET")
def teams(request, user, ip):
    st, q = request.GET.get("status", "all"), q_of(request)
    qs = Team.objects.select_related("org").annotate(player_count=Count("players", distinct=True), entry_count=Count("entries", distinct=True))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(city__icontains=q) | Q(org__name__icontains=q))
    if st == "registered":
        qs = qs.filter(entry_count__gt=0)
    elif st == "active":
        qs = qs.filter(entries__competition__status="active", suspended=False).distinct()
    elif st == "suspended":
        qs = qs.filter(suspended=True)
    return paged(request, qs.order_by("name"), lambda t: {"id": t.id, "name": t.name, "slug": t.slug, "org": {"name": t.org.name, "id": t.org_id},
                                                         "city": t.city, "players": t.player_count, "competitions": t.entry_count,
                                                         "suspended": t.suspended, "created": ms(t.created)})


def team_action(t, action, actor, ip):
    if action not in ("suspend", "unsuspend"):
        raise ApiError(400, "Unknown action.")
    Team.objects.filter(id=t.id).update(suspended=action == "suspend")
    audit(actor.username, f"team {action}", ip, resource=f"team:{t.slug}", old={"suspended": t.suspended}, new={"suspended": action == "suspend"})


@admin("POST")
def team_act(request, user, ip, tid, action):
    t = Team.objects.filter(id=tid).first()
    if not t:
        raise ApiError(404, "Team not found.")
    team_action(t, action, user, ip)
    return {"ok": True}


@admin("POST")
def teams_bulk(request, user, ip):
    return bulk(request, user, ip, Team, team_action, ("suspend", "unsuspend"))


@admin("GET")
def players(request, user, ip):
    q, st = q_of(request), request.GET.get("status", "all")
    goals = MatchEvent.objects.filter(player=OuterRef("pk"), kind__in=GOALS).values("player").annotate(c=Count("id")).values("c")
    cards = MatchEvent.objects.filter(player=OuterRef("pk"), kind__in=("yellow", "second_yellow", "red")).values("player").annotate(c=Count("id")).values("c")
    qs = Player.objects.select_related("team__org").annotate(goals=Coalesce(Subquery(goals, output_field=IntegerField()), Value(0)),
                                                             cards=Coalesce(Subquery(cards, output_field=IntegerField()), Value(0)))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(team__name__icontains=q))
    if st == "active":
        qs = qs.filter(active=True)
    elif st == "inactive":
        qs = qs.filter(active=False)
    order = ["-goals", "-cards", "name"] if request.GET.get("sort") == "activity" else ["team__name", "number", "name"]
    return paged(request, qs.order_by(*order), lambda p: {"id": p.id, "name": p.name, "number": p.number, "position": p.position,
                                                          "active": p.active, "team": {"id": p.team_id, "name": p.team.name},
                                                          "org": p.team.org.name, "goals": p.goals, "cards": p.cards})


# ---------- matches, fixtures, results, groups, tables ----------
@admin("GET")
def matches(request, user, ip):
    g, now = request.GET, timezone.now()
    qs = MATCHES.all()
    st = g.get("status", "all")
    if st in ("scheduled", "live", "finished", "postponed", "cancelled"):
        qs = qs.filter(status=st)
    elif st == "upcoming":
        qs = qs.filter(status="scheduled").filter(Q(kickoff__gte=now) | Q(kickoff__isnull=True))
    elif st == "pending":
        qs = qs.filter(status="scheduled", kickoff__lt=now)
    if g.get("competition"):
        qs = qs.filter(competition_id=g["competition"]) if g["competition"].isdigit() else qs.filter(competition__slug=g["competition"])
    if g.get("org"):
        qs = qs.filter(competition__org__slug=g["org"])
    if g.get("team"):
        qs = qs.filter(Q(home__team__name__icontains=g["team"]) | Q(away__team__name__icontains=g["team"]))
    if g.get("venue"):
        qs = qs.filter(venue__icontains=g["venue"])
    if g.get("from"):
        qs = qs.filter(kickoff__date__gte=day_of(g["from"]))
    if g.get("to"):
        qs = qs.filter(kickoff__date__lte=day_of(g["to"]))
    if q_of(request):
        q = q_of(request)
        qs = qs.filter(Q(home__team__name__icontains=q) | Q(away__team__name__icontains=q) | Q(competition__name__icontains=q))
    asc = st in ("scheduled", "upcoming", "pending", "postponed") or g.get("order") == "asc"
    qs = qs.order_by(F("kickoff").asc(nulls_last=True) if asc else F("kickoff").desc(nulls_last=True), "id")
    return paged(request, qs, match_row, per=40)


@admin("GET")
def result_changes(request, user, ip):
    qs = Audit.objects.filter(action__startswith="result ").order_by("-id")
    return paged(request, qs, audit_row, per=40)


@admin("GET")
def groups(request, user, ip):
    q = q_of(request)
    rows = Entry.objects.exclude(group="")
    if q:
        rows = rows.filter(Q(competition__name__icontains=q) | Q(competition__org__name__icontains=q))
    rows = (rows.values("competition_id", "competition__name", "competition__status", "competition__org__name", "group")
            .annotate(teams=Count("id")).order_by("competition__name", "group"))
    mcount = {(x["competition_id"], x["group"]): x for x in Match.objects.filter(stage="league").exclude(group="")
              .values("competition_id", "group").annotate(total=Count("id"), finished=Count("id", filter=Q(status="finished")))}
    out = [{"competitionId": r["competition_id"], "competition": r["competition__name"], "org": r["competition__org__name"],
            "group": r["group"], "teams": r["teams"], "matches": mcount.get((r["competition_id"], r["group"]), {}).get("total", 0),
            "finished": mcount.get((r["competition_id"], r["group"]), {}).get("finished", 0), "status": r["competition__status"]} for r in rows]
    p = Paginator(out, 40).get_page(request.GET.get("page"))
    return {"items": list(p.object_list), "page": p.number, "pages": p.paginator.num_pages, "total": p.paginator.count}


@admin("GET")
def standings(request, user, ip, cid):
    c = comp_of(cid)
    if c.format == "knockout" or c.kind == "friendly":
        raise ApiError(400, "Knockout-only competitions and friendly series have no table.")
    return table_payload(c)


# ---------- staff, invitations ----------
@admin("GET")
def staff(request, user, ip):
    role, q = request.GET.get("role", ""), q_of(request)
    qs = Membership.objects.select_related("user", "org")
    if role in ROLE_INFO:
        qs = qs.filter(role=role)
    if q:
        qs = qs.filter(Q(user__username__icontains=q) | Q(org__name__icontains=q))
    return paged(request, qs.order_by("org__name", "role"), lambda m: {"id": m.id, "user": who(m.user), "org": {"id": m.org_id, "name": m.org.name},
                                                                        "role": m.role, "roleLabel": ROLE_INFO[m.role][0], "joined": ms(m.joined),
                                                                        "lastActive": ms(m.last_active)})


@admin("GET")
def invitations(request, user, ip):
    st, now = request.GET.get("status", "all"), timezone.now()
    qs = Invitation.objects.select_related("org", "invited_by", "accepted_by")
    if st == "pending":
        qs = qs.filter(status="pending", expires__gt=now)
    elif st == "expired":
        qs = qs.filter(status="pending", expires__lte=now)
    elif st in ("accepted", "revoked"):
        qs = qs.filter(status=st)
    return paged(request, qs.order_by("-id"), lambda i: {"id": i.id, "org": {"id": i.org_id, "name": i.org.name}, "email": i.email or None,
                                                          "role": i.role, "roleLabel": ROLE_INFO[i.role][0], "status": i.state,
                                                          "invitedBy": getattr(i.invited_by, "username", None), "created": ms(i.created),
                                                          "expires": ms(i.expires)})


@admin("POST")
def invitation_revoke(request, user, ip, iid):
    i = Invitation.objects.filter(id=iid).first()
    if not i or i.state != "pending":
        raise ApiError(400, "Only pending invitations can be revoked.")
    i.status = "revoked"
    i.save(update_fields=["status"])
    audit(user.username, "revoked an invitation", ip, resource=f"organization:{i.org.slug}", old="pending", new="revoked")
    return {"ok": True}


# ---------- audit ----------
@admin("GET")
def audit_log(request, user, ip):
    g, q = request.GET, q_of(request)
    qs = Audit.objects.all()
    if q:
        qs = qs.filter(Q(actor__icontains=q) | Q(action__icontains=q) | Q(resource__icontains=q) | Q(ip__icontains=q))
    if g.get("status") in ("ok", "failed", "denied"):
        qs = qs.filter(status=g["status"])
    if g.get("resource"):
        qs = qs.filter(resource__startswith=g["resource"])
    if g.get("from"):
        qs = qs.filter(ts__date__gte=day_of(g["from"]))
    if g.get("to"):
        qs = qs.filter(ts__date__lte=day_of(g["to"]))
    return paged(request, qs.order_by("-id"), audit_row, per=50)


# ---------- announcements ----------
@admin("GET", "POST")
def announcements(request, user, ip):
    if request.method == "POST":
        b = body(request)
        title = text(b, "title", 140)
        if len(title) < 2:
            raise ApiError(400, "Give the announcement a title.")
        a = PlatformAnnouncement.objects.create(title=title, body=text(b, "body", 3000), level="warning" if b.get("level") == "warning" else "info",
                                                active=b.get("active", True) is not False, author=user)
        audit(user.username, "posted a platform announcement", ip, resource="announcement", new=title)
        return {"ok": True, "id": a.id}
    plat = [{"id": a.id, "title": a.title, "body": a.body, "level": a.level, "active": a.active, "created": ms(a.created),
             "author": getattr(a.author, "username", None)} for a in PlatformAnnouncement.objects.order_by("-created")[:100]]
    org = [{"id": a.id, "title": a.title, "body": a.body, "published": a.published, "org": a.org.name,
            "competition": a.competition.name if a.competition else None, "author": getattr(a.author, "username", None), "created": ms(a.created)}
           for a in Announcement.objects.select_related("org", "competition", "author").order_by("-created")[:100]]
    return {"platform": plat, "organizations": org}


@admin("PATCH", "DELETE")
def announcement_detail(request, user, ip, aid):
    a = PlatformAnnouncement.objects.filter(id=aid).first()
    if not a:
        raise ApiError(404, "Announcement not found.")
    if request.method == "DELETE":
        a.delete()
        audit(user.username, "deleted a platform announcement", ip, resource="announcement", old=a.title)
        return {"ok": True}
    b = body(request)
    if "active" in b:
        a.active = bool(b["active"])
    if "title" in b:
        a.title = text(b, "title", 140) or a.title
    if "body" in b:
        a.body = text(b, "body", 3000)
    a.save()
    return {"ok": True}


@admin("PATCH", "DELETE")
def org_announcement(request, user, ip, aid):
    a = Announcement.objects.select_related("org").filter(id=aid).first()
    if not a:
        raise ApiError(404, "Announcement not found.")
    if request.method == "DELETE":
        a.delete()
        audit(user.username, "deleted an organization announcement", ip, resource=f"organization:{a.org.slug}", old=a.title)
        return {"ok": True}
    a.published = bool(body(request).get("published"))
    a.save(update_fields=["published"])
    audit(user.username, f"{'published' if a.published else 'hid'} an organization announcement", ip, resource=f"organization:{a.org.slug}", new=a.title)
    return {"ok": True}


# ---------- settings & system ----------
@admin("GET", "PATCH")
def platform_settings(request, user, ip):
    if request.method == "PATCH":
        b = body(request, 20_000)
        section, changes = b.get("section"), b.get("changes")
        if section not in store.DEFAULTS or not isinstance(changes, dict):
            raise ApiError(400, "Unknown settings section.")
        if section == "site" and "description" in changes and len(str(changes["description"]).strip()) > 300:
            raise ApiError(400, "Keep the description under 300 characters (share previews cut longer ones).")
        if section == "site" and "base_url" in changes:
            url = str(changes["base_url"] or "").strip().rstrip("/")
            if url and not re.fullmatch(r"https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{1,5})?", url):
                raise ApiError(400, "The site address is just the start of your web address, like https://example.com (no path).")
            changes["base_url"] = url
        if section == "payments":
            check_payment_settings(changes)
        if section == "sms":
            check_sms_settings(changes)
        if section == "proleague":
            check_proleague_settings(changes)
        if section == "rankings" and "friendly_weight" in changes and changes["friendly_weight"] not in ("0", "0.5", "1"):
            raise ApiError(400, "Friendlies count fully, half or not at all.")
        if section == "rankings" and "min_matches" in changes and (not isinstance(changes["min_matches"], int) or not 1 <= changes["min_matches"] <= 100):
            raise ApiError(400, "The minimum number of matches must be from 1 to 100.")
        if section == "email":
            if changes.get("provider", "") not in ("", "smtp", "brevo", "resend", "console"):
                raise ApiError(400, "Choose an email provider.")
            if changes.get("from") and not EMAIL_RE.fullmatch(parseaddr(str(changes["from"]))[1] or ""):
                raise ApiError(400, "The sender needs an email address, e.g. League <you@gmail.com>.")
        try:
            old, new = store.update(section, changes)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        audit(user.username, f"changed {section} settings", ip, resource=f"settings:{section}", old=old, new=new)
    cfg = emailer.config()
    from payments import paynova
    pay = paynova.config()
    from sms import providers as smsp
    return {"site": store.masked("site"), "email": store.masked("email"), "payments": store.masked("payments"), "sms": store.masked("sms"),
            "rankings": store.masked("rankings"), "proleague": store.masked("proleague"), "siteLogo": store.site_logo_url(),
            "smsStatus": {"ready": smsp.ready(), "providers": smsp.PROVIDERS},
            "paymentStatus": {"ready": paynova.ready(pay), "mode": paynova.mode(pay["secret_key"]) or None, "source": pay["source"] or None},
            "emailStatus": {"ready": emailer.ready(cfg), "provider": cfg["provider"] or None, "source": cfg["source"]}}


@admin("POST", "DELETE")
def site_logo(request, user, ip):
    if request.method == "DELETE":
        store.set_site_logo()
    else:
        try:
            raw, ctype = engine.decode_logo(body(request, 400_000).get("image"))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        store.set_site_logo(raw, ctype)
    audit(user.username, "changed the site logo", ip, resource="settings:site")
    return {"ok": True, "logo": store.site_logo_url()}


def site_logo_file(request):
    import base64
    from django.http import HttpResponse
    v = store.site_logo()
    if not v:
        return HttpResponse(status=404)
    return HttpResponse(base64.b64decode(v["data"]), content_type=v["type"],
                        headers={"Cache-Control": "public, max-age=86400", "Content-Disposition": "inline",
                                 "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff"})


def check_proleague_settings(c):
    from decimal import Decimal, InvalidOperation
    from payments.api import CURRENCIES
    c.pop("org_id", None)                             # set by the system only
    if "name" in c and not 3 <= len(str(c["name"]).strip()) <= 80:
        raise ApiError(400, "Give the league a name (3–80 characters).")
    if "divisions" in c:
        names = [n.strip() for n in str(c["divisions"]).split(",") if n.strip()]
        if not 1 <= len(names) <= 26 or any(len(n) > 40 for n in names):
            raise ApiError(400, "List 1 to 26 division names separated by commas (each up to 40 characters).")
        c["divisions"] = ", ".join(names)
    if "size" in c and (not isinstance(c["size"], int) or not 4 <= c["size"] <= 6):
        raise ApiError(400, "Teams in a division must be from 4 to 6.")
    if "move" in c and (not isinstance(c["move"], int) or not 0 <= c["move"] <= 3):
        raise ApiError(400, "Teams going up and down must be from 0 to 3.")
    if "legs" in c and c["legs"] not in (1, 2):
        raise ApiError(400, "Teams meet once or twice.")
    if "walkover_hours" in c and (not isinstance(c["walkover_hours"], int) or not 1 <= c["walkover_hours"] <= 168):
        raise ApiError(400, "The deadline must be from 1 to 168 hours after kick-off.")
    if "walkover_score" in c and (not isinstance(c["walkover_score"], int) or not 1 <= c["walkover_score"] <= 10):
        raise ApiError(400, "The walkover score must be from 1 to 10.")
    if "currency" in c and c["currency"] not in CURRENCIES:
        raise ApiError(400, "Choose a supported currency.")
    if "whatsapp" in c:
        digits = re.sub(r"\D", "", str(c["whatsapp"]))
        if c["whatsapp"] and not 9 <= len(digits) <= 15:
            raise ApiError(400, "Enter the WhatsApp number with the country code, e.g. +233241234567.")
        c["whatsapp"] = ("+" + digits) if digits else ""
    if "fee" in c:
        try:
            v = Decimal(str(c["fee"]).strip() or "0")
        except InvalidOperation:
            raise ApiError(400, "The entry fee must be a number, e.g. 10.") from None
        if not Decimal("0") <= v <= Decimal("100000") or v.as_tuple().exponent < -2:
            raise ApiError(400, "The entry fee must be from 0 with up to 2 decimals.")
        c["fee"] = f"{v:.2f}"


def check_sms_settings(c):
    from decimal import Decimal, InvalidOperation
    from payments.api import CURRENCIES
    from sms.providers import PROVIDERS
    if "provider" in c and c["provider"] not in ("", *PROVIDERS):
        raise ApiError(400, "Choose an SMS provider.")
    if c.get("sender") and not re.fullmatch(r"[A-Za-z0-9 ]{2,11}", str(c["sender"]).strip()):
        raise ApiError(400, "The sender ID is 2–11 letters or numbers, approved by your SMS provider.")
    if "country_code" in c and not re.fullmatch(r"[1-9][0-9]{0,3}", str(c["country_code"]).strip()):
        raise ApiError(400, "The country code is digits only, e.g. 233 for Ghana.")
    if "currency" in c and c["currency"] not in CURRENCIES:
        raise ApiError(400, "Choose a supported currency.")
    if "min_credits" in c and (not isinstance(c["min_credits"], int) or not 1 <= c["min_credits"] <= 100000):
        raise ApiError(400, "The smallest purchase must be from 1 to 100,000 credits.")
    if "credit_price" in c:
        try:
            v = Decimal(str(c["credit_price"]).strip() or "0")
        except InvalidOperation:
            raise ApiError(400, "The price per credit must be a number, e.g. 0.05.") from None
        if not Decimal("0") <= v <= Decimal("100") or v.as_tuple().exponent < -4:
            raise ApiError(400, "The price per credit must be from 0 to 100 (up to 4 decimals).")
        c["credit_price"] = str(v.normalize()) if v else "0"


def check_payment_settings(c):
    from decimal import Decimal, InvalidOperation
    from payments.api import CURRENCIES
    key = c.get("secret_key")
    if key and not (isinstance(key, str) and re.fullmatch(r"sk_(test|live)_[A-Za-z0-9_-]{8,200}", key.strip())):
        raise ApiError(400, "The PayNova secret key starts with sk_test_ or sk_live_. (Never use the public pk_ key here.)")
    if "currency" in c and c["currency"] not in CURRENCIES:
        raise ApiError(400, "Choose a supported currency.")
    if "wallet_id" in c and c["wallet_id"] and not re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(c["wallet_id"]).strip()):
        raise ApiError(400, "The wallet ID looks like 3f2b8c1e-0000-0000-0000-000000000000.")
    for k, hi in (("fee_percent", Decimal("50")), ("fee_fixed", Decimal("10000"))):
        if k in c:
            try:
                v = Decimal(str(c[k]).strip() or "0")
            except InvalidOperation:
                raise ApiError(400, "Fees must be numbers, e.g. 5 or 2.50.") from None
            if not Decimal("0") <= v <= hi or v.as_tuple().exponent < -2:
                raise ApiError(400, f"The fee must be from 0 to {hi} (up to 2 decimals).")
            c[k] = f"{v:.2f}"


@admin("POST")
def test_email(request, user, ip):
    to = text(body(request), "to", 254) or user.email
    if not to or not EMAIL_RE.fullmatch(to):
        raise ApiError(400, "Enter the address to send the test to.")
    txt, html = emailer.body("Email works ✔", [f"Hi {user.username},", "Your platform email settings are working."],
                             site_url(request), "Open the platform", "Sent from the super admin settings.")
    try:
        emailer.send(to, f"{store.site()['name']}: test email", txt, html)
    except emailer.MailError as e:
        audit(user.username, "test email failed", ip, resource="settings:email", new=str(e), status="failed")
        raise ApiError(502, f"Sending failed: {e}") from None
    audit(user.username, "sent a test email", ip, resource="settings:email", new=to)
    return {"ok": True}


@admin("GET")
def system(request, user, ip):
    sess = 0
    try:
        from django.contrib.sessions.models import Session
        sess = Session.objects.filter(expire_date__gt=timezone.now()).count()
    except Exception:
        pass
    return {"health": health(), "matrix": matrix(),
            "superAdmins": [user_row(u) for u in User.objects.filter(is_superuser=True).order_by("username")],
            "config": {"Site address (used in links)": site_url(request), "APP_URL": settings.APP_URL or "(not set)", "TIME_ZONE": settings.TIME_ZONE, "HOME_PAGE": settings.HOME_PAGE,
                       "DEBUG": settings.DEBUG, "TRUST_PROXY": settings.TRUST_PROXY, "Secure cookies": settings.SECURE,
                       "Database": connection.vendor + (" (DATABASE_URL)" if __import__("os").environ.get("DATABASE_URL") else " (local file)"),
                       "Allowed hosts": ", ".join(settings.ALLOWED_HOSTS), "Python": py_platform.python_version(), "Django": django.get_version()},
            "security": {"activeSessions": sess, "suspendedUsers": User.objects.filter(is_active=False).count(),
                         "failedLogins24h": health()["failedLogins24h"], "lockouts": "5 wrong passwords lock an account for 15 minutes; 10 from one IP block it",
                         "passwordRules": "At least 10 characters, not common, not like the username (scrypt hashing)"},
            "integrations": {"email": emailer.config()["provider"] or "not set", "database": connection.vendor,
                             "publicApi": "Read-only public pages, sitemap.xml and robots.txt", "healthCheck": "/api/health"}}


@admin("POST")
def logout_everyone(request, user, ip):
    n = User.objects.exclude(id=user.id).update(session_epoch=F("session_epoch") + 1)
    audit(user.username, "logged out every user", ip, resource="security", new=n)
    return {"ok": True, "users": n}


# ---------- support ----------
def ticket_row(t):
    return {"id": t.id, "kind": t.kind, "kindLabel": dict(SupportTicket.KINDS)[t.kind], "subject": t.subject, "body": t.body,
            "status": t.status, "reply": t.reply, "user": who(t.user), "email": t.user.email if t.user else None,
            "created": ms(t.created), "updated": ms(t.updated)}


@admin("GET")
def tickets(request, user, ip):
    qs = SupportTicket.objects.select_related("user")
    if request.GET.get("status") in ("open", "in_progress", "closed"):
        qs = qs.filter(status=request.GET["status"])
    if request.GET.get("kind") in dict(SupportTicket.KINDS):
        qs = qs.filter(kind=request.GET["kind"])
    return paged(request, qs.order_by("status", "-id"), ticket_row)


@admin("PATCH")
def ticket_detail(request, user, ip, tid):
    t = SupportTicket.objects.select_related("user").filter(id=tid).first()
    if not t:
        raise ApiError(404, "Ticket not found.")
    b = body(request)
    old = {"status": t.status}
    if b.get("status") in ("open", "in_progress", "closed"):
        t.status = b["status"]
    if "reply" in b:
        t.reply = text(b, "reply", 5000)
    t.save()
    audit(user.username, "updated a support ticket", ip, resource=f"ticket:{t.id}", old=old, new={"status": t.status})
    if "reply" in b and t.reply and t.user and t.user.email and emailer.ready():
        txt, html = emailer.body(f"Re: {t.subject}", [f"Hi {t.user.username},", t.reply], site_url(request) + "/app/support",
                                 "View your requests", "Reply from the platform team.")
        try:
            emailer.send(t.user.email, f"Re: {t.subject}", txt, html)
        except emailer.MailError:
            pass
    return {"ok": True, "ticket": ticket_row(t)}


# ---------- for every user: contacting support ----------
@endpoint("GET", "POST", login_required=True)
def my_support(request, user, ip):
    if request.method == "POST":
        b = body(request)
        subject, msg = text(b, "subject", 140), text(b, "body", 5000)
        if len(subject) < 3 or len(msg) < 5:
            raise ApiError(400, "Add a subject and a message.")
        from league.logic import hit
        if hit(f"ticket:{user.id}", 10, 3600):
            raise ApiError(429, "Too many requests. Try again later.")
        t = SupportTicket.objects.create(user=user, kind=b.get("kind") if b.get("kind") in dict(SupportTicket.KINDS) else "support",
                                         subject=subject, body=msg)
        audit(user.username, "opened a support ticket", ip, resource=f"ticket:{t.id}", new=subject)
        return {"ok": True, "ticket": ticket_row(t)}
    return {"tickets": [ticket_row(t) for t in user.tickets.order_by("-id")[:50]], "supportEmail": store.site()["support_email"] or None}
