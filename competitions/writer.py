"""Text written by the text AI (Grok when chosen, then Aries, then our own words): match reports, round-up
announcements, WhatsApp share messages, competition descriptions and lineup flyer captions.

Only public facts are sent (teams, scores, scorers, competition details). Every prompt asks for plain text from the
facts given and nothing invented; what comes back is cleaned (no emojis, hashtags or markup), and people can edit it
before it's published (match reports and round-ups) or it's only a suggestion (descriptions)."""
import re

from django.core.cache import cache
from django.utils import timezone

from league.http import ApiError, body, endpoint, text
from league.logic import hit
from orgs.api import access, log

from .flyers import ask_aries, ask_grok, clean_text, text_ai
from .models import Competition, Match

GOALS = ("goal", "penalty_goal", "own_goal")
RULES = "Use only the facts given; never invent players, events or numbers. Plain text only: no hashtags, no emojis, no quotation marks."


def write(prompt, limit):
    """(text, by): Grok (when chosen and its key is saved) → Aries → None (the caller uses its own words)."""
    import json
    import urllib.error
    from superadmin import store
    cfg = text_ai()
    if cfg["provider"] == "off":
        return None, "built-in"
    writers = []
    if cfg["provider"] == "grok":
        writers.append(("grok", lambda: ask_grok(prompt, cfg["model"], cfg["key"])))
    writers.append(("aries", lambda: ask_aries(prompt, store.get("ai").get("aries_model") or "aries-1.3")))
    for name, ask in writers:
        try:
            out = clean_text(ask(), limit)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            continue
        if len(out) >= 12 and "high load" not in out.lower():
            return out, name
    return None, "built-in"


# ---------- match reports ----------
def facts(m):
    home, away = m.home.team.name, m.away.team.name
    goals = []
    for e in m.events.select_related("player").filter(kind__in=GOALS).order_by("minute", "id"):
        who = (e.player.name if e.player else e.player_name) or "an unnamed player"
        team = home if e.side == "home" else away
        goals.append(f"{who}{' own goal' if e.kind == 'own_goal' else ' penalty' if e.kind == 'penalty_goal' else ''}"
                     f"{f' {e.minute} min' if e.minute is not None else ''} for {team}")
    pens = f", {m.home_pens}-{m.away_pens} on penalties" if m.home_pens is not None and m.away_pens is not None else ""
    how = {"disconnect": " (decided after a connection dropped)", "forfeit": " (a forfeit)", "walkover": " (a walkover)"}.get(m.decided, "")
    return (f"Competition: {m.competition.name}, {m.round_name or 'round ' + str(m.round)}. Result: {home} {m.home_score}-{m.away_score} {away}{pens}{how}. "
            + (f"Goals: {'; '.join(goals)}." if goals else "No scorers recorded."))


def builtin_report(m):
    home, away, hs, as_ = m.home.team.name, m.away.team.name, m.home_score, m.away_score
    if hs == as_:
        line = f"{home} and {away} drew {hs}-{as_}"
        if m.home_pens is not None and m.away_pens is not None and m.home_pens != m.away_pens:
            line += f", and {home if m.home_pens > m.away_pens else away} won {max(m.home_pens, m.away_pens)}-{min(m.home_pens, m.away_pens)} on penalties"
    else:
        win, lose = (home, away) if hs > as_ else (away, home)
        line = f"{win} beat {lose} {max(hs, as_)}-{min(hs, as_)}"
    scorers = {}
    for e in m.events.select_related("player").filter(kind__in=("goal", "penalty_goal")):
        n = (e.player.name if e.player else e.player_name).strip()
        if n:
            scorers[n] = scorers.get(n, 0) + 1
    top = sorted(scorers.items(), key=lambda x: -x[1])[:3]
    tail = ("; " + ", ".join(f"{n}{' (' + str(k) + ')' if k > 1 else ''}" for n, k in top) + " scored") if top else ""
    return f"{line} in the {m.competition.name} ({m.round_name or 'round ' + str(m.round)}){tail}."


