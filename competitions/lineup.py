"""The squad from eFootball screenshots, up to 10 at a time: the AI reads each player card's printed position,
rating and name. The lineup screen (Game Plan) shows who starts and who's on the bench but prints no names; the
collection (player list) screens print names, so their cards are paired with the lineup's by position and rating
(the lineup's ratings are usually a few points higher, from the team boost, so the gap is allowed for and worked
out from the cards that pair up for sure). It never tries to tell who someone is from their face: anything that
can't be paired for sure is left for people to choose, and they review the list before saving.

Uses the platform's AI service (Platform settings → AI): OpenAI, or Google Gemini (has a free tier).
"""
import json
import logging
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
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-flash-latest"
GROK = "https://api.x.ai/v1/chat/completions"           # xAI's Grok: the same request shape as OpenAI's      # always Google's current Flash model, so a retired version never breaks reading
log = logging.getLogger("league")
MAX_IMAGES = 10
READ_DEADLINE = 50          # seconds one screenshot may take across the whole chain of readers
CODES = {k for k, _ in POSITIONS if k}
GAME = CODES - {"DF", "MF", "FW"}
MAX_PLAYERS = 40
PROMPT = """This is a screenshot from the football video game eFootball. It is one of two kinds:
- "lineup": the Game Plan screen, a pitch with player cards (the starting XI) and a list of cards beside or below it
  (substitutes and reserves).
- "collection": a grid of player cards (the player list), none of them on a pitch.
Each card shows a rating number, a position code (GK, CB, LB, RB, DMF, CMF, LMF, RMF, AMF, LWF, RWF, SS, CF), a small
national flag or club badge under the position, and, on collection cards, the player's name printed on the card
(sometimes sideways).
For "badge" give the country of that flag (e.g. "Argentina") or the club of that badge (e.g. "Barcelona"), or "" if
there is none or you can't tell. Read names only from printed text. Never identify or guess who a person is from
their face or appearance, and never use the flag or badge to guess a name. If a card has no printed name, use "".
If a card prints the player's shirt number, give it as "number" (a whole number), otherwise null.
Return JSON: {"screen": "lineup", "players": [{"position": "CF", "rating": 102, "starter": true, "name": "", "badge": "Barcelona", "number": 9}], "formation": "4-2-1-3"}
On a lineup screen list the starters first (from the goalkeeper forward), then the substitutes in the order shown.
On a collection screen every card has "starter": false and there is no formation. If it isn't an eFootball screen,
return {"screen": "", "players": []}."""


RESULT_PROMPT = """This is a screenshot of the full-time result screen of the football video game eFootball.
It shows two teams (left and right) with their names, the final score, and under each team the goal scorers with the
minute of each goal (sometimes "(PK)" or "P" for a penalty, "(OG)" for an own goal) and sometimes the assist.
Read only text printed on the screen. Never identify or guess who a person is from their face or appearance.
Return JSON: {"isResult": true, "left": {"name": "Team A", "score": 2, "goals": [{"name": "L. Messi", "minute": 23,
"penalty": false, "ownGoal": false, "assist": "D. Rice"}]}, "right": {"name": "Team B", "score": 1, "goals": []}}
List each team's goals under the team that got the goal (an own goal goes under the team it counts for, with the
player's printed name and "ownGoal": true). Use "" when an assist isn't shown. If it isn't an eFootball result screen,
return {"isResult": false}."""


GEMINI_KEYS = ("gemini_key", "gemini_key2", "gemini_key3", "gemini_key4", "gemini_key5")


