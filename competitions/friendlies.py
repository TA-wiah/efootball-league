"""Friendlies: "Challenge a team", inside your organization or against another one.

Inside the organization: whoever manages both teams sets the match straight away; a team manager challenging another of
the organization's teams waits for that team's managers to accept.

A team's managers pick any team with a public page (or of an organization open to friendlies), propose a kick-off and send a challenge. The other team's managers
accept or decline. On acceptance the match is created in the challenger organization's "Friendlies" series (a friendly
series: no league table, results and head-to-head only), with both teams entered, so both sides can check in, send
screenshots and follow it like any other match.
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from league import emailer
from league.http import ApiError, body, endpoint, ms, site_url, text
from league.logic import hit
from orgs.api import access, log
from orgs.models import Membership
from orgs.permissions import PLAYER, can

from . import engine
from .api import moment, team_brief
from .models import Competition, Entry, FriendlyChallenge, Match, Team
from .public import listed

MAX_DAYS = 730            # how far ahead a friendly can be planned (two years: in practice, any time)
OPEN_HOURS = 24          # a challenge stays open this long after its proposed kick-off ("play now" included)
PAST_DAYS = 365          # whoever manages both teams can also record a friendly already played


def manageable(m):
    """The teams this member may arrange friendlies for."""
    if can(m.role, "teams.manage", m.org):
        return list(m.org.teams.filter(suspended=False).order_by("name"))
    if can(m.role, "teams.manage_assigned", m.org):
        return list(m.teams.filter(suspended=False).order_by("name"))
    return []


def challengeable():
    """Teams that can be found and challenged: those with a public page, and every team of an organization that chose
    "Let other organizations challenge all our teams" (private competitions included; only the team is shown)."""
    public = Entry.objects.filter(listed("competition__")).values("team_id")
    return (Team.objects.select_related("org").filter(suspended=False, org__status="active")
            .filter(Q(id__in=public) | Q(org__open_to_friendlies=True)))


def is_open(ch):
    """An open request: "looking for a friendly" posted to the team's own organization; any of its teams can take it."""
    return not ch.to_team_id and ch.to_org_id == ch.from_team.org_id


def to_name(ch):
    return ch.to_team.name if ch.to_team_id else ("any team (open request)" if is_open(ch) else f"{ch.to_org.name} (their choice of team)")


def my_teams(m):
    """Teams this member can post or take a friendly for: those they manage, and those they play or coach for."""
    teams = {t.id: t for t in manageable(m)}
    for t in m.teams.filter(suspended=False):
        teams.setdefault(t.id, t)
    return sorted(teams.values(), key=lambda t: t.name.lower())


def state(ch):
    late = ch.kickoff + timezone.timedelta(hours=OPEN_HOURS) < timezone.now()
    return "expired" if ch.status == "pending" and late else ch.status


def side_json(t):
    return {**team_brief(t), "org": {"name": t.org.name, "slug": t.org.slug}}


def challenge_json(ch, org, mine_ids, staff=False):
    """`staff`: the viewer manages fixtures in `org` (may delete a played friendly hosted there)."""
    st = state(ch)
    played = bool(ch.match_id and (ch.match.home_score is not None or ch.match.status == "finished"))
    their_side = ch.to_org_id == org.id and (ch.to_team_id in mine_ids if ch.to_team_id else bool(mine_ids))
    my_side = (ch.from_team.org_id == org.id and ch.from_team_id in mine_ids) or their_side
    internal = bool(ch.to_team_id) and ch.from_team.org_id == ch.to_org_id
    if internal:                                          # inside one organization: "received" by the other team's managers
        sent = not (ch.to_team_id in mine_ids and ch.from_team_id not in mine_ids)
    else:
        sent = ch.from_team.org_id == org.id
    to = side_json(ch.to_team) if ch.to_team_id else {"name": "Any team (open request)" if is_open(ch) else "Their choice of team", "choice": True,
                                                       "open": is_open(ch), "logo": None, "org": {"name": ch.to_org.name, "slug": ch.to_org.slug}}
    return {"id": ch.id, "direction": "sent" if sent else "received", "from": side_json(ch.from_team), "to": to,
            "kickoff": ch.kickoff.isoformat(), "message": ch.message or None, "status": st, "created": ms(ch.created),
            "by": getattr(ch.created_by, "username", None), "answeredBy": getattr(ch.answered_by, "username", None),
            "match": {"id": ch.match_id, "slug": ch.match.slug} if ch.match_id else None,
            "internal": internal, "host": ch.from_team.org.slug,
            "canAnswer": st == "pending" and their_side,
            "canCancel": st == "pending" and ch.from_team.org_id == org.id and ch.from_team_id in mine_ids,
            "played": played,
            # either team's managers, until it's played; after that only the hosting organization's fixtures staff
            # once accepted, a friendly is the hosting organization's to change or remove (teams send results instead)
            "canDelete": (staff and ch.from_team.org_id == org.id) or (my_side and st != "accepted"),
            "canEdit": (st == "pending" and ch.from_team.org_id == org.id and ch.from_team_id in mine_ids)
                       or (st == "accepted" and not played and staff and ch.from_team.org_id == org.id)}


