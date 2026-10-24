"""Flyers for the big knockout matches (quarter-finals, semi-finals, the final) and for a team's lineup: the page draws
them as PNG images to download and share. The headline and caption can be written by a text AI (Aries by default,
switchable in Platform settings → AI); only public match details are sent, and built-in wording is used when the
AI is off or doesn't answer."""
import json
import re
import urllib.error
import urllib.request

from league.http import ApiError, endpoint
from orgs.api import access
from orgs.permissions import PLAYER

from .api import MATCHES, logo_url
from .public import org_logo, team_view

BIG_ROUNDS = {"Final": "Grand Final", "Semi-finals": "Semi-final", "Quarter-finals": "Quarter-final"}
TEAM_ROLES = {PLAYER, "team_manager", "coach"}
ARIES = "https://aries.doy.best/api/v1/aries/chat"


def text_ai():
    from superadmin import store
    s = store.get("ai")
    provider = s.get("text") if s.get("text") in ("aries", "grok", "off") else "aries"
    if provider == "grok" and not s.get("grok_key"):
        provider = "aries"                                   # no Grok key saved: Aries writes instead
    return {"provider": provider, "model": s.get("grok_model") or "grok-4" if provider == "grok" else s.get("aries_model") or "aries-1.3",
            "key": s.get("grok_key", "") if provider == "grok" else ""}


def winner(m):
    """The winning side of a played match (penalties count), or None for a draw."""
    if m.home_score is None or m.away_score is None:
        return None
    if m.home_score != m.away_score:
        return "home" if m.home_score > m.away_score else "away"
    if m.home_pens is not None and m.away_pens is not None and m.home_pens != m.away_pens:
        return "home" if m.home_pens > m.away_pens else "away"
    return None


def flyer_json(m):
    t = lambda e: {**team_view(e.team, set()), "id": e.team_id} if e else None
    org = m.competition.org
    return {"id": m.id, "stage": BIG_ROUNDS.get(m.round_name, m.round_name), "leg": m.leg, "winner": winner(m), "kickoff": m.kickoff.isoformat() if m.kickoff else None,
            "played": m.home_score is not None and m.away_score is not None, "hs": m.home_score, "as": m.away_score, "hp": m.home_pens, "ap": m.away_pens,
            "competition": {"name": m.competition.name, "season": m.competition.season, "logo": logo_url("competition", m.competition)},
            "home": t(m.home), "away": t(m.away), "org": {"name": org.name, "color": org.brand_color or None},
            "orgLogo": org_logo(org)}


@endpoint("GET", login_required=True)
def org_flyers(request, user, ip, slug):
    """The quarter-finals, semi-finals and finals with both teams known. Team members see their own team's."""
    org, m = access(user, slug, "org.view")
    qs = MATCHES.select_related("competition__org").filter(competition__org=org, stage="knockout", round_name__in=list(BIG_ROUNDS),
                                                         home__isnull=False, away__isnull=False)
    if m.role in TEAM_ROLES:
        mine = list(m.teams.values_list("id", flat=True))
        qs = qs.filter(home__team_id__in=mine) | qs.filter(away__team_id__in=mine)
    rows = sorted(qs.distinct()[:60], key=lambda x: (x.home_score is not None, x.kickoff is None, x.kickoff.timestamp() if x.kickoff else 0))
    return {"flyers": [flyer_json(x) for x in rows[:30]], "textAi": text_ai()["provider"] != "off"}


def fallback(f):
    big = f["stage"]
    if f["played"] and f["winner"] and big == "Grand Final":
        return {"headline": "Champions", "caption": "Crowned. Every match, every goal, all for this."}
    if f["played"]:
        return {"headline": "Full time", "caption": f"{f['competition']['name']} {big.lower()}: what a match."}
    lines = {"Grand Final": "One match. One trophy. Who lifts it?", "Semi-final": "One step from the final.",
             "Quarter-final": "Eight became four? Not yet. Win and you're in the semis."}
    return {"headline": big, "caption": lines.get(big, "Knockout football. No second chances.")}


def clean_text(s, limit):
    s = re.sub(r"[^\w\s.,:;!?'’\-–&()/]", "", str(s or ""), flags=re.UNICODE)       # no emojis, hashtags or markup
    return re.sub(r"\s+", " ", s).strip().strip('"').strip()[:limit]


def offline():
    from django.conf import settings
    if getattr(settings, "AI_OFFLINE", False):
        raise urllib.error.URLError("AI is offline while testing")


def ask_grok(prompt, model, key):
    offline()
    from .lineup import GROK
    req = urllib.request.Request(GROK, data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": .8}).encode(),
                                 method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return str(json.loads(r.read().decode())["choices"][0]["message"]["content"] or "")


def ask_aries(prompt, model):
    offline()
    req = urllib.request.Request(ARIES, data=json.dumps({"prompt": prompt, "model": model}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    if not data.get("success"):
        raise ValueError("no answer")
    return str(data.get("response") or "")


@endpoint("POST", login_required=True)
def flyer_caption(request, user, ip, slug, match_id):
    """A headline and a one-line caption for the flyer, written by the text AI (or built in)."""
    org, m = access(user, slug, "org.view")
    match = MATCHES.select_related("competition__org").filter(competition__org=org, id=match_id, stage="knockout").first()
    if not match or not match.home_id or not match.away_id:
        raise ApiError(404, "Match not found.")
    f = flyer_json(match)
    base = fallback(f)
    cfg = text_ai()
    if cfg["provider"] == "off":
        return {**base, "by": "built-in"}
    stage = f["stage"].lower()
    champions = f["played"] and f["winner"] and f["stage"] == "Grand Final"
    about = ("crowning the champions of the competition (their name is already shown)" if champions else
             f"celebrating a {stage} that has just been played (the final score is already shown)" if f["played"]
             else f"about what is at stake in a {stage}")
    prompt = (f"You write one hype line for an eFootball {f['stage']} flyer. The flyer already shows the team names, the competition, "
              f"the date and the score, so do not mention any of them. Write one punchy line (6 to 12 words) {about}. "
              "Plain text only: no hashtags, no emojis, no quotation marks. Reply with the line only.")
    # the chain: Grok (when chosen and its key is saved) → Aries → our own words
    from superadmin import store
    writers = []
    if cfg["provider"] == "grok":
        writers.append(("grok", lambda: ask_grok(prompt, cfg["model"], cfg["key"])))
    writers.append(("aries", lambda: ask_aries(prompt, store.get("ai").get("aries_model") or "aries-1.3")))
    for name, ask in writers:
        try:
            caption = clean_text(ask(), 120)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            continue
        if len(caption) >= 8 and "high load" not in caption.lower():    # Aries sometimes answers that it's busy
            return {"headline": base["headline"], "caption": caption, "by": name}
    return {**base, "by": "built-in"}
