"""Position guesses for players nobody has given a position yet, from real data only, and only when it's clear:

1. The same player on other teams' squads: eFootball players are real footballers, so when organizers elsewhere set
   "Virgil van Dijk" as CB, he's a CB. At least two other teams must agree (two thirds or more of them); if they
   only agree on the line (RWF / CF), the guess is just the line (FWD).
2. The player's own goals and assists for this team: lots of goals and few assists is a forward; more assists than
   goals is a midfielder. Too few goals and assists, or a mix, and there's no guess. Defenders and goalkeepers are
   never guessed from goals.

A guess is always shown as "auto"; a position someone sets always wins.
"""
from collections import Counter
from functools import reduce
from operator import or_

from django.db.models import Q

from .engine import name_tokens
from .models import LINE

GENERIC = {"DF", "MF", "FW"}
SHORT = {"GK": "GK", "DF": "DEF", "MF": "MID", "FW": "FWD"}
MIN_TEAMS = 2                      # other teams that must agree
FW_GOALS, MF_ASSISTS = 5, 4        # goals / assists needed before guessing from stats


def key(name):
    """"Messi Lionel" and "Lionel Messi" are the same key."""
    return " ".join(sorted(name_tokens(name)))


def label(pos):
    return SHORT.get(pos, pos)


def elsewhere(names, exclude_team=None):
    """{name key: Counter(position: number of teams)} from every squad on the platform with that position set."""
    from .models import Player
    keys = {key(n) for n in names if key(n)}
    if not keys:
        return {}
    words = {max(k.split(), key=len) for k in keys}
    qs = Player.objects.exclude(position="").filter(reduce(or_, (Q(name__icontains=w) for w in words)))
    if exclude_team:
        qs = qs.exclude(team_id=exclude_team)
    seen, out = set(), {}
    for team_id, name, pos in qs.values_list("team_id", "name", "position")[:5000]:
        k = key(name)
        if k in keys and (team_id, k, pos) not in seen:
            seen.add((team_id, k, pos))
            out.setdefault(k, Counter())[pos] += 1
    return out


def from_others(counts):
    if not counts:
        return None
    total = sum(counts.values())
    exact = Counter({p: n for p, n in counts.items() if p not in GENERIC})
    if exact:
        top, n = exact.most_common(1)[0]
        if n >= MIN_TEAMS and n * 3 >= total * 2:
            return {"pos": top, "label": top, "why": f"set as {top} by {n} other teams"}
    lines = Counter()
    for p, n in counts.items():
        lines[LINE.get(p, "")] += n
    top, n = lines.most_common(1)[0]
    if top and n >= MIN_TEAMS and n * 3 >= total * 2:
        return {"pos": top, "label": label(top), "why": f"a {label(top).lower()} on {n} other teams"}
    return None


def from_stats(goals, assists):
    plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    why = f"{plural(goals, 'goal')}, {plural(assists, 'assist')}"
    if goals >= FW_GOALS and goals >= 2 * assists:
        return {"pos": "FW", "label": "FWD", "why": why}
    if assists >= MF_ASSISTS and assists > goals:
        return {"pos": "MF", "label": "MID", "why": why}
    return None


def guess(name, others, goals=0, assists=0):
    g = from_others(others.get(key(name)))
    return g or from_stats(goals, assists)


def for_team(team):
    """{player id: guess} for this team's squad players with no position set."""
    from .api import team_events
    from .engine import same_player, scorers
    blank = [p for p in team.players.all() if not p.position]
    if not blank:
        return {}
    others = elsewhere([p.name for p in blank], exclude_team=team.id)
    events = team_events(team).select_related("player", "assist", "match__home__team", "match__away__team")
    stats = [r for r in scorers(events) if r["team"] and r["team"].id == team.id]
    out = {}
    for p in blank:
        row = next((r for r in stats if r["playerId"] == p.id), None) or next((r for r in stats if not r["playerId"] and same_player(r["name"], p.name)), None)
        g = guess(p.name, others, row["goals"] if row else 0, row["assists"] if row else 0)
        if g:
            out[p.id] = g
    return out