def audience(team=None, org=None, skip=None):
    """Who hears about a friendly:
    - about a particular team: only the people on that team (players, coaches, team managers); if nobody is on it
      yet, the organization's managers instead, so it isn't lost;
    - for everyone (an open request, or a challenge to the whole organization): everyone on any of the
      organization's teams, and its managers, except the team that posted it (`skip`)."""
    qs = Membership.objects.select_related("user", "org").filter(org_id=team.org_id if team else org.id)
    if team:
        on_team = list(qs.filter(teams=team).distinct())
        if on_team:
            return on_team
    everyone = [mb for mb in qs.distinct() if can(mb.role, "teams.manage", mb.org) or (not team and mb.teams.exists())]
    return [mb for mb in everyone if not (skip and mb.teams.filter(id=skip.id).exists())]


def managers_with_email(team, org=None, skip=None):
    return list(dict.fromkeys(mb.user.email for mb in audience(team, org, skip) if mb.user.email))[:200]


def text_people(members, org, message):
    """An SMS to these members when their organization turned automatic texts on (its credits)."""
    numbers = list(dict.fromkeys(mb.user.phone for mb in members if mb.user.phone))
    if not numbers or not org.auto_sms_reminders:
        return
    try:
        from sms.api import send_text
        send_text(org, None, numbers, message[:450], kind="notice")
    except ApiError:
        pass                                          # no credits, SMS off…: the emails still go


def notify(team, subject, lines, request, org_slug, org=None, page="friendlies", skip=None):
    """Email (and text, when turned on) the team concerned, or everyone for an open request / whole-organization challenge."""
    link = f"{site_url(request)}/app/org/{org_slug}/{page}"
    text_people(audience(team, org, skip), team.org if team else org, f"{subject}. {lines[1] if len(lines) > 1 else ''} Open: {link}".replace("  ", " "))
    if not emailer.ready():
        return
    txt, html = emailer.body(subject, lines, link, "Open friendlies", "You get this because you're on this team or manage teams there.")
    for to in managers_with_email(team, org, skip):
        try:
            emailer.send(to, subject, txt, html)
        except emailer.MailError:
            pass


def host_series(org):
    """The challenger organization's "Friendlies" series (made the first time it's needed)."""
    c = Competition.objects.filter(org=org, kind="friendly", name="Friendlies").first()
    if c:
        return c
    return Competition.objects.create(org=org, name="Friendlies", kind="friendly", format="league", visibility="public", status="active",
                                      slug=engine.unique_slug(Competition, f"{org.slug} friendlies", "friendlies"),
                                      description=f"Friendly matches arranged by {org.name}.")


def delete_friendly(ch):
    """Remove a challenge and, if it was accepted, its match (screenshots and goals go with it). Teams left with no
    match in the Friendlies series are taken out of it too."""
    match = ch.match
    ch.delete()
    if not match:
        return
    c, entries = match.competition, [e for e in (match.home, match.away) if e]
    match.delete()
    for e in entries:
        if not c.matches.filter(Q(home=e) | Q(away=e)).exists():
            e.delete()


def make_match(ch):
    """The friendly itself, in the challenger organization's "Friendlies" series with both teams entered."""
    c = host_series(ch.from_team.org)
    home, _ = Entry.objects.get_or_create(competition=c, team=ch.from_team)
    away, _ = Entry.objects.get_or_create(competition=c, team=ch.to_team)
    return Match.objects.create(competition=c, stage="league", round=1, round_name="Friendly", home=home, away=away, kickoff=ch.kickoff,
                                slug=engine.unique_slug(Match, f"{ch.from_team.name} v {ch.to_team.name} friendly", "match", limit=120))


