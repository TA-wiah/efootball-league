"""Ready-made texts. The server writes them from real data, so organizers just pick one and press send."""
import zoneinfo

from django.utils import timezone

from competitions import bracket
from competitions.models import Competition, Match
from league.http import ApiError
from orgs.models import Membership

TEMPLATES = {
    "match_reminder": "Match reminder",
    "fixture_change": "Match time changed",
    "result": "Result",
    "fixtures_out": "Fixtures are out",
    "notice": "Your own message",
}


def when(org, dt):
    tz = org.timezone or "UTC"
    try:
        local = timezone.localtime(dt, zoneinfo.ZoneInfo(tz))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        local = timezone.localtime(dt)
    return local.strftime("%a %d %b, %H:%M")


def public(c):
    return c.visibility != "private" and not c.suspended and c.org.status == "active"


def team_people(org, team_ids):
    """Members (players and team staff) assigned to these teams who have a phone number."""
    return list(Membership.objects.filter(org=org, teams__id__in=team_ids).exclude(user__phone="").values_list("id", flat=True).distinct())


def build(org, key, base, b):
    """(text, member ids it goes to by default) for a template, from the request's match/competition/body."""
    if key not in TEMPLATES:
        raise ApiError(400, "Choose a message.")
    if key == "notice":
        return (str(b.get("body") or "").strip()[:918], [])
    if key == "fixtures_out":
        c = Competition.objects.select_related("org").filter(org=org, slug=str(b.get("competition") or "")).first()
        if not c:
            raise ApiError(400, "Choose the competition.")
        link = f" See them here: {base}/competition/{c.slug}/fixtures" if public(c) else ""
        teams = list(c.entries.values_list("team_id", flat=True))
        return (f"{org.name}: the fixtures for {c.name} are out.{link}", team_people(org, teams))
    m = Match.objects.select_related("competition__org", "home__team", "away__team").filter(competition__org=org, id=b.get("match")).first()
    if not m:
        raise ApiError(400, "Choose the match.")
    home = m.home.team.name if m.home else (bracket.label(m.home_from) or "To be decided")
    away = m.away.team.name if m.away else (bracket.label(m.away_from) or "To be decided")
    link = f"{base}/match/{m.slug}" if public(m.competition) else ""
    teams = [t for t in (m.home.team_id if m.home else None, m.away.team_id if m.away else None) if t]
    if key == "result":
        if m.home_score is None:
            raise ApiError(400, "That match has no result yet.")
        pens = f" ({m.home_pens}-{m.away_pens} on penalties)" if m.home_pens is not None else ""
        table = f" Table: {base}/competition/{m.competition.slug}/table" if public(m.competition) and m.stage == "league" else ""
        return (f"Full time: {home} {m.home_score}-{m.away_score} {away}{pens}. {m.competition.name}.{table}", team_people(org, teams))
    if not m.kickoff:
        raise ApiError(400, "That match has no date yet.")
    venue = f" at {m.venue}" if m.venue else ""
    follow = f" Follow it: {link}" if link else ""
    if key == "fixture_change":
        return (f"{org.name}: {home} v {away} ({m.competition.name}) is now on {when(org, m.kickoff)}{venue}.{follow}", team_people(org, teams))
    return (f"Reminder from {org.name}: {home} v {away}, {when(org, m.kickoff)}{venue}. {m.competition.name}.{follow}", team_people(org, teams))


def payment_reminder(inv):
    due = f", due {inv.due_date:%d %b}" if inv.due_date else ""
    return (f"Reminder from {inv.org.name}: hi {inv.customer_name.split()[0]}, your bill of {inv.currency} {inv.amount:.2f} for "
            f"{inv.description} is still unpaid{due}. Pay securely here: {inv.pay_url}")
