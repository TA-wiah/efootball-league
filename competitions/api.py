"""JSON API for competitions, teams, players, matches, results, standings and announcements.

Every endpoint starts with `access(user, org_slug, perm)` (membership + permission, checked on the server), and every
object is looked up *inside that organization*, so an ID from another organization is simply "not found".
"""
from datetime import date, datetime, timedelta, timezone as dt_tz

from django.db import transaction
from django.db.models import ProtectedError, Q, RestrictedError
from django.http import HttpResponse
from django.utils import timezone

from league.http import ApiError, body, endpoint, ms, session_user, text
from orgs.api import access, create_invitation, invitation_json, log, team_invite_roles
from orgs.models import Membership
from orgs.permissions import ROLE_INFO, STAFF_ROLES, can

from . import bracket, engine
from .models import (COMP_STATUS, EVENT_KINDS, FORMATS, KINDS, MATCH_STATUS, POSITIONS, VISIBILITY, Announcement, Competition,
                     Entry, Match, MatchEvent, Player, Team)

CHOICE = {"kind": dict(KINDS), "format": dict(FORMATS), "visibility": dict(VISIBILITY), "status": dict(COMP_STATUS)}


# ---------- small helpers ----------
def need(m, *perms):
    """At least one of `perms`, or 403."""
    if not any(can(m.role, p, m.org) for p in perms):
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) doesn't allow that.")


def team_access(user, slug, team_id):
    """Teams and their players: anyone with teams.manage, or a team manager/coach assigned to this team."""
    org, m = access(user, slug, "org.view")
    t = team_of(org, team_id)
    if not (can(m.role, "teams.manage", org) or (can(m.role, "teams.manage_assigned", org) and m.teams.filter(id=t.id).exists())):
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) can't change {t.name}.")
    return org, m, t


def num(b, key, lo, hi, allow_null=True):
    v = b.get(key)
    if v is None or v == "":
        if allow_null:
            return None
        raise ApiError(400, f"{key} is required.")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or not lo <= v <= hi:
        raise ApiError(400, f"{key} must be a whole number from {lo} to {hi}.")
    return int(v)


def day(b, key):
    v = b.get(key)
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ApiError(400, f"{key} must be a date like 2026-09-01.") from None


def moment(b, key):
    v = b.get(key)
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, f"{key} must be a date and time.") from None
    return d if d.tzinfo else d.replace(tzinfo=dt_tz.utc)


def iso(d):
    return d.astimezone(dt_tz.utc).isoformat().replace("+00:00", "Z") if d else None


def logo_url(kind, obj):
    return f"/media/{kind}/{obj.id}/logo?v={obj.version}" if obj.data else None


def team_brief(t):
    return {"id": t.id, "name": t.name, "shortName": t.short_name or None, "slug": t.slug, "logo": logo_url("team", t)}


def entry_brief(e):
    return {"entryId": e.id, **team_brief(e.team)} if e else None


def comp_json(c, extra=False):
    d = {"id": c.id, "name": c.name, "slug": c.slug, "description": c.description, "country": c.country, "region": c.region,
         "season": c.season, "kind": c.kind, "format": c.format, "visibility": c.visibility, "status": c.status,
         "startDate": c.start_date.isoformat() if c.start_date else None, "endDate": c.end_date.isoformat() if c.end_date else None,
         "logo": logo_url("competition", c)}
    if extra:
        d["schedule"] = {**engine.SCHEDULE_DEFAULT, **(c.schedule or {})}
        d.update({"rules": c.rules, "pointsWin": c.points_win, "pointsDraw": c.points_draw, "pointsLoss": c.points_loss,
                  "tiebreakers": engine.clean_tiebreakers(c.tiebreakers or engine.DEFAULT_TIEBREAKERS), "legs": c.legs,
                  "maxTeams": c.max_teams, "qualifiersPerGroup": c.qualifiers_per_group,
                  "teams": c.entries.count(), "matches": c.matches.count(),
                  "finished": c.matches.filter(status="finished").count()})
    return d


def team_json(t, players=False):
    d = {**team_brief(t), "city": t.city, "venue": t.venue, "founded": t.founded, "colors": t.colors, "description": t.description,
         "playerCount": t.players.count()}
    if players:
        d["players"] = [player_json(p) for p in t.players.order_by("number", "name")]
        d["competitions"] = [{"name": e.competition.name, "slug": e.competition.slug, "group": e.group, "visibility": e.competition.visibility}
                             for e in t.entries.select_related("competition")]
    return d


def player_json(p, team=False):
    d = {"id": p.id, "name": p.name, "number": p.number, "position": p.position, "active": p.active}
    if team:
        d["team"] = team_brief(p.team)
    return d


def event_json(e):
    return {"id": e.id, "minute": e.minute, "kind": e.kind, "side": e.side, "playerId": e.player_id,
            "playerName": e.player.name if e.player else e.player_name or None, "assistId": e.assist_id,
            "assistName": e.assist.name if e.assist else e.assist_name or None, "note": e.note or None}


def match_json(m, detail=False):
    d = {"id": m.id, "slug": m.slug, "competition": {"name": m.competition.name, "slug": m.competition.slug, "visibility": m.competition.visibility},
         "stage": m.stage, "group": m.group or None, "round": m.round, "roundName": m.round_name or None, "leg": m.leg,
         "home": entry_brief(m.home), "away": entry_brief(m.away), "slot": m.slot,
         "homeFrom": bracket.label(m.home_from) or None, "awayFrom": bracket.label(m.away_from) or None, "slot": m.slot,
         "homeFrom": bracket.label(m.home_from) or None, "awayFrom": bracket.label(m.away_from) or None, "kickoff": iso(m.kickoff), "venue": m.venue or None,
         "referee": m.referee or None, "status": m.status, "homeScore": m.home_score, "awayScore": m.away_score,
         "homePens": m.home_pens, "awayPens": m.away_pens}
    if detail:
        d["notes"] = m.notes
        d["events"] = [event_json(e) for e in m.events.select_related("player", "assist")]
        d["players"] = {side: [player_json(p) for p in (e.team.players.filter(active=True).order_by("number", "name") if e else [])]
                        for side, e in (("home", m.home), ("away", m.away))}
    return d