@endpoint("GET", login_required=True)
def team_search(request, user, ip):
    """Teams that can be challenged (see challengeable), found by team or organization name, and the organizations
    themselves (challenge them and they choose which team plays)."""
    q = (request.GET.get("q") or "").strip()[:60]
    if len(q) < 2:
        return {"teams": [], "orgs": []}
    teams = challengeable().filter(Q(name__icontains=q) | Q(org__name__icontains=q)).order_by("name")[:20]
    orgs = {}
    for t in challengeable().filter(org__name__icontains=q).order_by("org__name")[:200]:
        o = orgs.setdefault(t.org_id, {"name": t.org.name, "slug": t.org.slug, "teams": 0})
        o["teams"] += 1
    return {"teams": [side_json(t) for t in teams], "orgs": list(orgs.values())[:8]}


@endpoint("GET", "POST", login_required=True)
def org_friendlies(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    mine = manageable(m)
    mine_ids = {t.id for t in mine}
    if request.method == "GET":
        rows = (FriendlyChallenge.objects.select_related("from_team__org", "to_team__org", "to_org", "created_by", "answered_by", "match")
                .filter(Q(from_team__org=org) | Q(to_org=org)).order_by("-created")
                .exclude(to_team__isnull=True, to_org=org, from_team__org=org))   # open requests live on "Find a friendly"
        if m.role == PLAYER:                             # a player follows their own teams' friendlies only
            ids = list(m.teams.values_list("id", flat=True))
            rows = rows.filter(Q(from_team_id__in=ids) | Q(to_team_id__in=ids))
        rows = rows[:200]
        staff = can(m.role, "fixtures.manage", org)
        return {"challenges": [challenge_json(ch, org, mine_ids, staff) for ch in rows], "teams": [team_brief(t) for t in mine]}
    b = body(request)
    mine_t = next((t for t in mine if t.id == b.get("fromTeamId")), None)
    if not mine_t:
        raise ApiError(403, "Choose one of the teams you manage.")
    if b.get("toOrg"):                               # a whole organization: they choose which team plays
        return challenge_org(request, user, org, mine_t, mine_ids, b)
    tid = b.get("toTeamId") if isinstance(b.get("toTeamId"), int) else -1
    other = (org.teams.select_related("org").filter(id=tid, suspended=False).first()      # one of our own teams
             or challengeable().exclude(org=org).filter(id=tid).first())
    if not other:
        raise ApiError(404, "Team not found. Search for the team by name.")
    if other.id == mine_t.id:
        raise ApiError(400, "A team can't play itself. Choose another opponent.")
    now = timezone.now()
    kickoff = now if b.get("kickoff") == "now" else moment(b, "kickoff")
    both = other.id in mine_ids
    earliest = now - (timezone.timedelta(days=PAST_DAYS) if both else timezone.timedelta(minutes=30))
    if not kickoff or kickoff > now + timezone.timedelta(days=MAX_DAYS):
        raise ApiError(400, "Choose when the friendly is played.")
    if kickoff < earliest:
        raise ApiError(400, "That time has already passed. Pick now or a later time." if not both
                       else f"A friendly already played can be recorded up to {PAST_DAYS} days back.")
    if FriendlyChallenge.objects.filter(status="pending", kickoff__gt=now - timezone.timedelta(hours=OPEN_HOURS)).filter(
            Q(from_team=mine_t, to_team=other) | Q(from_team=other, to_team=mine_t)).exists():
        raise ApiError(409, "There's already a challenge waiting between these two teams.")
    if hit(f"friendly:{org.id}", 30, 3600):
        raise ApiError(429, "Too many challenges. Try again later.")
    if both:                                         # you manage both teams: no need to ask, the match is set straight away
        with transaction.atomic():
            ch = FriendlyChallenge.objects.create(from_team=mine_t, to_team=other, to_org=other.org, kickoff=kickoff, message=text(b, "message", 300),
                                                  created_by=user, status="accepted", answered_by=user, answered=now)
            ch.match = make_match(ch)
            ch.save(update_fields=["match"])
        log(org, user, f"arranged a friendly: {mine_t.name} v {other.name}")
        return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}
    ch = FriendlyChallenge.objects.create(from_team=mine_t, to_team=other, to_org=other.org, kickoff=kickoff, message=text(b, "message", 300), created_by=user)
    if other.org_id == org.id:
        log(org, user, f"challenged {other.name} to a friendly against {mine_t.name}")
        notify(other, f"{mine_t.name} challenged {other.name} to a friendly",
               [f"{mine_t.name} wants to play {other.name}.", f"Proposed kick-off: {kickoff:%a %d %b %Y, %H:%M} UTC."]
               + ([f"Message: {ch.message}"] if ch.message else []) + ["Accept or decline it in the Friendlies page."], request, org.slug)
        return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}
    log(org, user, f"challenged {other.name} ({other.org.name}) to a friendly against {mine_t.name}")
    log(other.org, None, f"{mine_t.name} ({org.name}) challenged {other.name} to a friendly")
    notify(other, f"{mine_t.name} challenged {other.name} to a friendly",
           [f"{mine_t.name} from {org.name} wants to play {other.name}.", f"Proposed kick-off: {kickoff:%a %d %b %Y, %H:%M} UTC."]
           + ([f"Message: {ch.message}"] if ch.message else []) + ["Accept or decline it in your organization's Friendlies page."],
           request, other.org.slug)
    return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}


