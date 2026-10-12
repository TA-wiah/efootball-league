"""The squad from eFootball screenshots, up to 10 at a time: the AI reads each player card's printed position,
rating and name. The lineup screen (Game Plan) shows who starts and who's on the bench but prints no names; the
collection (player list) screens print names, so their cards are paired with the lineup's by position and rating
(the lineup's ratings are usually a few points higher, from the team boost, so the gap is allowed for and worked
out from the cards that pair up for sure). It never tries to tell who someone is from their face: anything that
can't be paired for sure is left for people to choose, and they review the list before saving.

Uses the platform's AI service (Platform settings → AI): OpenAI, or Google Gemini (has a free tier).
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
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
MAX_IMAGES = 10
CODES = {k for k, _ in POSITIONS if k}
GAME = CODES - {"DF", "MF", "FW"}
MAX_PLAYERS = 40
PROMPT = """This is a screenshot from the football video game eFootball. It is one of two kinds:
- "lineup": the Game Plan screen, a pitch with player cards (the starting XI) and a list of cards beside or below it
  (substitutes and reserves).
- "collection": a grid of player cards (the player list), none of them on a pitch.
Each card shows a rating number, a position code (GK, CB, LB, RB, DMF, CMF, LMF, RMF, AMF, LWF, RWF, SS, CF) and,
on collection cards, the player's name printed on the card (sometimes sideways).
Read only text that is printed on the screen. Never identify or guess who a person is from their face, appearance,
kit, flag or badge. If a card has no printed name, use an empty string for the name.
Return JSON: {"screen": "lineup", "players": [{"position": "CF", "rating": 102, "starter": true, "name": ""}], "formation": "4-2-1-3"}
On a lineup screen list the starters first (from the goalkeeper forward), then the substitutes in the order shown.
On a collection screen every card has "starter": false and there is no formation. If it isn't an eFootball screen,
return {"screen": "", "players": []}."""


def config():
    """Reading screenshots: Gemini when the platform chose it (free tier), otherwise OpenAI."""
    from superadmin import store
    s = store.get("ai")
    vision = s.get("vision") if s.get("vision") in ("openai", "gemini") else "openai"
    key = s.get("gemini_key", "") if vision == "gemini" else s.get("api_key", "")
    return {"enabled": bool(s.get("enabled")) and bool(key), "vision": vision, "key": key,
            "daily_limit": int(s.get("daily_limit") or 5) * MAX_IMAGES}     # each use can be up to 10 screenshots


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
    if cfg["vision"] == "gemini":
        mime, b64 = image[5:].split(";base64,", 1)
        payload = {"contents": [{"parts": [{"text": PROMPT}, {"inline_data": {"mime_type": mime, "data": b64}}]}],
                   "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        req = urllib.request.Request(GEMINI, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": cfg["key"]})
    else:
        payload = {"model": MODEL, "response_format": {"type": "json_object"}, "temperature": 0,
                   "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT},
                                                             {"type": "image_url", "image_url": {"url": image, "detail": "high"}}]}]}
        req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
        if cfg["vision"] == "gemini":
            return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        return json.loads(data["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ApiError(429, "The AI service is busy (its free limit may be used up for now). Try again in a minute.") from None
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


def screen_of(raw, rows):
    s = str(raw.get("screen") or "").lower() if isinstance(raw, dict) else ""
    if s in ("lineup", "collection"):
        return s
    return "lineup" if any(r["starter"] for r in rows) else "collection"


def merge(reads):
    """Several screenshots into one list. reads: [{"screen", "players", "formation"}]. The first lineup screen gives
    the XI and subs; collection cards (with names) fill in the names. Returns (rows, extras, formation): extras are
    named cards that aren't in the lineup (or every named card when there's no lineup screenshot)."""
    lineup = next((r for r in reads if r["screen"] == "lineup"), None)
    pool, seen = [], {}
    for r in reads:
        for p in r["players"]:
            if not p["name"]:
                continue
            k = (" ".join(sorted(engine.name_tokens(p["name"]))), p["position"])
            if k in seen:                                    # the same card on two screenshots
                seen[k]["rating"] = max(seen[k]["rating"] or 0, p["rating"] or 0) or None
                continue
            seen[k] = dict(p, starter=False)
            pool.append(seen[k])
    if not lineup:
        return pool[:MAX_PLAYERS], pool[MAX_PLAYERS:MAX_PLAYERS + 60], ""
    rows = [dict(p) for p in lineup["players"]]
    used = set()

    def cands(row, gap=None):
        return [i for i, c in enumerate(pool) if i not in used and c["position"] == row["position"] and row["rating"] and c["rating"]
                and (row["rating"] - c["rating"] == gap if gap is not None else -1 <= row["rating"] - c["rating"] <= 8)]

    gaps = []
    changed = True
    while changed:                                           # pair the sure ones first: only one card fits
        changed = False
        for row in rows:
            if row["name"]:
                continue
            c = cands(row)
            if len(c) == 1:
                row["name"], row["fromShot"] = pool[c[0]]["name"], True
                used.add(c[0])
                gaps.append(row["rating"] - pool[c[0]]["rating"])
                changed = True
    if gaps:                                                 # then use the team boost worked out from those
        gap = max(set(gaps), key=gaps.count)
        for row in rows:
            if not row["name"]:
                c = cands(row, gap)
                if len(c) == 1:
                    row["name"], row["fromShot"] = pool[c[0]]["name"], True
                    used.add(c[0])
    named = {" ".join(sorted(engine.name_tokens(r["name"]))) for r in rows if r["name"]}
    extras = [c for i, c in enumerate(pool) if i not in used and " ".join(sorted(engine.name_tokens(c["name"]))) not in named]
    return rows, extras[:60], lineup.get("formation", "")


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
    """GET: is it available (and how many left today). POST {image}: one screenshot read (the page sends each of up
    to 10, then asks lineup_merge to put them together)."""
    org, m, t = member_access(user, slug, team_id)
    cfg = config()
    key = f"ailineup:{user.id}"
    if request.method == "GET":
        used = Hit.objects.filter(key=key, ts__gte=time.time() - 86400).count()
        return {"enabled": cfg["enabled"], "left": max(0, cfg["daily_limit"] - used), "limit": cfg["daily_limit"], "names": known_names(t), "perUse": MAX_IMAGES}
    if not cfg["enabled"]:
        raise ApiError(400, "Reading screenshots isn't available. Please contact the platform administrator.")
    image = str(body(request, 1_000_000).get("image") or "")
    if not re.match(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$", image):
        raise ApiError(400, "Choose a screenshot (PNG, JPG or WebP).")
    if over(key, cfg["daily_limit"], 86400):
        raise ApiError(429, f"That's today's {cfg['daily_limit']} screenshots. Try again tomorrow, or add the players by hand.")
    hit(key, cfg["daily_limit"] + 1, 86400)
    raw = read_image(cfg, image)
    rows, formation = clean(raw)
    return {"screen": screen_of(raw, rows) if rows else "", "players": rows, "formation": formation}


@endpoint("POST", login_required=True)
def lineup_merge(request, user, ip, slug, team_id):
    """The screenshots read one by one, put together: the lineup with names from the collection screenshots and the
    squad, plus the other named players found."""
    org, m, t = member_access(user, slug, team_id)
    reads = body(request, 200_000).get("reads")
    if not isinstance(reads, list) or not 1 <= len(reads) <= MAX_IMAGES:
        raise ApiError(400, f"Send 1 to {MAX_IMAGES} screenshots.")
    ok = []
    for r in reads:
        if not isinstance(r, dict):
            raise ApiError(400, "Bad screenshots.")
        rows, formation = clean({"players": r.get("players"), "formation": r.get("formation")})
        if rows:
            ok.append({"screen": r.get("screen") if r.get("screen") in ("lineup", "collection") else screen_of({}, rows), "players": rows, "formation": formation})
    if not ok:
        raise ApiError(400, "No eFootball player cards were found. Use screenshots of your Game Plan (lineup) and your players list.")
    rows, extras, formation = merge(ok)
    return {"players": suggest(t, rows), "extras": extras, "formation": formation,
            "lineups": sum(r["screen"] == "lineup" for r in ok), "collections": sum(r["screen"] == "collection" for r in ok)}


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