MATCHES = Match.objects.select_related("competition", "home__team", "away__team")


SUSPENDED = "This competition has been suspended by the platform, so it can't be changed. Contact support."


def competition_of(org, cslug, request=None):
    c = org.competitions.filter(slug=cslug).first()
    if not c:
        raise ApiError(404, "Competition not found.")
    if c.suspended and request is not None and request.method != "GET":
        raise ApiError(403, SUSPENDED)
    return c


def entry_of(c, entry_id, label="team"):
    if entry_id in (None, ""):
        return None
    e = c.entries.select_related("team").filter(id=entry_id).first() if isinstance(entry_id, int) else None
    if not e:
        raise ApiError(400, f"Choose a {label} that's entered in this competition.")
    return e


# ---------- organization summary (dashboard overview) ----------
@endpoint("GET", login_required=True)
def summary(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    now = timezone.now()
    matches = MATCHES.filter(competition__org=org)
    return {"competitions": org.competitions.count(), "activeCompetitions": org.competitions.filter(status="active").count(),
            "teams": org.teams.count(), "players": Player.objects.filter(team__org=org).count(),
            "upcoming": matches.filter(status="scheduled").filter(Q(kickoff__gte=now) | Q(kickoff__isnull=True)).count(),
            "completed": matches.filter(status="finished").count(),
            "awaitingResults": matches.filter(status__in=["scheduled", "live"], kickoff__lt=now - timezone.timedelta(hours=2)).count(),
            "nextMatches": [match_json(x) for x in matches.filter(status__in=["scheduled", "live"], kickoff__isnull=False, kickoff__gte=now - timezone.timedelta(hours=3)).order_by("kickoff")[:5]],
            "recentResults": [match_json(x) for x in matches.filter(status="finished").order_by("-kickoff", "-updated")[:5]],
            "members": org.memberships.count(), "staff": org.memberships.filter(role__in=STAFF_ROLES).count(),
            "pendingInvitations": org.invitations.filter(status="pending", expires__gt=now).count() if can(m.role, "members.invite", org) else None,
            "activity": [{"ts": ms(e.ts), "actor": e.actor, "action": e.action} for e in org.events.order_by("-id")[:8]]}


# ---------- competitions ----------
COMP_STRUCTURE = {"name", "country", "region", "season", "kind", "format", "visibility", "status", "startDate", "endDate", "pointsWin",
                  "pointsDraw", "pointsLoss", "tiebreakers", "legs", "maxTeams", "qualifiersPerGroup", "schedule"}
COMP_CONTENT = {"description", "rules"}


def apply_competition(c, b):
    if "name" in b:
        name = text(b, "name", 100)
        if len(name) < 2:
            raise ApiError(400, "Give the competition a name (at least 2 characters).")
        c.name = name
    for f, limit in (("description", 5000), ("country", 60), ("region", 60), ("season", 30), ("rules", 10000)):
        if f in b:
            setattr(c, f, text(b, f, limit))
    for f in ("kind", "format", "visibility", "status"):
        if f in b:
            if b[f] not in CHOICE[f]:
                raise ApiError(400, f"Unknown {f}.")
            setattr(c, f, b[f])
    if "startDate" in b:
        c.start_date = day(b, "startDate")
    if "endDate" in b:
        c.end_date = day(b, "endDate")
    if c.start_date and c.end_date and c.end_date < c.start_date:
        raise ApiError(400, "The end date is before the start date.")
    for f, attr in (("pointsWin", "points_win"), ("pointsDraw", "points_draw"), ("pointsLoss", "points_loss")):
        if f in b:
            setattr(c, attr, num(b, f, 0, 10, allow_null=False))
    if "tiebreakers" in b:
        c.tiebreakers = engine.clean_tiebreakers(b["tiebreakers"])
    if "legs" in b:
        c.legs = num(b, "legs", 1, 2, allow_null=False)
    if "maxTeams" in b:
        c.max_teams = num(b, "maxTeams", 2, 500)
    if "schedule" in b:
        try:
            c.schedule = {**(c.schedule or {}), **engine.clean_schedule(b["schedule"])}
        except ValueError as e:
            raise ApiError(400, str(e)) from None
    if "qualifiersPerGroup" in b:
        c.qualifiers_per_group = num(b, "qualifiersPerGroup", 0, 32, allow_null=False)


@endpoint("GET", "POST", login_required=True)
def competitions(request, user, ip, slug):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        return {"competitions": [comp_json(c, extra=True) for c in org.competitions.order_by("-created")]}
    org, m = access(user, slug, "competitions.manage")
    b = body(request, 30_000)
    c = Competition(org=org, created_by=user, tiebreakers=list(engine.DEFAULT_TIEBREAKERS))
    apply_competition(c, {"name": "", **b})
    if org.competitions.count() >= 200:
        raise ApiError(400, "An organization can have at most 200 competitions.")
    c.slug = engine.unique_slug(Competition, c.name, "competition")
    c.save()
    log(org, user, f"created the competition {c.name}")
    return {"ok": True, "competition": comp_json(c, extra=True)}


@endpoint("GET", "PATCH", "DELETE", login_required=True)
def competition_detail(request, user, ip, slug, cslug):
    org, m = access(user, slug, "org.view")
    c = competition_of(org, cslug, request)
    if request.method == "GET":
        return {"competition": comp_json(c, extra=True), "criteria": engine.CRITERIA,
                "entries": [{"id": e.id, "group": e.group, "pointsAdjustment": e.points_adjustment, "team": team_brief(e.team)}
                            for e in c.entries.select_related("team").order_by("group", "team__name")]}
    if request.method == "DELETE":
        need(m, "competitions.manage")
        if text(body(request), "confirm", 100) != c.name:
            raise ApiError(400, "Type the competition's exact name to confirm.")
        name = c.name
        c.delete()
        log(org, user, f"deleted the competition {name}")
        return {"ok": True}
    b = body(request, 30_000)
    if set(b) & COMP_STRUCTURE:
        need(m, "competitions.manage")
    elif set(b) & COMP_CONTENT:
        need(m, "competitions.manage", "content.edit")
    before = {"status": c.status, "visibility": c.visibility}
    apply_competition(c, b)
    c.save()
    after = {"status": c.status, "visibility": c.visibility}
    changed = before != after
    log(org, user, f"updated the competition {c.name}", old=before if changed else None, new=after if changed else None)
    return {"ok": True, "competition": comp_json(c, extra=True)}


def set_logo(request, obj, label):
    if request.method == "DELETE":
        obj.data, obj.content_type = None, ""
    else:
        try:
            obj.data, obj.content_type = engine.decode_logo(body(request, 400_000).get("image"))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
    obj.version += 1
    obj.save(update_fields=["data", "content_type", "version"])
    return {"ok": True, "logo": logo_url(label, obj)}


@endpoint("POST", "DELETE", login_required=True)
def competition_logo(request, user, ip, slug, cslug):
    org, m = access(user, slug, "org.view")
    need(m, "competitions.manage", "content.edit")
    return set_logo(request, competition_of(org, cslug, request), "competition")


# ---------- entries (teams in a competition) ----------
@endpoint("POST", login_required=True)
def entries(request, user, ip, slug, cslug):
    org, m = access(user, slug, "competitions.manage")
    c = competition_of(org, cslug, request)
    b = body(request)
    ids = b.get("teamIds") if isinstance(b.get("teamIds"), list) else [b.get("teamId")]
    teams = list(org.teams.filter(id__in=[i for i in ids if isinstance(i, int)]))
    if not teams or len(teams) != len(ids):
        raise ApiError(400, "Choose teams from this organization.")
    group = text(b, "group", 10)
    if c.max_teams and c.entries.count() + len(teams) > c.max_teams:
        raise ApiError(400, f"This competition is limited to {c.max_teams} teams.")
    added = 0
    for t in teams:
        _, made = Entry.objects.get_or_create(competition=c, team=t, defaults={"group": group})
        added += made
    log(org, user, f"added {added} team(s) to {c.name}")
    return {"ok": True, "added": added}


@endpoint("PATCH", "DELETE", login_required=True)
def entry_detail(request, user, ip, slug, cslug, entry_id):
    org, m = access(user, slug, "org.view")
    c = competition_of(org, cslug, request)
    e = c.entries.select_related("team").filter(id=entry_id).first()
    if not e:
        raise ApiError(404, "Team not found in this competition.")
    if request.method == "DELETE":
        need(m, "competitions.manage")
        if Match.objects.filter(Q(home=e) | Q(away=e)).exists():
            raise ApiError(409, f"{e.team.name} already has matches in this competition. Delete those matches first.")
        e.delete()
        log(org, user, f"removed {e.team.name} from {c.name}")
        return {"ok": True}
    b = body(request)
    if "group" in b:
        need(m, "competitions.manage")
        group = text(b, "group", 10)
        if group != e.group and Match.objects.filter(stage="league").filter(Q(home=e) | Q(away=e)).exists():
            raise ApiError(409, f"{e.team.name} already has group matches in Group {e.group or '–'}. Regenerate the fixtures after "
                                "changing groups, or delete its matches first.")
        e.group = group
    if "pointsAdjustment" in b:
        need(m, "standings.manage")
        e.points_adjustment = num(b, "pointsAdjustment", -99, 99, allow_null=False)
        log(org, user, f"set a points adjustment of {e.points_adjustment} for {e.team.name} in {c.name}")
    e.save()
    bracket.resolve(c)
    return {"ok": True}


# ---------- fixtures ----------
@endpoint("POST", login_required=True)
def generate(request, user, ip, slug, cslug):
    org, m = access(user, slug, "fixtures.manage")
    c = competition_of(org, cslug, request)
    if c.format == "knockout":
        raise ApiError(400, "Knockout competitions don't have a league stage. Use “Draw a knockout round” instead.")
    b = body(request)
    legs = num(b, "legs", 1, 2) or c.legs
    sched = use_schedule(c, b)
    existing = c.matches.filter(stage="league")
    if existing.filter(Q(status="finished") | Q(home_score__isnull=False)).exists():
        raise ApiError(409, "Results have already been entered for the league stage, so the fixtures can't be regenerated.")
    if existing.exists() and b.get("replace") is not True:
        raise ApiError(409, "This competition already has league fixtures. Confirm to replace them.", needsConfirm=True)
    groups = {}
    for e in c.entries.select_related("team"):
        groups.setdefault(e.group, []).append(e)
    if not any(len(g) >= 2 for g in groups.values()):
        raise ApiError(400, "Add at least two teams (in the same group) first.")
    pairs = []                                   # (matchday, group, order, home, away): every group's matchday 1 first, and so on
    for label, members in sorted(groups.items()):
        by_id = {e.id: e for e in members}
        for i, (rnd, h, a) in enumerate(engine.round_robin(by_id, legs)):
            pairs.append((rnd, label, i, by_id[h], by_id[a]))
    pairs.sort(key=lambda x: (x[0], x[1], x[2]))
    kickoffs = plan(c, org, len(pairs), first_free_day(c, org, sched, after_existing=False), sched, b)
    made = 0
    with transaction.atomic():
        existing.delete()
        for (rnd, label, _, home, away), ko in zip(pairs, kickoffs):
            Match.objects.create(competition=c, stage="league", group=label, round=rnd, round_name=f"Matchday {rnd}",
                                 home=home, away=away, kickoff=ko, venue=home.team.venue,
                                 slug=engine.match_slug(Match, home, away, ko, c.season))
            made += 1
        if c.status == "draft":
            c.status = "active"
            c.save(update_fields=["status"])
    log(org, user, f"generated {made} fixtures for {c.name}")
    ko = None
    if isinstance(b.get("knockout"), dict) and c.format == "groups_knockout":
        ko = create_plan(c, org, user, {**b["knockout"], "replace": True})
    return {"ok": True, "created": made, "knockout": ko, "first": kickoffs[0].isoformat() if kickoffs else None,
            "last": kickoffs[-1].isoformat() if kickoffs else None}


def org_tz(org):
    import zoneinfo
    from django.conf import settings
    for tz in (org.timezone, settings.TIME_ZONE, "UTC"):
        try:
            if tz:
                zoneinfo.ZoneInfo(tz)
                return tz
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


def use_schedule(c, b):
    """Save schedule changes sent with a generate/draw request, then return the competition's schedule."""
    if isinstance(b.get("schedule"), dict):
        try:
            c.schedule = {**(c.schedule or {}), **engine.clean_schedule(b["schedule"])}
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        c.save(update_fields=["schedule"])
    return {**engine.SCHEDULE_DEFAULT, **(c.schedule or {})}


def plan(c, org, blocks, first_day, sched, b):
    """Kick-offs for `count` matches. An explicit "start" (date and time) wins; otherwise the competition's schedule is used.
    Refuses plans that run past the competition's end date, saying what would fit."""
    tz = org_tz(org)
    start = moment(b, "start")
    if start:
        from django.utils import timezone as djtz
        local = djtz.localtime(start, __import__("zoneinfo").ZoneInfo(tz))
        sched = {**sched, "time": local.strftime("%H:%M")}
        first_day = local.date()
    count = blocks if isinstance(blocks, int) else len(blocks)
    days, span = engine.match_days_needed(blocks, sched)
    if c.end_date and count and first_day + timedelta(days=span) > c.end_date:
        room = (c.end_date - first_day).days // sched["everyDays"] + 1
        need = -(-count // room) if room > 0 else None
        hint = (f" Play {need} matches a day" + (" (the most is 20)" if need > 20 else "") + ", fewer days between match days, or move the end date."
                if need else " Move the end date or the start date.")
        raise ApiError(400, f"{count} matches at {sched['perDay']} a day, every {sched['everyDays']} day{'s' if sched['everyDays'] > 1 else ''}, "
                            f"would run until {(first_day + timedelta(days=span)):%d %b %Y}, after the end date ({c.end_date:%d %b %Y}).{hint}")
    return engine.plan_kickoffs(blocks, first_day, sched, tz)


def first_free_day(c, org, sched, after_existing):
    """The first day to schedule on: the start date (or today, if that has passed); for later rounds, the next match day after
    the last scheduled match."""
    import zoneinfo
    today = timezone.localtime(timezone.now(), zoneinfo.ZoneInfo(org_tz(org))).date()
    day = max(c.start_date or today, today)
    if after_existing:
        last = c.matches.exclude(kickoff=None).order_by("-kickoff").values_list("kickoff", flat=True).first()
        if last:
            day = max(day, timezone.localtime(last, zoneinfo.ZoneInfo(org_tz(org))).date() + timedelta(days=sched["everyDays"]))
    return day


def create_plan(c, org, user, b):
    """Lay out every knockout round now, with placeholders like "Group A winner" that fill in by themselves."""
    if c.format != "groups_knockout":
        raise ApiError(400, "A knockout plan is for competitions played as groups, then knockouts.")
    mode = b.get("mode") if b.get("mode") in bracket.MODES else "cross"
    legs = num(b, "legs", 1, 2) or 1
    ko = c.matches.filter(stage="knockout")
    if ko.filter(Q(status="finished") | Q(home_score__isnull=False)).exists():
        raise ApiError(409, "Knockout results have already been entered, so the knockout plan can't be changed.")
    if ko.exists() and b.get("replace") is not True:
        raise ApiError(409, "This competition already has knockout matches. Confirm to replace them.", needsConfirm=True)
    entries = list(c.entries.all())
    if any(not e.group for e in entries):
        raise ApiError(400, "Put every team in a group first (Teams tab).")
    groups = sorted({e.group for e in entries})
    q = c.qualifiers_per_group
    if q < 1:
        raise ApiError(400, "Set how many teams go through from each group (Settings).")
    small = [g for g in groups if sum(e.group == g for e in entries) < q]
    if small:
        raise ApiError(400, f"Group {small[0]} has fewer than {q} teams, but {q} go through from each group.")
    try:
        rounds = bracket.plan_rounds(bracket.first_round(groups, q, mode), legs)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    note = ""
    with transaction.atomic():
        ko.delete()
        created = []
        for r, name, ties, lg in rounds:
            for s, (hs, as_) in enumerate(ties, 1):
                for leg in range(1, lg + 1):
                    hf, af = (hs, as_) if leg == 1 else (as_, hs)
                    created.append(Match.objects.create(
                        competition=c, stage="knockout", round=r, round_name=name, leg=leg, slot=s, home_from=hf, away_from=af,
                        slug=engine.unique_slug(Match, f"{c.name} {name} {s}{' second leg' if leg == 2 else ''}", "match", limit=120)))
        bracket.resolve(c)
        if created and not c.matches.filter(stage="league", kickoff__isnull=True).exists():
            created.sort(key=lambda x: (x.round, x.leg, x.slot))
            sched = {**engine.SCHEDULE_DEFAULT, **(c.schedule or {})}
            try:
                kickoffs = plan(c, org, [(x.round, x.leg) for x in created], first_free_day(c, org, sched, after_existing=True), sched, {})
                for x, k in zip(created, kickoffs):
                    x.kickoff = k
                    x.save(update_fields=["kickoff"])
            except ApiError as e:
                note = f"The knockout matches don't have dates yet: {e.message}"
    log(org, user, f"set the knockout plan for {c.name} ({' → '.join(r[1] for r in rounds)})")
    return {"ok": True, "created": len(created), "rounds": [r[1] for r in rounds], "note": note}


@endpoint("POST", login_required=True)
def knockout_plan(request, user, ip, slug, cslug):
    org, m = access(user, slug, "fixtures.manage")
    c = competition_of(org, cslug, request)
    return create_plan(c, org, user, body(request))


@endpoint("POST", login_required=True)
def set_dates(request, user, ip, slug, cslug):
    """Give dates to the existing fixtures that haven't been played, keeping who plays whom (matchday by matchday)."""
    org, m = access(user, slug, "fixtures.manage")
    c = competition_of(org, cslug, request)
    b = body(request)
    sched = use_schedule(c, b)
    todo = c.matches.filter(status__in=["scheduled", "postponed"], home_score__isnull=True)
    if not b.get("all"):
        todo = todo.filter(kickoff__isnull=True)
    todo = sorted(todo, key=lambda x: (x.stage != "league", x.round, x.leg, x.slot, x.group, x.id))
    if not todo:
        raise ApiError(400, "Every fixture that hasn't been played already has a date.")
    after = c.matches.exclude(id__in=[x.id for x in todo]).exclude(kickoff=None).exists()
    kickoffs = plan(c, org, [None if x.stage == "league" else (x.round, x.leg) for x in todo],
                    first_free_day(c, org, sched, after_existing=after), sched, b)
    with transaction.atomic():
        for mt, ko in zip(todo, kickoffs):
            mt.kickoff = ko
            mt.save(update_fields=["kickoff"])
    log(org, user, f"set dates for {len(todo)} fixtures in {c.name}")
    return {"ok": True, "updated": len(todo), "first": kickoffs[0].isoformat(), "last": kickoffs[-1].isoformat()}


@endpoint("POST", login_required=True)
def draw(request, user, ip, slug, cslug):
    """A random knockout round between the chosen teams (shuffled on the server)."""
    org, m = access(user, slug, "fixtures.manage")
    c = competition_of(org, cslug, request)
    b = body(request)
    ids = b.get("entryIds") if isinstance(b.get("entryIds"), list) else []
    picked = [entry_of(c, i) for i in ids]
    if len(picked) < 2 or len(picked) % 2 or len({e.id for e in picked}) != len(picked):
        raise ApiError(400, "Pick an even number of different teams (at least 2).")
    name = text(b, "roundName", 40) or "Knockout round"
    legs = num(b, "legs", 1, 2) or 1
    sched = use_schedule(c, b)
    rnd = (c.matches.filter(stage="knockout").order_by("-round").values_list("round", flat=True).first() or 0) + 1
    by_id = {e.id: e for e in picked}
    ties = engine.draw_pairs(by_id)
    games = [(leg, h, a) for leg in range(1, legs + 1) for h, a in ties]        # all first legs, then all second legs
    kickoffs = plan(c, org, [leg for leg, _, _ in games], first_free_day(c, org, sched, after_existing=True), sched, b)
    created = []
    with transaction.atomic():
        for (leg, h, a), ko in zip(games, kickoffs):
            home, away = (by_id[h], by_id[a]) if leg == 1 else (by_id[a], by_id[h])   # second leg: home and away swap
            created.append(Match.objects.create(competition=c, stage="knockout", round=rnd, round_name=name, leg=leg,
                                                home=home, away=away, kickoff=ko, venue=home.team.venue,
                                                slug=engine.match_slug(Match, home, away, ko, c.season)))
    log(org, user, f"drew {name} for {c.name}")
    return {"ok": True, "matches": [match_json(x) for x in created]}


# ---------- matches ----------
MATCH_STRUCTURE = {"stage", "group", "round", "roundName", "leg", "homeId", "awayId"}
MATCH_RESULT = {"kickoff", "venue", "referee", "status", "homeScore", "awayScore", "homePens", "awayPens", "notes"}


def apply_match(mt, b):
    c = mt.competition
    if "stage" in b:
        if b["stage"] not in ("league", "knockout"):
            raise ApiError(400, "Unknown stage.")
        mt.stage = b["stage"]
    if "group" in b:
        mt.group = text(b, "group", 10)
    if "round" in b:
        mt.round = num(b, "round", 1, 999, allow_null=False)
    if "roundName" in b:
        mt.round_name = text(b, "roundName", 40)
    if "leg" in b:
        mt.leg = num(b, "leg", 1, 2, allow_null=False)
    if "homeId" in b:
        mt.home = entry_of(c, b["homeId"], "home team")
    if "awayId" in b:
        mt.away = entry_of(c, b["awayId"], "away team")
    if "homeId" in b:
        mt.home_from = ""                                # picked by hand: no longer follows the knockout plan
    if "awayId" in b:
        mt.away_from = ""
    if mt.home_id and mt.home_id == mt.away_id:
        raise ApiError(400, "A team can't play itself.")
    if mt.stage == "league" and mt.home_id and mt.away_id:
        if mt.home.group != mt.away.group:
            raise ApiError(400, f"In the group stage, teams only play teams from their own group: {mt.home.team.name} is in "
                                f"Group {mt.home.group or '–'} and {mt.away.team.name} is in Group {mt.away.group or '–'}.")
        mt.group = mt.home.group
    if "kickoff" in b:
        mt.kickoff = moment(b, "kickoff")
    for f, limit in (("venue", 100), ("referee", 100), ("notes", 2000)):
        if f in b:
            setattr(mt, f, text(b, f, limit))
    for f, attr in (("homeScore", "home_score"), ("awayScore", "away_score"), ("homePens", "home_pens"), ("awayPens", "away_pens")):
        if f in b:
            setattr(mt, attr, num(b, f, 0, 99))
    if "status" in b:
        if b["status"] not in dict(MATCH_STATUS):
            raise ApiError(400, "Unknown match status.")
        mt.status = b["status"]
    elif "homeScore" in b and mt.home_score is not None and mt.away_score is not None and mt.status == "scheduled":
        mt.status = "finished"                          # entering a full score finishes a scheduled match
    if mt.status in ("finished", "live") and (mt.home_score is None or mt.away_score is None):
        raise ApiError(400, "Enter both scores for a live or finished match.")
    if mt.status in ("finished", "live") and not (mt.home_id and mt.away_id):
        raise ApiError(400, "Both teams must be known before a score is entered.")


@endpoint("GET", "POST", login_required=True)
def comp_matches(request, user, ip, slug, cslug):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        c = competition_of(org, cslug, request)
        return {"matches": [match_json(x) for x in MATCHES.filter(competition=c).order_by("stage", "round", "slot", "leg", "kickoff", "id")]}
    org, m = access(user, slug, "fixtures.manage")
    c = competition_of(org, cslug, request)
    mt = Match(competition=c, stage="knockout" if c.format == "knockout" else "league")
    apply_match(mt, body(request))
    mt.slug = engine.match_slug(Match, mt.home, mt.away, mt.kickoff, c.season)
    if not mt.venue and mt.home:
        mt.venue = mt.home.team.venue
    mt.save()
    log(org, user, f"added a match to {c.name}")
    return {"ok": True, "match": match_json(mt)}


@endpoint("GET", login_required=True)
def org_matches(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    qs = MATCHES.filter(competition__org=org)
    if request.GET.get("competition"):
        qs = qs.filter(competition__slug=request.GET["competition"])
    view = request.GET.get("view", "all")
    if view == "upcoming":
        qs = qs.filter(status__in=["scheduled", "live", "postponed"]).order_by("kickoff", "round", "id")
    elif view == "results":
        qs = qs.filter(status="finished").order_by("-kickoff", "-round", "-id")
    else:
        qs = qs.order_by("competition__name", "stage", "round", "kickoff", "id")
    return {"matches": [match_json(x) for x in qs[:300]]}


def match_of(org, match_id, request=None):
    mt = MATCHES.filter(competition__org=org, id=match_id).first()
    if not mt:
        raise ApiError(404, "Match not found.")
    if mt.competition.suspended and request is not None and request.method != "GET":
        raise ApiError(403, SUSPENDED)
    return mt


@endpoint("GET", "PATCH", "DELETE", login_required=True)
def match_detail(request, user, ip, slug, match_id):
    org, m = access(user, slug, "org.view")
    mt = match_of(org, match_id, request)
    if request.method == "GET":
        return {"match": match_json(mt, detail=True)}
    if request.method == "DELETE":
        need(m, "fixtures.manage")
        mt.delete()
        bracket.resolve(mt.competition)
        log(org, user, f"deleted a match in {mt.competition.name}")
        return {"ok": True}
    b = body(request)
    if set(b) & MATCH_STRUCTURE:
        need(m, "fixtures.manage")
    if set(b) & MATCH_RESULT:
        need(m, "results.enter")
    before = {"score": [mt.home_score, mt.away_score], "status": mt.status}
    apply_match(mt, b)
    if mt.status == "finished" and before["status"] != "finished":
        mt.finished_at = timezone.now()
    mt.save()
    bracket.resolve(mt.competition)                       # a finished group or tie fills in the next knockout matches
    after = {"score": [mt.home_score, mt.away_score], "status": mt.status}
    if after != before and mt.home and mt.away:
        log(org, user, f"result {mt.home.team.name} vs {mt.away.team.name} in {mt.competition.name}", old=before, new=after)
    return {"ok": True, "match": match_json(mt, detail=True)}


@endpoint("POST", login_required=True)
def match_events(request, user, ip, slug, match_id):
    org, m = access(user, slug, "results.enter")
    mt = match_of(org, match_id, request)
    b = body(request)
    if b.get("kind") not in dict(EVENT_KINDS):
        raise ApiError(400, "Choose what happened (goal, card…).")
    side = b.get("side")
    entry = mt.home if side == "home" else mt.away if side == "away" else None
    if not entry:
        raise ApiError(400, "Choose the team.")
    if mt.events.count() >= 80:
        raise ApiError(400, "Too many events for one match.")

    def player(key):
        pid = b.get(key)
        if pid in (None, ""):
            return None
        p = entry.team.players.filter(id=pid).first() if isinstance(pid, int) else None
        if not p:
            raise ApiError(400, "That player isn't in this team's squad.")
        return p

    ev = MatchEvent(match=mt, kind=b["kind"], side=side, minute=num(b, "minute", 0, 150), player=player("playerId"),
                    player_name=text(b, "playerName", 80), assist=player("assistId"), assist_name=text(b, "assistName", 80),
                    note=text(b, "note", 120))
    ev.save()
    return {"ok": True, "event": event_json(ev)}


@endpoint("DELETE", login_required=True)
def match_event_detail(request, user, ip, slug, match_id, event_id):
    org, m = access(user, slug, "results.enter")
    mt = match_of(org, match_id, request)
    if not mt.events.filter(id=event_id).delete()[0]:
        raise ApiError(404, "Event not found.")
    return {"ok": True}


# ---------- standings & scorers ----------
@endpoint("GET", login_required=True)
def standings(request, user, ip, slug, cslug):
    org, m = access(user, slug, "org.view")
    c = competition_of(org, cslug, request)
    return table_payload(c)


def table_payload(c):
    entries = list(c.entries.select_related("team"))
    matches = list(c.matches.filter(stage="league", status="finished"))
    groups = sorted({e.group for e in entries})
    out = []
    for g in groups:
        rows = engine.standings(c, [e for e in entries if e.group == g], matches)   # only matches between these teams count
        out.append({"name": g or None, "rows": [{**r, "team": team_brief(r["team"])} for r in rows]})
    return {"competition": comp_json(c), "groups": out, "qualifiersPerGroup": c.qualifiers_per_group if c.format == "groups_knockout" else 0,
            "pointsSystem": {"win": c.points_win, "draw": c.points_draw, "loss": c.points_loss},
            "tiebreakers": [engine.CRITERIA[k] for k in engine.clean_tiebreakers(c.tiebreakers or engine.DEFAULT_TIEBREAKERS)]}


@endpoint("GET", login_required=True)
def comp_scorers(request, user, ip, slug, cslug):
    org, m = access(user, slug, "org.view")
    c = competition_of(org, cslug, request)
    events = MatchEvent.objects.select_related("player", "assist", "match__home__team", "match__away__team").filter(match__competition=c)
    return {"scorers": [{**r, "team": team_brief(r["team"]) if r["team"] else None} for r in engine.scorers(events)[:50]]}


# ---------- teams & players ----------
def apply_team(t, b):
    if "name" in b:
        name = text(b, "name", 80)
        if len(name) < 2:
            raise ApiError(400, "Give the team a name (at least 2 characters).")
        t.name = name
    for f, attr, limit in (("shortName", "short_name", 12), ("city", "city", 60), ("venue", "venue", 100), ("colors", "colors", 40),
                           ("description", "description", 2000)):
        if f in b:
            setattr(t, attr, text(b, f, limit))
    if "founded" in b:
        t.founded = num(b, "founded", 1800, 2100)


@endpoint("GET", "POST", login_required=True)
def teams(request, user, ip, slug):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        return {"teams": [team_json(t) for t in org.teams.order_by("name")]}
    org, m = access(user, slug, "teams.manage")
    if org.teams.count() >= 1000:
        raise ApiError(400, "An organization can have at most 1000 teams.")
    t = Team(org=org)
    apply_team(t, {"name": "", **body(request)})
    t.slug = engine.unique_slug(Team, t.name, "team")
    t.save()
    log(org, user, f"added the team {t.name}")
    return {"ok": True, "team": team_json(t)}


def team_of(org, team_id):
    t = org.teams.filter(id=team_id).first()
    if not t:
        raise ApiError(404, "Team not found.")
    return t


@endpoint("GET", "PATCH", "DELETE", login_required=True)
def team_detail(request, user, ip, slug, team_id):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        t = team_of(org, team_id)
        mine = m.teams.filter(id=t.id).exists()
        people = [{"id": x.id, "username": x.user.username, "role": x.role, "roleLabel": ROLE_INFO[x.role][0]}
                  for x in t.assigned_staff.select_related("user").order_by("role", "user__username")]
        return {"team": {**team_json(t, players=True), "members": people, "mine": mine,
                         "inviteRoles": team_invite_roles(m) if (mine or can(m.role, "members.invite", org)) else []}}
    if request.method == "PATCH":
        org, m, t = team_access(user, slug, team_id)
        apply_team(t, body(request))
        t.save()
        return {"ok": True, "team": team_json(t, players=True)}
    org, m = access(user, slug, "teams.manage")
    t = team_of(org, team_id)
    if request.method == "DELETE":
        if Match.objects.filter(Q(home__team=t) | Q(away__team=t)).exists():
            raise ApiError(409, f"{t.name} has played or scheduled matches. Delete those matches first.")
        name = t.name
        try:
            with transaction.atomic():
                t.entries.all().delete()          # no matches, so leaving its competitions loses nothing
                t.delete()
        except (ProtectedError, RestrictedError):
            raise ApiError(409, f"{name} is still used by matches.") from None
        log(org, user, f"deleted the team {name}")
        return {"ok": True}


@endpoint("GET", "POST", login_required=True)
def team_invitations(request, user, ip, slug, team_id):
    """Invite players and coaches straight into a team (team managers for their own teams; staff for any team)."""
    org, m = access(user, slug, "org.view")
    t = team_of(org, team_id)
    roles = team_invite_roles(m)
    if not roles or not (can(m.role, "members.invite", org) or m.teams.filter(id=t.id).exists()):
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) can't invite people to {t.name}.")
    if request.method == "GET":
        rows = t.invitations.select_related("invited_by", "accepted_by").prefetch_related("teams").order_by("-id")[:100]
        return {"invitations": [invitation_json(i) for i in rows], "roles": roles}
    b = body(request)
    role = text(b, "role", 20)
    if role not in roles:
        raise ApiError(403, f"Your role ({ROLE_INFO[m.role][0]}) can't invite people as {ROLE_INFO.get(role, ('that role',))[0]}.")
    return create_invitation(request, org, user, role, text(b, "email", 254).lower(), teams=[t])


@endpoint("POST", "DELETE", login_required=True)
def team_logo(request, user, ip, slug, team_id):
    org, m, t = team_access(user, slug, team_id)
    return set_logo(request, t, "team")


def apply_player(p, b):
    if "name" in b:
        name = text(b, "name", 80)
        if not name:
            raise ApiError(400, "Enter the player's name.")
        p.name = name
    if "number" in b:
        p.number = num(b, "number", 0, 999)
    if "position" in b:
        if b["position"] not in dict(POSITIONS):
            raise ApiError(400, "Unknown position.")
        p.position = b["position"]
    if "active" in b:
        p.active = bool(b["active"])


@endpoint("POST", login_required=True)
def team_players(request, user, ip, slug, team_id):
    org, m, t = team_access(user, slug, team_id)
    if t.players.count() >= 100:
        raise ApiError(400, "A team can have at most 100 players.")
    p = Player(team=t)
    apply_player(p, {"name": "", **body(request)})
    p.save()
    return {"ok": True, "player": player_json(p)}


@endpoint("GET", login_required=True)
def players(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    return {"players": [player_json(p, team=True) for p in Player.objects.select_related("team").filter(team__org=org).order_by("team__name", "number", "name")[:3000]]}


@endpoint("PATCH", "DELETE", login_required=True)
def player_detail(request, user, ip, slug, player_id):
    org, m = access(user, slug, "org.view")
    p = Player.objects.select_related("team").filter(team__org=org, id=player_id).first()
    if not p:
        raise ApiError(404, "Player not found.")
    team_access(user, slug, p.team_id)
    if request.method == "DELETE":
        p.delete()
        return {"ok": True}
    apply_player(p, body(request))
    p.save()
    return {"ok": True, "player": player_json(p)}


# ---------- announcements ----------
def ann_json(a):
    return {"id": a.id, "title": a.title, "body": a.body, "published": a.published, "pinned": a.pinned,
            "competition": {"name": a.competition.name, "slug": a.competition.slug} if a.competition else None,
            "author": getattr(a.author, "username", None), "authorId": a.author_id, "created": ms(a.created), "updated": ms(a.updated)}


@endpoint("GET", "POST", login_required=True)
def announcements(request, user, ip, slug):
    if request.method == "GET":
        org, m = access(user, slug, "org.view")
        return {"announcements": [ann_json(a) for a in org.announcements.select_related("competition", "author").order_by("-pinned", "-created")[:200]]}
    org, m = access(user, slug, "content.edit")
    b = body(request, 20_000)
    title = text(b, "title", 140)
    if len(title) < 2:
        raise ApiError(400, "Give the announcement a title.")
    comp = competition_of(org, b["competition"]) if b.get("competition") else None
    a = Announcement.objects.create(org=org, competition=comp, title=title, body=text(b, "body", 5000), author=user,
                                    published=b.get("published", True) is not False, pinned=bool(b.get("pinned")))
    log(org, user, f"posted “{title}”")
    return {"ok": True, "announcement": ann_json(a)}


@endpoint("PATCH", "DELETE", login_required=True)
def announcement_detail(request, user, ip, slug, ann_id):
    org, m = access(user, slug, "org.view")
    a = org.announcements.select_related("competition", "author").filter(id=ann_id).first()
    if not a:
        raise ApiError(404, "Announcement not found.")
    own = a.author_id == user.id and can(m.role, "content.edit", org)
    if request.method == "DELETE":
        if not (own or can(m.role, "content.moderate", org)):
            raise ApiError(403, "Only its author or a moderator can delete this announcement.")
        a.delete()
        log(org, user, f"deleted the announcement “{a.title}”")
        return {"ok": True}
    b = body(request, 20_000)
    if {"title", "body"} & set(b):
        if not (own or can(m.role, "competitions.manage", org)):    # staff can edit any; editors their own
            raise ApiError(403, "You can only edit your own announcements.")
        if "title" in b:
            title = text(b, "title", 140)
            if len(title) < 2:
                raise ApiError(400, "Give the announcement a title.")
            a.title = title
        if "body" in b:
            a.body = text(b, "body", 5000)
    if {"published", "pinned"} & set(b):
        need(m, "content.moderate", "content.edit")
        if "published" in b:
            a.published = bool(b["published"])
        if "pinned" in b:
            a.pinned = bool(b["pinned"])
    a.save()
    return {"ok": True, "announcement": ann_json(a)}


# ---------- logos (images) ----------
def logo_file(request, kind, obj_id):
    if kind == "org":
        from orgs.models import Organization
        o = Organization.objects.filter(id=obj_id).only("id", "logo_data", "logo_type").first()
        if not o or not o.logo_data:
            return HttpResponse(status=404)
        return HttpResponse(bytes(o.logo_data), content_type=o.logo_type,
                            headers={"Cache-Control": "public, max-age=86400", "Content-Disposition": "inline",
                                     "Content-Security-Policy": "default-src 'none'; sandbox"})
    model = Competition if kind == "competition" else Team
    obj = model.objects.filter(id=obj_id).only("id", "data", "content_type", "version", "org_id",
                                                *(["visibility"] if kind == "competition" else [])).first()
    if not obj or not obj.data:
        return HttpResponse(status=404)
    private = kind == "competition" and obj.visibility == "private"
    if private:
        user = session_user(request)
        if not user or not Membership.objects.filter(org_id=obj.org_id, user=user).exists():
            return HttpResponse(status=404)
    return HttpResponse(bytes(obj.data), content_type=obj.content_type,
                        headers={"Cache-Control": ("private" if private else "public") + ", max-age=86400",
                                 "Content-Disposition": "inline", "Content-Security-Policy": "default-src 'none'; sandbox"})
