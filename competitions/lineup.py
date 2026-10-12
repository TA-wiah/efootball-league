"""The squad from an eFootball lineup screenshot (Game Plan): the AI reads each player card's printed position, rating
and (when the screen shows it) name, and whether the card is on the pitch (starting XI) or in the substitutes list.
It never tries to tell who someone is from their face: the lineup screen prints no names, so people review the list,
the names already known for the team are suggested, and returning players are recognised by position and rating.

Uses the platform's AI service (Platform settings → AI), with the same daily limit per person as AI logos.
"""
import json
import re
import time
import urllib.error
import urllib.request

from django.db import transaction
from django.utils import timezone

from league.http import ApiError, body, endpoint, text
from league.logic import hit, over
from league.models import Hit

from . import engine
from .models import POSITIONS, Player

API = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4.1-mini"
CODES = {k for k, _ in POSITIONS if k}
GAME = CODES - {"DF", "MF", "FW"}
MAX_PLAYERS = 40
PROMPT = """This is a screenshot of a football video game lineup screen (eFootball "Game Plan").
Player cards on the pitch are the starting XI; cards in the list beside or below the pitch are substitutes and reserves.
Each card shows a position code (GK, CB, LB, RB, DMF, CMF, LMF, RMF, AMF, LWF, RWF, SS, CF) and a rating number.
Read only text that is printed on the screen. Never identify or guess who a person is from their face, appearance,
kit, flag or badge. If a card has no printed player name, use an empty string for the name.
Return JSON: {"players": [{"position": "CF", "rating": 102, "starter": true, "name": ""}], "formation": "4-2-1-3"}
List starters first (from the goalkeeper forward), then substitutes in the order shown. If it isn't an eFootball
lineup screen, return {"players": []}."""


def config():
    from .ailogo import config as ai
    return ai()


def member_access(user, slug, team_id):
    """Organizers and team staff (team_access), or anyone assigned to this team (players too)."""
    from .api import access, team_access, team_of
    try:
        return team_access(user, slug, team_id)
    except ApiError as e:
        if e.status != 403:
            raise
        org, m = access(user, slug, "org.view")
        t = team_of(org, team_id)
        if not m.teams.filter(id=t.id).exists():
            raise
        return org, m, t


def read_image(cfg, image):
    payload = {"model": MODEL, "response_format": {"type": "json_object"}, "temperature": 0,
               "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT},
                                                         {"type": "image_url", "image_url": {"url": image, "detail": "high"}}]}]}
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
        return json.loads(data["choices"][0]["message"]["content"])
    except urllib.error.HTTPError:
        raise ApiError(502, "The AI service isn't working right now. Please contact the platform administrator.") from None
    except (urllib.error.URLError, TimeoutError):
        raise ApiError(502, "The AI service couldn't be reached. Try again in a moment.") from None
    except (KeyError, IndexError, ValueError):
        raise ApiError(502, "The AI service sent back something unexpected. Try again.") from None


def clean(raw):
    """Only what the game can show: known position codes, ratings 40-130, at most 11 starters."""
    out, starters = [], 0
    for p in (raw.get("players") if isinstance(raw, dict) else None) or []:
        if not isinstance(p, dict):
            continue
        pos = str(p.get("position") or "").upper().strip()
        if pos not in GAME:
            continue
        try:
            rating = int(p.get("rating"))
        except (TypeError, ValueError):
            rating = None
        if rating is not None and not 40 <= rating <= 130:
            rating = None
        starter = bool(p.get("starter")) and starters < 11
        starters += starter
        name = re.sub(r"\s+", " ", str(p.get("name") or "")).strip()[:80]
        out.append({"position": pos, "rating": rating, "starter": starter, "name": name})
        if len(out) >= MAX_PLAYERS:
            break
    formation = str(raw.get("formation") or "")[:12] if isinstance(raw, dict) else ""
    return out, formation if re.fullmatch(r"\d(-\d){2,4}", formation) else ""


def suggest(team, rows):
    """Fill in names we already know: a printed name matched to the squad, or the squad player who had exactly this
    position and rating in the last lineup."""
    squad = list(team.players.all())
    used = set()
    for r in rows:
        r["playerId"] = None
        if r["name"]:
            hit_ = [p for p in squad if engine.same_player(r["name"], p.name) and p.id not in used]
            if len(hit_) == 1:
                r["playerId"], r["name"] = hit_[0].id, hit_[0].name
        elif r["rating"]:
            hit_ = [p for p in squad if p.position == r["position"] and p.rating == r["rating"] and p.id not in used]
            if len(hit_) == 1:
                r["playerId"], r["name"] = hit_[0].id, hit_[0].name
        if r["playerId"]:
            used.add(r["playerId"])
    return rows


