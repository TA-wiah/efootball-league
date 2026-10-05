"""Competition logic with no HTTP in it: fixtures, standings, tie-breakers, top scorers, slugs and logos."""
import base64
import random
from datetime import timedelta

from django.utils.text import slugify

_rng = random.SystemRandom()   # fixture order and draws come from the OS random generator: nobody can predict them

CRITERIA = {
    "points": "Points",
    "goal_difference": "Goal difference",
    "goals_for": "Goals scored",
    "goals_against": "Fewest goals conceded",
    "wins": "Wins",
    "away_goals": "Away goals scored",
    "head_to_head_points": "Head-to-head points",
    "head_to_head_goal_difference": "Head-to-head goal difference",
    "head_to_head_goals_for": "Head-to-head goals scored",
}
DEFAULT_TIEBREAKERS = ["points", "goal_difference", "goals_for", "head_to_head_points", "wins"]


def clean_tiebreakers(keys):
    """Valid, unique keys, always starting with points."""
    out = ["points"]
    for k in keys if isinstance(keys, list) else []:
        if k in CRITERIA and k not in out:
            out.append(k)
    return out


# ---------- slugs ----------
def unique_slug(model, text, fallback, limit=60, exclude_id=None):
    base = slugify(text)[:limit].strip("-") or fallback
    slug, n = base, 2
    qs = model.objects.all() if exclude_id is None else model.objects.exclude(id=exclude_id)
    while qs.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def match_slug(model, home, away, kickoff, season):
    when = kickoff.strftime("%Y-%m-%d") if kickoff else (season or "")
    text = f"{home.team.name} vs {away.team.name} {when}" if home and away else f"match {when}"
    return unique_slug(model, text, "match", limit=120)


# ---------- fixtures ----------
def round_robin(ids, legs=1, shuffle=True):
    """[(round, home_id, away_id), …] with everyone meeting everyone `legs` times (circle method)."""
    ids = list(ids)
    if shuffle:
        _rng.shuffle(ids)
    if len(ids) % 2:
        ids.append(None)                       # a "bye" for odd numbers of teams
    n = len(ids)
    rounds, arr = [], ids[:]
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))   # alternate home and away
        rounds.append(pairs)
        arr = [arr[0], arr[-1]] + arr[1:-1]
    out = [(r + 1, h, a) for r, pairs in enumerate(rounds) for h, a in pairs]
    if legs == 2:
        k = len(rounds)
        out += [(r + 1 + k, a, h) for r, pairs in enumerate(rounds) for h, a in pairs]
    return out


SCHEDULE_DEFAULT = {"perDay": 5, "everyDays": 1, "time": "18:00", "gap": 30}
SCHEDULE_LIMITS = {"perDay": (2, 20), "everyDays": (1, 5), "gap": (10, 240)}


def clean_schedule(data):
    """{perDay: 2–20 matches a day, everyDays: 1–5 days between match days, time: "HH:MM" first kick-off, gap: minutes between matches}."""
    if not isinstance(data, dict):
        raise ValueError("Send the schedule as an object.")
    out = {}
    for k, v in data.items():
        if k in SCHEDULE_LIMITS:
            lo, hi = SCHEDULE_LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError({"perDay": f"Matches a day must be from {lo} to {hi}.", "everyDays": f"Days between match days must be from {lo} to {hi}.",
                                  "gap": f"Minutes between matches must be from {lo} to {hi}."}[k])
            out[k] = v
        elif k == "time":
            if not isinstance(v, str) or not __import__("re").fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                raise ValueError("The first kick-off time must look like 18:00.")
            out[k] = v
        else:
            raise ValueError(f"Unknown schedule setting: {k}")
    return out