def config():
    """Reading screenshots: a chain of readers, OpenAI → Gemini → Grok, using each one that has a key saved. When one
    fails or is busy, the next takes over (Gemini's backup keys take turns among themselves first)."""
    from superadmin import store
    s = store.get("ai")
    chain = [(name, keys) for name, keys in (("openai", [s.get("api_key", "")]), ("gemini", [s.get(k, "") for k in GEMINI_KEYS]),
                                              ("grok", [s.get("grok_key", "")])) if any(keys)]
    chain = [(name, [k for k in keys if k]) for name, keys in chain]
    first = chain[0] if chain else ("openai", [])
    return {"enabled": bool(s.get("enabled")) and bool(chain), "chain": chain, "vision": first[0], "keys": first[1],
            "gemini_model": s.get("gemini_model") or GEMINI_MODEL, "grok_model": s.get("grok_model") or "grok-4",
            "daily_limit": int(s.get("lineup_per_day") or 1) * MAX_IMAGES,   # each upload can be up to 10 screenshots
            "daily_total": int(s.get("vision_daily_total") or 0)}


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


_turn = {"n": 0}


class Busy(Exception):
    pass


def read_image(cfg, image):
    """Each reader in the chain in turn (OpenAI → Gemini → Grok); within a reader, its keys take turns (each request
    starts with the next key, spreading the load). A busy or failing reader hands over to the next one."""
    from django.core.cache import cache
    chain = cfg.get("chain") or [(cfg["vision"], cfg["keys"])]
    # a reader whose key or model just failed is skipped for a while (unless every reader is in that state)
    live = [(n, k) for n, k in chain if not cache.get(f"reader-down:{n}")] or chain
    deadline = time.monotonic() + READ_DEADLINE              # one screenshot never waits more than this, whatever fails
    last = None
    for name, keys in live:
        sub = dict(cfg, vision=name, keys=keys)
        start = _turn["n"] % len(keys)
        _turn["n"] += 1
        for i in range(len(keys)):
            left = deadline - time.monotonic()
            if left < 5:
                raise ApiError(504, "Reading this screenshot took too long. Try it again, or type the lineup.")
            try:
                return read_with(dict(sub, timeout=min(40, left)), keys[(start + i) % len(keys)], image)
            except Busy as e:
                last = e
                if str(e) in ("401", "403", "404", "key"):       # a bad key or a retired model won't fix itself in a minute
                    cache.set(f"reader-down:{name}", True, 600)
                    break
            except ApiError as e:                             # this reader couldn't read it: the next one may
                last = e
                break
    if isinstance(last, ApiError):
        raise last
    if isinstance(last, Busy) and str(last) == "429":
        raise ApiError(429, "The AI service is busy (its limit may be used up for now). Try again in a minute.")
    if isinstance(last, Busy) and str(last) == "404":
        raise ApiError(502, "The AI model chosen in Platform settings isn't available. Please contact the platform administrator.")
    if isinstance(last, Busy) and str(last) in ("401", "403", "key"):
        raise ApiError(502, "The AI service didn't accept the platform's key. Please contact the platform administrator.")
    raise ApiError(502, "The AI service isn't working right now. Please contact the platform administrator.")