def kickoff_of(b, now):
    kickoff = now if b.get("kickoff") == "now" else moment(b, "kickoff")
    if not kickoff or kickoff > now + timezone.timedelta(days=MAX_DAYS):
        raise ApiError(400, "Choose when the friendly is played.")
    if kickoff < now - timezone.timedelta(minutes=30):
        raise ApiError(400, "That time has already passed. Pick now or a later time.")
    return kickoff


def challenge_org(request, user, org, mine_t, mine_ids, b):
    from orgs.models import Organization
    other = Organization.objects.filter(slug=str(b["toOrg"])[:80], status="active").exclude(id=org.id).first()
    if not other or not challengeable().filter(org=other).exists():
        raise ApiError(404, "Organization not found. Search for it by name.")
    now = timezone.now()
    kickoff = kickoff_of(b, now)
    if FriendlyChallenge.objects.filter(status="pending", from_team=mine_t, to_org=other, to_team__isnull=True,
                                        kickoff__gt=now - timezone.timedelta(hours=OPEN_HOURS)).exists():
        raise ApiError(409, f"{mine_t.name} already has a challenge waiting for {other.name}.")
    if hit(f"friendly:{org.id}", 30, 3600):
        raise ApiError(429, "Too many challenges. Try again later.")
    ch = FriendlyChallenge.objects.create(from_team=mine_t, to_org=other, kickoff=kickoff, message=text(b, "message", 300), created_by=user)
    log(org, user, f"challenged {other.name} to a friendly against {mine_t.name} (they choose their team)")
    log(other, None, f"{mine_t.name} ({org.name}) challenged {other.name} to a friendly: choose a team to play")
    notify(None, f"{mine_t.name} challenged {other.name} to a friendly",
           [f"{mine_t.name} from {org.name} wants to play one of your teams: you choose which.", f"Proposed kick-off: {kickoff:%a %d %b %Y, %H:%M} UTC."]
           + ([f"Message: {ch.message}"] if ch.message else []) + ["Pick your team and accept, or decline, in your organization's Friendlies page."],
           request, other.slug, org=other)
    return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}


# ---------- find a friendly (players, coaches, team managers) ----------
def board_json(ch, mine_ids, user=None, assigned=()):
    """`mine`: you posted it, or it's for a team you're on (staff who manage every team can still take others' requests)."""
    return {"id": ch.id, "team": side_json(ch.from_team), "kickoff": ch.kickoff.isoformat(), "message": ch.message or None,
            "by": getattr(ch.created_by, "username", None), "status": state(ch),
            "mine": bool(user and ch.created_by_id == user.id) or ch.from_team_id in assigned, "canCancel": ch.from_team_id in mine_ids}


