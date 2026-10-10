"""Friendlies between teams of different organizations: "Challenge a team".

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
from orgs.permissions import can

from . import engine
from .api import moment, team_brief
from .models import Competition, Entry, FriendlyChallenge, Match, Team
from .public import listed

MAX_DAYS = 180


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


def state(ch):
    return "expired" if ch.status == "pending" and ch.kickoff < timezone.now() else ch.status


def side_json(t):
    return {**team_brief(t), "org": {"name": t.org.name, "slug": t.org.slug}}


def challenge_json(ch, org, mine_ids):
    sent = ch.from_team.org_id == org.id
    st = state(ch)
    return {"id": ch.id, "direction": "sent" if sent else "received", "from": side_json(ch.from_team), "to": side_json(ch.to_team),
            "kickoff": ch.kickoff.isoformat(), "message": ch.message or None, "status": st, "created": ms(ch.created),
            "by": getattr(ch.created_by, "username", None), "answeredBy": getattr(ch.answered_by, "username", None),
            "match": {"id": ch.match_id, "slug": ch.match.slug} if ch.match_id else None,
            "canAnswer": not sent and st == "pending" and ch.to_team_id in mine_ids,
            "canCancel": sent and st == "pending" and ch.from_team_id in mine_ids}


def managers_with_email(team):
    """Who to tell about a challenge: people in the team's organization who manage it."""
    out = []
    for mb in Membership.objects.select_related("user", "org").filter(org_id=team.org_id).exclude(user__email=""):
        if can(mb.role, "teams.manage", mb.org) or (can(mb.role, "teams.manage_assigned", mb.org) and mb.teams.filter(id=team.id).exists()):
            out.append(mb.user.email)
    return out[:20]


def notify(team, subject, lines, request, org_slug):
    if not emailer.ready():
        return
    link = f"{site_url(request)}/app/org/{org_slug}/friendlies"
    txt, html = emailer.body(subject, lines, link, "Open friendlies", "You get this because you manage this team.")
    for to in managers_with_email(team):
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


@endpoint("GET", login_required=True)
def team_search(request, user, ip):
    """Teams that can be challenged (see challengeable), found by team or organization name."""
    q = (request.GET.get("q") or "").strip()[:60]
    if len(q) < 2:
        return {"teams": []}
    teams = challengeable().filter(Q(name__icontains=q) | Q(org__name__icontains=q)).order_by("name")[:20]
    return {"teams": [side_json(t) for t in teams]}


@endpoint("GET", "POST", login_required=True)
def org_friendlies(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    mine = manageable(m)
    mine_ids = {t.id for t in mine}
    if request.method == "GET":
        rows = (FriendlyChallenge.objects.select_related("from_team__org", "to_team__org", "created_by", "answered_by", "match")
                .filter(Q(from_team__org=org) | Q(to_team__org=org)).order_by("-created")[:200])
        return {"challenges": [challenge_json(ch, org, mine_ids) for ch in rows], "teams": [team_brief(t) for t in mine]}
    b = body(request)
    mine_t = next((t for t in mine if t.id == b.get("fromTeamId")), None)
    if not mine_t:
        raise ApiError(403, "Choose one of the teams you manage.")
    other = challengeable().filter(id=b.get("toTeamId") if isinstance(b.get("toTeamId"), int) else -1).first()
    if not other:
        raise ApiError(404, "Team not found. Search for the team by name.")
    if other.org_id == org.id:
        raise ApiError(400, "That team is in your own organization: add the friendly from Fixtures instead.")
    kickoff = moment(b, "kickoff")
    now = timezone.now()
    if not kickoff or kickoff < now or kickoff > now + timezone.timedelta(days=MAX_DAYS):
        raise ApiError(400, f"Pick a kick-off in the future (within {MAX_DAYS} days).")
    if FriendlyChallenge.objects.filter(status="pending", kickoff__gt=now).filter(
            Q(from_team=mine_t, to_team=other) | Q(from_team=other, to_team=mine_t)).exists():
        raise ApiError(409, "There's already a challenge waiting between these two teams.")
    if hit(f"friendly:{org.id}", 30, 3600):
        raise ApiError(429, "Too many challenges. Try again later.")
    ch = FriendlyChallenge.objects.create(from_team=mine_t, to_team=other, kickoff=kickoff, message=text(b, "message", 300), created_by=user)
    log(org, user, f"challenged {other.name} ({other.org.name}) to a friendly against {mine_t.name}")
    log(other.org, None, f"{mine_t.name} ({org.name}) challenged {other.name} to a friendly")
    notify(other, f"{mine_t.name} challenged {other.name} to a friendly",
           [f"{mine_t.name} from {org.name} wants to play {other.name}.", f"Proposed kick-off: {kickoff:%a %d %b %Y, %H:%M} UTC."]
           + ([f"Message: {ch.message}"] if ch.message else []) + ["Accept or decline it in your organization's Friendlies page."],
           request, other.org.slug)
    return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}


@endpoint("POST", login_required=True)
def org_friendly_action(request, user, ip, slug, ch_id, action):
    org, m = access(user, slug, "org.view")
    mine_ids = {t.id for t in manageable(m)}
    with transaction.atomic():
        ch = (FriendlyChallenge.objects.select_for_update().select_related("from_team__org", "to_team__org")
              .filter(Q(from_team__org=org) | Q(to_team__org=org), id=ch_id).first())
        if not ch:
            raise ApiError(404, "Challenge not found.")
        info = challenge_json(ch, org, mine_ids)
        if action == "cancel":
            if not info["canCancel"]:
                raise ApiError(403, "Only the challenging team's managers can cancel a waiting challenge.")
            ch.status = "cancelled"
        elif action in ("accept", "decline"):
            if not info["canAnswer"]:
                raise ApiError(403, "Only the challenged team's managers can answer a waiting challenge."
                               if info["status"] == "pending" else f"This challenge is {info['status']}.")
            ch.status = "accepted" if action == "accept" else "declined"
            if action == "accept":
                c = host_series(ch.from_team.org)
                home, _ = Entry.objects.get_or_create(competition=c, team=ch.from_team)
                away, _ = Entry.objects.get_or_create(competition=c, team=ch.to_team)
                ch.match = Match.objects.create(competition=c, stage="league", round=1, round_name="Friendly", home=home, away=away,
                                                kickoff=ch.kickoff, slug=engine.unique_slug(Match, f"{ch.from_team.name} v {ch.to_team.name} friendly", "match", limit=120))
        else:
            raise ApiError(404, "Unknown action.")
        ch.answered_by, ch.answered = user, timezone.now()
        ch.save()
    pair = f"{ch.from_team.name} v {ch.to_team.name}"
    for o in {ch.from_team.org, ch.to_team.org}:
        log(o, user if o.id == org.id else None, f"friendly {pair}: {ch.status}")
    if action in ("accept", "decline"):
        notify(ch.from_team, f"{ch.to_team.name} {ch.status} your friendly", [f"{pair} on {ch.kickoff:%a %d %b %Y, %H:%M} UTC: {ch.status}."]
               + (["The match is in your Friendlies series. Good luck!"] if ch.status == "accepted" else []), request, ch.from_team.org.slug)
    return {"ok": True, "challenge": challenge_json(ch, org, mine_ids)}
