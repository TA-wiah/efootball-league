"""Match reminders: a few hours before kick-off, everyone on both teams gets an email, and a text message too when the
team's organization switched on automatic text messages: each team's people only, from its own organization's
SMS credits.

Runs at most every 10 minutes when someone opens the site, or on demand: python manage.py send_reminders
(e.g. as an hourly scheduled task). Each match is reminded once.
"""
import logging
import time

from django.db.models import Q
from django.utils import timezone

from league import emailer
from league.http import ApiError, site_url
from orgs.models import Membership

from .models import Match

REMIND_HOURS = 3
log = logging.getLogger("league")
_last = {"t": 0.0}


def team_members(team_ids):
    """Everyone assigned to these teams (players, coaches, team managers…), in any organization."""
    return Membership.objects.select_related("user").filter(teams__id__in=team_ids).distinct()


def email_teams(team_ids, subject, lines, link, button="Open"):
    """Best effort: email everyone on these teams (when email is set up)."""
    if not emailer.ready():
        return 0
    txt, html = emailer.body(subject, lines, link, button, "You get this because you're on one of these teams.")
    sent = 0
    for to in {m.user.email for m in team_members(team_ids) if m.user.email}:
        try:
            emailer.send(to, subject, txt, html)
            sent += 1
        except emailer.MailError:
            pass
    return sent


def text_team(team, message, kind="notice"):
    """Best effort: an SMS to everyone on the team with a phone, paid from the team's organization's credits, only when
    that organization turned on automatic text messages. Returns how many people it went to."""
    org = team.org
    if not org.auto_sms_reminders:
        return 0
    numbers = list(Membership.objects.filter(org=org, teams=team).exclude(user__phone="").values_list("user__phone", flat=True).distinct())
    if not numbers:
        return 0
    try:
        from sms.api import send_text
        send_text(org, None, numbers, message[:450], kind=kind)
        return len(numbers)
    except ApiError as e:                                  # no credits, SMS off, blocked…: emails still go
        log.info("SMS to %s not sent: %s", team.name, e.message)
        return 0


def send_due(force=False):
    now_t = time.monotonic()
    if not force and now_t - _last["t"] < 600:
        return 0
    _last["t"] = now_t
    now = timezone.now()
    due = (Match.objects.select_related("competition__org", "home__team__org", "away__team__org")
           .filter(status="scheduled", reminded__isnull=True, home__isnull=False, away__isnull=False,
                   kickoff__gt=now, kickoff__lte=now + timezone.timedelta(hours=REMIND_HOURS))[:50])
    done = 0
    base = site_url()
    for m in due:
        if not Match.objects.filter(id=m.id, reminded__isnull=True).update(reminded=now):
            continue                                       # someone else sent it already
        org = m.competition.org
        from sms import templates
        when = templates.when(org, m.kickoff)
        pair = f"{m.home.team.name} v {m.away.team.name}"
        email_teams([m.home.team_id, m.away.team_id], f"Match reminder: {pair}",
                    [f"{pair} kicks off {when} ({m.competition.name}).", "Before kick-off press “We’re here”, and after the match send the result with a screenshot."],
                    f"{base}/app/report/{m.id}", "Open the match")
        try:
            text, _ = templates.build(org, "match_reminder", base, {"match": m.id})
        except ApiError:
            text = f"Reminder: {pair}, {when}. {m.competition.name}."
        for team in (m.home.team, m.away.team):            # each team: only its own people, from its own organization's credits
            text_team(team, text, kind="reminder")
        done += 1
    return done