def write_report(m, force=False):
    """A short report for a played match, saved on the match (not when someone has edited it, unless forced)."""
    if m.home_score is None or m.away_score is None or not m.home_id or not m.away_id:
        return None
    if m.report and m.report_by == "edited" and not force:
        return m.report
    prompt = ("Write a short football match report (2 or 3 sentences, at most 60 words) for fans of an eFootball competition. "
              + RULES + " Facts: " + facts(m))
    out, by = write(prompt, 420)
    m.report, m.report_by = (out, by) if out else (builtin_report(m), "built-in")
    m.save(update_fields=["report", "report_by"])
    return m.report


@endpoint("POST", "PATCH", login_required=True)
def match_report(request, user, ip, slug, match_id):
    """POST: write (or write again) the report. PATCH {text}: save an edited report ("" removes it)."""
    org, m = access(user, slug, "results.enter")
    match = Match.objects.select_related("competition", "home__team", "away__team").filter(competition__org=org, id=match_id).first()
    if not match:
        raise ApiError(404, "Match not found.")
    if request.method == "PATCH":
        match.report, match.report_by = text(body(request), "text", 600), "edited"
        match.save(update_fields=["report", "report_by"])
        log(org, user, f"edited the match report for {match.home.team.name if match.home else ''} v {match.away.team.name if match.away else ''}")
        return {"ok": True, "report": match.report, "by": match.report_by}
    if match.home_score is None:
        raise ApiError(400, "The match has no result yet.")
    if hit(f"write:{user.id}", 60, 3600):
        raise ApiError(429, "That's a lot of writing for one hour. Try again later.")
    return {"ok": True, "report": write_report(match, force=True), "by": match.report_by}


# ---------- round-up announcements ----------
@endpoint("POST", login_required=True)
def roundup(request, user, ip, slug):
    """A draft announcement for a competition: the latest results, the top of the table, the next fixtures."""
    org, m = access(user, slug, "content.edit")
    c = Competition.objects.filter(org=org, slug=text(body(request), "competition", 120)).first()
    if not c:
        raise ApiError(400, "Choose the competition the round-up is about.")
    if hit(f"write:{user.id}", 60, 3600):
        raise ApiError(429, "That's a lot of writing for one hour. Try again later.")
    now = timezone.now()
    played = list(Match.objects.select_related("home__team", "away__team").filter(competition=c, home_score__isnull=False,
                                                                               finished_at__gte=now - timezone.timedelta(days=10)).order_by("-finished_at")[:8])
    nxt = list(Match.objects.select_related("home__team", "away__team").filter(competition=c, home_score__isnull=True, kickoff__gte=now,
                                                                            home__isnull=False, away__isnull=False).order_by("kickoff")[:5])
    from .api import table_payload
    table = []
    try:
        groups = table_payload(c)["groups"]
        rows = groups[0]["rows"] if groups else []
        table = [f"{r['team']['name']} {r['points']} pts" for r in rows[:3]]
    except Exception:                                        # a knockout-only competition has no table
        table = []
    res = [f"{x.home.team.name} {x.home_score}-{x.away_score} {x.away.team.name}" for x in played if x.home and x.away]
    fix = [f"{x.home.team.name} v {x.away.team.name} ({x.kickoff:%a %d %b})" for x in nxt]
    if not res and not fix:
        raise ApiError(400, "Nothing to round up yet: no recent results or upcoming fixtures.")
    title = f"{c.name}: the latest"
    fact = (f"Competition: {c.name}. Recent results: {'; '.join(res) or 'none'}. Top of the table: {', '.join(table) or 'not available'}. "
            f"Next fixtures: {'; '.join(fix) or 'none scheduled'}.")
    out, by = write("Write a short, upbeat round-up announcement (3 to 5 sentences, at most 110 words) for the players and fans of an "
                    "eFootball competition. " + RULES + " Facts: " + fact, 900)
    if not out:
        parts = []
        if res:
            parts.append("Results: " + "; ".join(res) + ".")
        if table:
            parts.append("Top of the table: " + ", ".join(table) + ".")
        if fix:
            parts.append("Coming up: " + "; ".join(fix) + ".")
        out = " ".join(parts)
    return {"ok": True, "title": title, "body": out, "by": by}