def known_names(team):
    from .api import team_events
    names = [p.name for p in team.players.order_by("name")]
    for e in team_events(team).only("player_name", "assist_name"):
        for n in (e.player_name.strip(), e.assist_name.strip()):
            if n and not any(engine.same_player(n, x) for x in names):
                names.append(n)
    return names[:300]


@endpoint("GET", "POST", login_required=True)
def lineup_scan(request, user, ip, slug, team_id):
    """GET: is it available (and how many left today). POST {image}: the players read from the screenshot."""
    org, m, t = member_access(user, slug, team_id)
    cfg = config()
    key = f"ailineup:{user.id}"
    if request.method == "GET":
        used = Hit.objects.filter(key=key, ts__gte=time.time() - 86400).count()
        return {"enabled": cfg["enabled"], "left": max(0, cfg["daily_limit"] - used), "limit": cfg["daily_limit"], "names": known_names(t)}
    if not cfg["enabled"]:
        raise ApiError(400, "Reading screenshots isn't available. Please contact the platform administrator.")
    image = str(body(request, 1_000_000).get("image") or "")
    if not re.match(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$", image):
        raise ApiError(400, "Choose a screenshot (PNG, JPG or WebP).")
    if over(key, cfg["daily_limit"], 86400):
        raise ApiError(429, f"That's today's {cfg['daily_limit']} screenshots. Try again tomorrow, or add the players by hand.")
    hit(key, cfg["daily_limit"] + 1, 86400)
    rows, formation = clean(read_image(cfg, image))
    if not rows:
        raise ApiError(400, "No eFootball player cards were found. Use a screenshot of your Game Plan (lineup) screen.")
    return {"players": suggest(t, rows), "formation": formation}


@endpoint("POST", login_required=True)
def lineup_save(request, user, ip, slug, team_id):
    """Save the reviewed lineup: each row updates the squad player (by id, or the same name) or adds a new one, with
    position, rating and starter/sub. Squad players not in the lineup stay in the squad, without a lineup place."""
    org, m, t = member_access(user, slug, team_id)
    rows = body(request, 20_000).get("players")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_PLAYERS:
        raise ApiError(400, "Add at least one player to the lineup.")
    clean_rows, starters = [], 0
    for r in rows:
        if not isinstance(r, dict):
            raise ApiError(400, "Bad lineup.")
        name = text(r, "name", 80)
        if not name:
            continue                                         # rows without a name are skipped
        pos = str(r.get("position") or "")
        if pos not in CODES:
            raise ApiError(400, f"Choose a position for {name}.")
        rating = r.get("rating")
        if rating in ("", None):
            rating = None
        elif isinstance(rating, bool) or not isinstance(rating, int) or not 40 <= rating <= 130:
            raise ApiError(400, f"{name}'s rating must be a number from 40 to 130.")
        starter = bool(r.get("starter"))
        starters += starter
        clean_rows.append((r.get("playerId"), name, pos, rating, starter))
    if not clean_rows:
        raise ApiError(400, "Type the players' names first.")
    if starters > 11:
        raise ApiError(400, "Only 11 players can start.")
    squad = list(t.players.all())
    if len(squad) + len(clean_rows) > 140:
        raise ApiError(400, "That's too many players for one team.")
    added = updated = 0
    with transaction.atomic():
        Player.objects.filter(team=t).update(lineup="")
        taken = set()
        for pid, name, pos, rating, starter in clean_rows:
            p = next((x for x in squad if x.id == pid and x.id not in taken), None) if isinstance(pid, int) else None
            p = p or next((x for x in squad if x.id not in taken and engine.same_player(name, x.name)), None)
            if p is None:
                if t.players.count() >= 100:
                    raise ApiError(400, "A team can have at most 100 players.")
                p = Player(team=t)
                added += 1
            else:
                updated += 1
            p.name, p.position, p.rating, p.lineup, p.active = name, pos, rating, "starter" if starter else "sub", True
            p.save()
            taken.add(p.id)
            if p not in squad:
                squad.append(p)
        t.lineup_at = timezone.now()
        t.save(update_fields=["lineup_at"])
    from .api import log
    log(org, user, f"updated {t.name}'s lineup ({starters} starting, {len(clean_rows) - starters} subs)")
    return {"ok": True, "added": added, "updated": updated}


def squad_todo(team):
    """What's missing for this team's squad to be picked properly, or None when it's fine."""
    players = [p for p in team.players.all() if p.active]
    with_pos = [p for p in players if p.position]
    if not any(p.position == "GK" for p in players):
        return "no goalkeeper listed"
    if len(with_pos) < 11:
        return f"only {len(with_pos)} player{'' if len(with_pos) == 1 else 's'} with a position"
    return None