@endpoint("GET", "POST", login_required=True)
def friendly_board(request, user, ip, slug):
    """Teams of this organization looking for a friendly. Post "we're looking", or take someone's request: the match is
    set straight away in the organization's Friendlies series (results are sent with screenshots and confirmed)."""
    org, m = access(user, slug, "org.view")
    teams = my_teams(m)
    ids = {t.id for t in teams}
    now = timezone.now()
    if request.method == "GET":
        live = (FriendlyChallenge.objects.select_related("from_team__org", "created_by")
                .filter(from_team__org=org, to_org=org, to_team__isnull=True, status="pending", kickoff__gt=now - timezone.timedelta(hours=OPEN_HOURS))
                .order_by("kickoff"))
        direct = (FriendlyChallenge.objects.select_related("from_team__org", "to_team__org", "to_org", "match", "created_by")
                  .filter(from_team__org=org, to_team__org=org, status="pending", kickoff__gt=now - timezone.timedelta(hours=OPEN_HOURS))
                  .filter(Q(from_team_id__in=ids) | Q(to_team_id__in=ids)).order_by("kickoff"))
        agreed = (FriendlyChallenge.objects.select_related("from_team__org", "to_team__org", "to_org", "match")
                  .filter(status="accepted", match__isnull=False).filter(Q(from_team_id__in=ids) | Q(to_team_id__in=ids))
                  .exclude(match__status__in=["finished", "cancelled"]).order_by("kickoff")[:30])
        assigned = set(m.teams.values_list("id", flat=True))
        org_teams = [{**team_brief(t), "players": t.players.filter(active=True).count(), "mine": t.id in ids} for t in org.teams.filter(suspended=False).order_by("name")]
        return {"teams": [team_brief(t) for t in teams], "open": [board_json(ch, ids, user, assigned) for ch in live], "orgTeams": org_teams,
                "incoming": [{**board_json(ch, ids), "to": side_json(ch.to_team)} for ch in direct if ch.to_team_id in ids and ch.from_team_id not in ids],
                "outgoing": [{**board_json(ch, ids), "to": side_json(ch.to_team)} for ch in direct if ch.from_team_id in ids],
                "upcoming": [{"id": ch.match_id, "home": ch.from_team.name, "away": ch.to_team.name, "kickoff": ch.match.kickoff.isoformat() if ch.match.kickoff else None}
                             for ch in agreed]}
    b = body(request)
    team = next((t for t in teams if t.id == b.get("teamId")), None)
    if not team:
        raise ApiError(403, "Choose one of your teams.")
    kickoff = kickoff_of(b, now)
    if b.get("toTeamId"):                            # a particular team of the organization: they approve or decline
        other = org.teams.filter(id=b["toTeamId"] if isinstance(b["toTeamId"], int) else -1, suspended=False).first()
        if not other or other.id == team.id:
            raise ApiError(400, "Choose another team of the organization to play.")
        if FriendlyChallenge.objects.filter(status="pending", kickoff__gt=now - timezone.timedelta(hours=OPEN_HOURS)).filter(
                Q(from_team=team, to_team=other) | Q(from_team=other, to_team=team)).exists():
            raise ApiError(409, "There's already a challenge waiting between these two teams.")
        if hit(f"friendly:{org.id}", 30, 3600):
            raise ApiError(429, "Too many challenges. Try again later.")
        ch = FriendlyChallenge.objects.create(from_team=team, to_team=other, to_org=org, kickoff=kickoff, message=text(b, "message", 300), created_by=user)
        log(org, user, f"{team.name} challenged {other.name} to a friendly")
        notify(other, f"{team.name} challenged {other.name} to a friendly",
               [f"{team.name} wants to play {other.name}.", f"Proposed kick-off: {kickoff:%a %d %b %Y, %H:%M} UTC."]
               + ([f"Message: {ch.message}"] if ch.message else []) + ["Approve or decline it on the Find a friendly page."],
               request, org.slug, page="findfriendly")
        return {"ok": True, "challenge": challenge_json(ch, org, ids)}
    if FriendlyChallenge.objects.filter(from_team=team, to_org=org, to_team__isnull=True, status="pending",
                                        kickoff__gt=now - timezone.timedelta(hours=OPEN_HOURS)).count() >= 3:
        raise ApiError(409, f"{team.name} already has 3 open requests. Cancel one first.")
    if hit(f"friendly:{org.id}", 30, 3600):
        raise ApiError(429, "Too many requests. Try again later.")
    ch = FriendlyChallenge.objects.create(from_team=team, to_org=org, kickoff=kickoff, message=text(b, "message", 300), created_by=user)
    log(org, user, f"{team.name} is looking for a friendly ({kickoff:%a %d %b, %H:%M} UTC)")
    notify(None, f"{team.name} is looking for a friendly", [f"{team.name} wants a friendly: {kickoff:%a %d %b %Y, %H:%M} UTC.",
                                                           "Any team can take it on the Find a friendly page."] + ([f"Message: {ch.message}"] if ch.message else []),
           request, org.slug, org=org, page="findfriendly", skip=team)
    return {"ok": True, "request": board_json(ch, ids, user)}