# ---------- competition descriptions ----------
@endpoint("POST", login_required=True)
def describe(request, user, ip, slug):
    """A suggested description for a competition from its name, type, format, rules and prizes."""
    org, m = access(user, slug, "competitions.manage")
    b = body(request)
    name = text(b, "name", 120)
    if len(name) < 2:
        raise ApiError(400, "Give the competition a name first.")
    if hit(f"write:{user.id}", 60, 3600):
        raise ApiError(429, "That's a lot of writing for one hour. Try again later.")
    fact = (f"Name: {name}. Organizer: {org.name}{', ' + org.country if org.country else ''}. Type: {text(b, 'kind', 60) or 'competition'}. "
            f"Format: {text(b, 'format', 80) or 'not set'}. Rules: {text(b, 'rules', 600) or 'not given'}. Prizes: {text(b, 'prizes', 200) or 'not given'}.")
    out, by = write("Write a short description (2 to 4 sentences, at most 80 words) inviting eFootball players to this competition. "
                    + RULES + " Facts: " + fact, 700)
    if not out:
        out = f"{name} is an eFootball {text(b, 'kind', 60).lower() or 'competition'} run by {org.name}. Results are confirmed from game screenshots, and every match counts in the platform rankings."
    return {"ok": True, "text": out, "by": by}


# ---------- WhatsApp share messages (public pages) ----------
@endpoint("GET")
def share_text(request, user, ip):
    """A fresh line to share a public page on WhatsApp (kept for a day). Match pages use their report."""
    path = str(request.GET.get("path") or "")[:200]
    key = "share:" + re.sub(r"[^a-z0-9/_-]", "", path.lower())
    got = cache.get(key)
    if got is not None:
        return {"text": got}
    if hit(f"share:{ip}", 30, 3600):
        return {"text": ""}
    line = ""
    mm = re.fullmatch(r"/match/([a-z0-9-]+)", path)
    cm = re.fullmatch(r"/competition/([a-z0-9-]+)(?:/[a-z]+)?", path)
    if mm:
        match = Match.objects.select_related("competition", "home__team", "away__team").filter(slug=mm.group(1)).first()
        if match and match.competition.visibility == "public" and match.home and match.away:
            if match.report:
                line = match.report.split(". ")[0].rstrip(".") + "."
            elif match.home_score is None:
                out, _ = write(f"Write one short line (at most 15 words) inviting friends to follow this eFootball match: "
                               f"{match.home.team.name} v {match.away.team.name}, {match.competition.name}. " + RULES, 160)
                line = out or ""
    elif cm:
        c = Competition.objects.filter(slug=cm.group(1), visibility="public").first()
        if c:
            out, _ = write(f"Write one short line (at most 15 words) inviting friends to follow the eFootball competition {c.name}: "
                           "tables, results and fixtures. " + RULES, 160)
            line = out or ""
    cache.set(key, line, 86400)
    return {"text": line}


# ---------- lineup flyer caption ----------
@endpoint("POST", login_required=True)
def lineup_caption(request, user, ip, slug, team_id):
    from .lineup import member_access
    org, m, t = member_access(user, slug, team_id)
    if hit(f"write:{user.id}", 60, 3600):
        return {"caption": ""}
    starters = [p.name for p in t.players.filter(lineup="starter")[:11]]
    formation = (f"fluid {t.formation_attack} with the ball and {t.formation_defence} without" if t.formation_style == "fluid"
                 else t.formation or "not set")
    out, by = write(f"Write one punchy line (6 to 12 words) for an eFootball team's starting lineup flyer. The flyer already shows the "
                    f"players and the team name, so do not list them. Formation: {formation}. " + RULES, 120)
    return {"caption": out or "", "by": by}