def plan_kickoffs(count, first_day, sched, tz):
    """Kick-off times for `count` matches in order: `perDay` matches on each match day, `gap` minutes apart from `time`,
    with a match day every `everyDays` days starting on `first_day`."""
    import zoneinfo
    from datetime import datetime, time as dtime
    s = {**SCHEDULE_DEFAULT, **(sched or {})}
    hh, mm = map(int, s["time"].split(":"))
    zone = zoneinfo.ZoneInfo(tz)
    out = []
    for i in range(count):
        day = first_day + timedelta(days=(i // s["perDay"]) * s["everyDays"])
        at = datetime.combine(day, dtime(hh, mm), tzinfo=zone) + timedelta(minutes=(i % s["perDay"]) * s["gap"])
        out.append(at)
    return out


def match_days_needed(count, sched):
    s = {**SCHEDULE_DEFAULT, **(sched or {})}
    days = -(-count // s["perDay"])
    return days, (days - 1) * s["everyDays"] if days else 0       # (match days, calendar days after the first)


def kickoff_for(start, round_no, days_between):
    return start + timedelta(days=days_between * (round_no - 1)) if start else None


def draw_pairs(ids):
    ids = list(ids)
    _rng.shuffle(ids)
    return [(ids[i], ids[i + 1]) for i in range(0, len(ids) - 1, 2)]


# ---------- standings ----------
def _blank(entry):
    return {"entry": entry, "p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "ag": 0, "pts": 0, "form": []}


def _apply(rows, m, pw, pd, pl):
    h, a = rows.get(m.home_id), rows.get(m.away_id)
    if h is None or a is None:
        return
    hs, as_ = m.home_score, m.away_score
    for me, gf, ga, away in ((h, hs, as_, False), (a, as_, hs, True)):
        me["p"] += 1
        me["gf"] += gf
        me["ga"] += ga
        if away:
            me["ag"] += gf
        res = "W" if gf > ga else "L" if gf < ga else "D"
        me[{"W": "w", "D": "d", "L": "l"}[res]] += 1
        me["pts"] += {"W": pw, "D": pd, "L": pl}[res]
        me["form"].append(res)


def _stat(row, key):
    if key == "points":
        return row["pts"]
    if key == "goal_difference":
        return row["gf"] - row["ga"]
    if key == "goals_for":
        return row["gf"]
    if key == "goals_against":
        return -row["ga"]                       # fewer is better
    if key == "wins":
        return row["w"]
    if key == "away_goals":
        return row["ag"]
    raise KeyError(key)


def _rank(rows, criteria, matches, pts_cfg):
    if len(rows) <= 1 or not criteria:
        return sorted(rows, key=lambda r: r["entry"].team.name.lower())
    key, rest = criteria[0], criteria[1:]
    if key.startswith("head_to_head_"):
        ids = {r["entry"].id for r in rows}
        mini = {i: _blank(None) for i in ids}
        for m in matches:
            if m.home_id in ids and m.away_id in ids:
                _apply(mini, m, *pts_cfg)
        sub = key[len("head_to_head_"):]
        value = lambda r: _stat(mini[r["entry"].id], sub)   # noqa: E731
    else:
        value = lambda r: _stat(r, key)   # noqa: E731
    out = []
    for v in sorted({value(r) for r in rows}, reverse=True):
        out += _rank([r for r in rows if value(r) == v], rest, matches, pts_cfg)
    return out


def standings(comp, entries, matches):
    """Table rows (already sorted) for these entries, from these finished league matches."""
    pts_cfg = (comp.points_win, comp.points_draw, comp.points_loss)
    rows = {e.id: _blank(e) for e in entries}
    played = [m for m in matches if m.home_score is not None and m.away_score is not None and m.home_id in rows and m.away_id in rows]
    played.sort(key=lambda m: (m.kickoff is None, m.kickoff, m.round, m.id))
    for m in played:
        _apply(rows, m, *pts_cfg)
    for r in rows.values():
        r["pts"] += r["entry"].points_adjustment
    ranked = _rank(list(rows.values()), clean_tiebreakers(comp.tiebreakers or DEFAULT_TIEBREAKERS), played, pts_cfg)
    return [{"position": i + 1, "entryId": r["entry"].id, "team": r["entry"].team, "played": r["p"], "won": r["w"], "drawn": r["d"],
             "lost": r["l"], "goalsFor": r["gf"], "goalsAgainst": r["ga"], "goalDifference": r["gf"] - r["ga"], "points": r["pts"],
             "adjustment": r["entry"].points_adjustment, "form": r["form"][-5:]} for i, r in enumerate(ranked)]


# ---------- top scorers ----------
def scorers(events):
    """Goals and assists per player from match events (own goals don't count for the scorer)."""
    table = {}

    def row(player, name, team):
        key = ("id", player.id) if player else ("name", (name or "").strip().lower(), team.id if team else None)
        if key not in table:
            table[key] = {"name": player.name if player else name.strip(), "playerId": player.id if player else None,
                          "team": team, "goals": 0, "assists": 0}
        return table[key]

    for e in events:
        entry = e.match.home if e.side == "home" else e.match.away
        team = entry.team if entry else None
        if e.kind in ("goal", "penalty_goal") and (e.player or e.player_name.strip()):
            row(e.player, e.player_name, team)["goals"] += 1
        if e.kind in ("goal", "penalty_goal") and (e.assist or e.assist_name.strip()):
            row(e.assist, e.assist_name, team)["assists"] += 1
    return sorted(table.values(), key=lambda r: (-r["goals"], -r["assists"], r["name"].lower()))


# ---------- logos ----------
SIGNATURES = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")]
MAX_LOGO = 256 * 1024


def decode_logo(data_url):
    """Accept a base64 data URL of a PNG, JPEG, GIF or WebP up to 256 KB. Returns (bytes, content_type) or raises ValueError.
    SVG is refused on purpose: it can carry scripts."""
    if not isinstance(data_url, str) or not data_url.startswith("data:image/") or ";base64," not in data_url:
        raise ValueError("Upload a PNG, JPG, GIF or WebP image.")
    try:
        raw = base64.b64decode(data_url.split(";base64,", 1)[1], validate=True)
    except ValueError:
        raise ValueError("That image file is damaged.") from None
    if len(raw) > MAX_LOGO:
        raise ValueError("The logo must be smaller than 256 KB.")
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return raw, "image/webp"
    for sig, ctype in SIGNATURES:
        if raw.startswith(sig):
            return raw, ctype
    raise ValueError("Upload a PNG, JPG, GIF or WebP image.")