@endpoint("POST", login_required=True)
def friendly_board_action(request, user, ip, slug, ch_id, action):
    org, m = access(user, slug, "org.view")
    ids = {t.id for t in my_teams(m)}
    with transaction.atomic():
        ch = (FriendlyChallenge.objects.select_for_update().select_related("from_team__org", "to_org", "to_team")
              .filter(id=ch_id, from_team__org=org, to_org=org).first())
        if not ch or state(ch) != "pending":
            raise ApiError(404, "That request isn't open any more.")
        if ch.to_team_id:                            # a challenge to a particular team: they approve or decline
            if action == "cancel":
                if ch.from_team_id not in ids:
                    raise ApiError(403, "Only the challenging team can cancel it.")
                ch.status = "cancelled"
            elif action in ("approve", "decline"):
                if ch.to_team_id not in ids:
                    raise ApiError(403, "Only the challenged team can answer it.")
                ch.status = "accepted" if action == "approve" else "declined"
                ch.answered_by, ch.answered = user, timezone.now()
                if action == "approve":
                    if ch.kickoff < timezone.now():
                        ch.kickoff = timezone.now()
                    ch.match = make_match(ch)
            else:
                raise ApiError(404, "Unknown action.")
            ch.save()
            pair = f"{ch.from_team.name} v {ch.to_team.name}"
            log(org, user, f"friendly {pair}: {ch.status}")
            if action in ("approve", "decline"):
                notify(ch.from_team, f"{ch.to_team.name} {ch.status} your friendly", [f"{pair} on {ch.kickoff:%a %d %b %Y, %H:%M} UTC: {ch.status}."]
                       + (["The match is set. Good luck!"] if ch.status == "accepted" else []), request, org.slug, page="findfriendly")
            return {"ok": True, "status": ch.status, "match": {"id": ch.match_id, "slug": ch.match.slug} if ch.match_id else None}
        if action == "cancel":
            if ch.from_team_id not in ids:
                raise ApiError(403, "Only that team can cancel its request.")
            ch.status = "cancelled"
            ch.save(update_fields=["status"])
            return {"ok": True}
        if action != "take":
            raise ApiError(404, "Unknown action.")
        tid = body(request).get("teamId")
        if tid not in ids or tid == ch.from_team_id:
            raise ApiError(400, "Choose one of your teams (not the one that posted it).")
        ch.to_team = Team.objects.get(id=tid)
        ch.status, ch.answered_by, ch.answered = "accepted", user, timezone.now()
        if ch.kickoff < timezone.now():
            ch.kickoff = timezone.now()
        ch.match = make_match(ch)
        ch.save()
    log(org, user, f"friendly set: {ch.from_team.name} v {ch.to_team.name} ({ch.kickoff:%a %d %b, %H:%M} UTC)")
    notify(ch.from_team, f"{ch.to_team.name} will play you", [f"{ch.to_team.name} took your friendly request.",
                                                             f"{ch.from_team.name} v {ch.to_team.name}, {ch.kickoff:%a %d %b %Y, %H:%M} UTC."],
           request, org.slug)
    return {"ok": True, "match": {"id": ch.match_id, "slug": ch.match.slug}}