def json_in(text):
    """The JSON object in a model's answer (some wrap it in a code block or a sentence)."""
    text = str(text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a < 0 or b <= a:
            raise
        return json.loads(text[a:b + 1])


def read_with(cfg, key, image, think=False):
    if cfg["vision"] == "gemini":
        mime, b64 = image[5:].split(";base64,", 1)
        gen = {"temperature": 0, "responseMimeType": "application/json"}
        if not think:                                       # reading printed text needs no "thinking": seconds instead of minutes
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        payload = {"contents": [{"parts": [{"text": cfg.get("prompt") or PROMPT}, {"inline_data": {"mime_type": mime, "data": b64}}]}], "generationConfig": gen}
        req = urllib.request.Request(GEMINI.format(model=cfg.get("gemini_model") or GEMINI_MODEL), data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
    else:                                                   # OpenAI, or Grok (same request shape, its own address and model)
        grok = cfg["vision"] == "grok"
        payload = {"model": cfg.get("grok_model") or "grok-4" if grok else MODEL, "temperature": 0,
                   "messages": [{"role": "user", "content": [{"type": "text", "text": cfg.get("prompt") or PROMPT},
                                                             {"type": "image_url", "image_url": {"url": image, "detail": "high"}}]}]}
        if not grok:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(GROK if grok else API, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout") or 40) as r:
            data = json.loads(r.read().decode())
        if cfg["vision"] == "gemini":
            parts = data["candidates"][0]["content"]["parts"]
            return json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))
        return json_in(data["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        log.warning("Reading a lineup screenshot failed (%s, HTTP %s): %s", cfg["vision"], e.code, detail)   # for the admin: the real reason
        if e.code == 400 and "API key" in detail:            # Gemini says a bad key with 400
            raise Busy("key") from None
        if e.code == 400 and "thinking" in detail.lower() and not think and cfg["vision"] == "gemini":
            return read_with(cfg, key, image, think=True)   # a model that must think: ask again without the setting
        if e.code == 400:                                   # the request itself: another key won't help
            raise ApiError(400, "The AI service couldn't read that image. Try another screenshot.") from None
        raise Busy(str(e.code)) from None                   # busy, bad key, retired model, server trouble: try the next key
    except (urllib.error.URLError, TimeoutError):
        raise Busy("network") from None
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
        badge = re.sub(r"[^a-z0-9 ]+", "", str(p.get("badge") or "").lower()).strip()[:40]
        try:
            number = int(p.get("number"))
            number = number if 0 <= number <= 99 else None
        except (TypeError, ValueError):
            number = None
        out.append({"position": pos, "rating": rating, "starter": starter, "name": name, "badge": badge, "number": number})
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
                seen[k]["number"] = seen[k].get("number") if seen[k].get("number") is not None else p.get("number")
                continue
            seen[k] = dict(p, starter=False)
            pool.append(seen[k])
    if not lineup:
        return pool[:MAX_PLAYERS], pool[MAX_PLAYERS:MAX_PLAYERS + 60], ""
    rows = [dict(p) for p in lineup["players"]]
    used = set()

    same_badge = lambda row, c: bool(row.get("badge") and row.get("badge") == c.get("badge"))

    def cands(row, gap=None):
        """Cards this lineup card can be: the same position, or another position with the same flag or badge (a player
        used out of position, like an LB card at CB); when the badge narrows it down, only those."""
        out = [i for i, c in enumerate(pool) if i not in used and (c["position"] == row["position"] or same_badge(row, pool[i]))
               and row["rating"] and c["rating"] and (row["rating"] - c["rating"] == gap if gap is not None else -1 <= row["rating"] - c["rating"] <= 8)]
        both = [i for i in out if same_badge(row, pool[i]) and pool[i]["position"] == row["position"]]
        badged = [i for i in out if same_badge(row, pool[i])]
        if both:                                             # same position and badge first
            return both
        if badged:                                           # then the same badge in another position
            return badged
        return [i for i in out if pool[i]["position"] == row["position"]]

    gaps = []

    def sure():                                              # pair every row only one card fits, again and again
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row["name"]:
                    continue
                c = cands(row)
                if len(c) == 1:
                    row["name"], row["fromShot"] = pool[c[0]]["name"], True
                    if row.get("number") is None:
                        row["number"] = pool[c[0]].get("number")
                    used.add(c[0])
                    gaps.append(row["rating"] - pool[c[0]]["rating"])
                    changed = True

    sure()
    if gaps:                                                 # then use the usual team boost worked out from those
        gap = max(set(gaps), key=gaps.count)
        for row in rows:
            if not row["name"]:
                c = cands(row, gap)
                if len(c) == 1:
                    row["name"], row["fromShot"] = pool[c[0]]["name"], True
                    used.add(c[0])
        sure()                                               # pairing some can leave only one card for others
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
            if r.get("number") is None:
                r["number"] = next((x.number for x in squad if x.id == r["playerId"]), None)
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
    key = f"lineup:read:{user.id}"          # successful reads only: a failed attempt never uses up anyone's allowance
    if request.method == "GET":
        used = Hit.objects.filter(key=key, ts__gte=time.time() - 86400).count()
        return {"enabled": cfg["enabled"], "left": max(0, cfg["daily_limit"] - used), "limit": cfg["daily_limit"], "names": known_names(t), "perUse": MAX_IMAGES}
    if not cfg["enabled"]:
        raise ApiError(400, "Reading screenshots isn't available. Please contact the platform administrator.")
    image = str(body(request, 1_000_000).get("image") or "")
    if not re.match(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$", image):
        raise ApiError(400, "Choose a screenshot (PNG, JPG or WebP).")
    if over(key, cfg["daily_limit"], 86400):
        raise ApiError(429, f"You've used today's {cfg['daily_limit']} screenshots. Try again tomorrow, or type the lineup.")
    if cfg["daily_total"] and over("lineup:read:all", cfg["daily_total"], 86400):
        raise ApiError(429, "The platform's daily limit for reading screenshots (shared by everyone, set by the platform admins) "
                            "is reached. Try again tomorrow, or type the lineup.")
    if hit(f"lineup:try:{user.id}", 40, 3600):              # many failing attempts in an hour: slow down (these don't use the allowance)
        raise ApiError(429, "Too many tries in the last hour. Wait a little, then try again.")
    raw = read_image(cfg, image)
    rows, formation = clean(raw)
    if rows:                                                 # only a screenshot that was actually read counts
        hit(key, cfg["daily_limit"] + 1, 86400)
        hit("lineup:read:all", 10 ** 6, 86400)              # the platform's total (shown to admins, and used by the optional cap)
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
        number = r.get("number")
        if number in ("", None):
            number = None
        elif isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 999:
            raise ApiError(400, f"{name}'s number must be a whole number from 0 to 999.")
        clean_rows.append((r.get("playerId"), name, pos, rating, starter, number))
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
        for pid, name, pos, rating, starter, number in clean_rows:
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
            if number is not None:
                p.number = number
            p.save()
            taken.add(p.id)
            if p not in squad:
                squad.append(p)
        t.lineup_at = timezone.now()
        formation = str(body(request, 20_000).get("formation") or "")
        try:
            formation = clean_formation(formation)
        except ApiError:
            formation = ""
        if formation and not t.formation and t.formation_style != "fluid":
            t.formation, t.formation_style = formation, "standard"
        t.save(update_fields=["lineup_at", "formation", "formation_style"])
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


def team_like(printed, team_name):
    """How well a name printed on the result screen fits one of our teams (0 none … 2 sure)."""
    a, b = set(engine.name_tokens(printed)), set(engine.name_tokens(team_name))
    if not a or not b:
        return 0
    if a == b:
        return 2
    return 1 if a & b or any(engine._near(x, y) for x in a for y in b) else 0


def like_any(printed, names):
    return max([team_like(printed, n) for n in names if n] or [0])


def clean_result(raw, home_names, away_names, swap=None):
    """The result screen read by the AI, put on our home/away sides: {homeScore, awayScore, events, swapped, sure}.
    `home_names`/`away_names`: every name a side may appear as (its name here, the name it plays as in eFootball).
    `swap`: force the orientation (True: the screen's left team is our away team)."""
    if not isinstance(raw, dict) or not raw.get("isResult") or not isinstance(raw.get("left"), dict) or not isinstance(raw.get("right"), dict):
        return None
    home_names = [home_names] if isinstance(home_names, str) else list(home_names)
    away_names = [away_names] if isinstance(away_names, str) else list(away_names)
    left, right = raw["left"], raw["right"]
    ln, rn = str(left.get("name") or ""), str(right.get("name") or "")
    straight, crossed = like_any(ln, home_names) + like_any(rn, away_names), like_any(ln, away_names) + like_any(rn, home_names)
    swapped = crossed > straight if swap is None else bool(swap)
    sure = swap is not None or straight != crossed
    home, away = (right, left) if swapped else (left, right)

    def score(x):
        try:
            v = int(x.get("score"))
        except (TypeError, ValueError):
            return None
        return v if 0 <= v <= 99 else None

    events = []
    for side, team in (("home", home), ("away", away)):
        for g in (team.get("goals") or [])[:20]:
            if not isinstance(g, dict):
                continue
            name = re.sub(r"\s+", " ", str(g.get("name") or "")).strip()[:80]
            try:
                minute = int(g.get("minute"))
                minute = minute if 0 <= minute <= 150 else None
            except (TypeError, ValueError):
                minute = None
            kind = "own_goal" if g.get("ownGoal") else "penalty_goal" if g.get("penalty") else "goal"
            assist = "" if kind == "own_goal" else re.sub(r"\s+", " ", str(g.get("assist") or "")).strip()[:80]
            events.append({"side": side, "kind": kind, "name": name, "minute": minute, "assist": assist})
    hs, as_ = score(home), score(away)
    if hs is None or as_ is None:
        return None
    return {"homeScore": hs, "awayScore": as_, "events": events, "swapped": swapped, "sure": sure,
            "printed": {"home": str(home.get("name") or "")[:60], "away": str(away.get("name") or "")[:60]},
            "screen": {"left": ln[:60], "right": rn[:60]}}


def clean_events(items):
    """Goals sent with a result: what a team checked before sending (from the AI or typed)."""
    out = []
    for g in (items if isinstance(items, list) else [])[:30]:
        if not isinstance(g, dict) or g.get("side") not in ("home", "away"):
            continue
        kind = g.get("kind") if g.get("kind") in ("goal", "penalty_goal", "own_goal") else "goal"
        m = g.get("minute")
        minute = m if isinstance(m, int) and not isinstance(m, bool) and 0 <= m <= 150 else None
        name = re.sub(r"\s+", " ", str(g.get("name") or "")).strip()[:80]
        assist = "" if kind == "own_goal" else re.sub(r"\s+", " ", str(g.get("assist") or "")).strip()[:80]
        out.append({"side": g["side"], "kind": kind, "name": name, "minute": minute, "assist": assist})
    return out


@endpoint("POST", login_required=True)
def team_game_name(request, user, ip, slug, team_id):
    """The name this team plays as in eFootball (a club like Barcelona, or its own): result screens show it."""
    org, m, t = member_access(user, slug, team_id)
    t.game_name = text(body(request), "gameName", 60)
    t.save(update_fields=["game_name"])
    from .api import log
    log(org, user, f"set {t.name}'s eFootball name to {t.game_name}" if t.game_name else f"cleared {t.name}'s eFootball name")
    return {"ok": True, "gameName": t.game_name}


FORMATION_RE = re.compile(r"^[1-6](-[1-6]){2,4}$")


def clean_formation(v):
    v = re.sub(r"[^0-9-]", "", str(v or "").replace(" ", "-").replace("–", "-")).strip("-")
    if v and (not FORMATION_RE.match(v) or sum(map(int, v.split("-"))) != 10):
        raise ApiError(400, f"{v} isn't a formation: use numbers that add up to 10 outfield players, like 4-3-3.")
    return v


@endpoint("POST", login_required=True)
def team_formation(request, user, ip, slug, team_id):
    """The team's formation: standard (one shape) or fluid (one shape with the ball, one without)."""
    org, m, t = member_access(user, slug, team_id)
    b = body(request)
    style = b.get("style") if b.get("style") in ("standard", "fluid") else "standard"
    t.formation_style = style
    t.formation = clean_formation(b.get("standard"))
    t.formation_attack = clean_formation(b.get("attack")) if style == "fluid" else ""
    t.formation_defence = clean_formation(b.get("defence")) if style == "fluid" else ""
    if style == "fluid" and not (t.formation_attack and t.formation_defence):
        raise ApiError(400, "A fluid formation needs both shapes: with the ball and without it.")
    t.save(update_fields=["formation_style", "formation", "formation_attack", "formation_defence"])
    from .api import log
    log(org, user, f"set {t.name}'s formation")
    return {"ok": True, "formation": {"style": style, "standard": t.formation, "attack": t.formation_attack, "defence": t.formation_defence}}