@endpoint("POST", login_required=True)
def org_friendly_action(request, user, ip, slug, ch_id, action):
    org, m = access(user, slug, "org.view")
    mine_ids = {t.id for t in manageable(m)}
    with transaction.atomic():
        ch = (FriendlyChallenge.objects.select_for_update().select_related("from_team__org", "to_team__org", "to_org", "match")
              .filter(Q(from_team__org=org) | Q(to_org=org), id=ch_id).first())
        if not ch:
            raise ApiError(404, "Challenge not found.")
        info = challenge_json(ch, org, mine_ids, can(m.role, "fixtures.manage", org))
        if action == "delete":
            if not info["canDelete"]:
                raise ApiError(403, "Once accepted, a friendly can only be deleted by the organization hosting it."
                               if info["status"] == "accepted" else "Only the managers of the two teams can delete this friendly.")
            pair = f"{ch.from_team.name} v {to_name(ch)}"
            delete_friendly(ch)
            for o in {ch.from_team.org, ch.to_org}:
                log(o, user if o.id == org.id else None, f"deleted the friendly {pair}")
            return {"ok": True, "deleted": True}
        if action == "edit":
            if not info["canEdit"]:
                raise ApiError(403, "This friendly can't be changed any more." if info["played"] or info["status"] not in ("pending", "accepted")
                               else "Once accepted, only the organization hosting the friendly can change it.")
            b, now = body(request), timezone.now()
            changes = []
            if "kickoff" in b:
                both = ch.from_team_id in mine_ids and ch.to_team_id in mine_ids
                kickoff = now if b.get("kickoff") == "now" else moment(b, "kickoff")
                earliest = now - (timezone.timedelta(days=PAST_DAYS) if both else timezone.timedelta(minutes=30))
                if not kickoff or kickoff > now + timezone.timedelta(days=MAX_DAYS):
                    raise ApiError(400, "Choose when the friendly is played.")
                if kickoff < earliest:
                    raise ApiError(400, "That time has already passed. Pick now or a later time.")
                if kickoff != ch.kickoff:
                    ch.kickoff = kickoff
                    changes.append(f"kick-off {kickoff:%a %d %b %Y, %H:%M} UTC")
                    if ch.match_id:
                        ch.match.kickoff = kickoff
                        ch.match.save(update_fields=["kickoff"])
            if "message" in b:
                ch.message = text(b, "message", 300)
            ch.save()
            pair = f"{ch.from_team.name} v {to_name(ch)}"
            for o in {ch.from_team.org, ch.to_org}:
                log(o, user if o.id == org.id else None, f"changed the friendly {pair}" + (f": {', '.join(changes)}" if changes else ""))
            if changes:                                  # tell the other side
                other = ch.to_team if ch.from_team.org_id == org.id and ch.from_team_id in mine_ids else ch.from_team
                notify(other, f"Friendly {pair}: new time", [f"{pair} is now on {ch.kickoff:%a %d %b %Y, %H:%M} UTC.", f"Changed by {user.username}."],
                       request, (other.org.slug if other else ch.to_org.slug), org=None if other else ch.to_org)
            return {"ok": True, "challenge": challenge_json(ch, org, mine_ids, can(m.role, "fixtures.manage", org))}
        if action == "cancel":
            if not info["canCancel"]:
                raise ApiError(403, "Only the challenging team's managers can cancel a waiting challenge.")
            ch.status = "cancelled"
        elif action in ("accept", "decline"):
            if not info["canAnswer"]:
                raise ApiError(403, "Only the challenged team's managers can answer a waiting challenge."
                               if info["status"] == "pending" else f"This challenge is {info['status']}.")
            ch.status = "accepted" if action == "accept" else "declined"
            if action == "accept" and not ch.to_team_id:     # a challenge to the organization: they pick the team
                b = body(request)
                ch.to_team = next((t for t in manageable(m) if t.id == b.get("teamId")), None)
                if not ch.to_team:
                    raise ApiError(400, "Choose which of your teams plays.")
            if action == "accept":
                if ch.kickoff < timezone.now():          # accepted after the proposed time: they play now
                    ch.kickoff = timezone.now()
                ch.match = make_match(ch)
        else:
            raise ApiError(404, "Unknown action.")
        ch.answered_by, ch.answered = user, timezone.now()
        ch.save()
    pair = f"{ch.from_team.name} v {to_name(ch)}"
    for o in {ch.from_team.org, ch.to_org}:
        log(o, user if o.id == org.id else None, f"friendly {pair}: {ch.status}")
    if action in ("accept", "decline"):
        notify(ch.from_team, f"{to_name(ch)} {ch.status} your friendly", [f"{pair} on {ch.kickoff:%a %d %b %Y, %H:%M} UTC: {ch.status}."]
               + (["The match is in your Friendlies series. Good luck!"] if ch.status == "accepted" else []), request, ch.from_team.org.slug)
    return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}
