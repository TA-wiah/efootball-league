"""The knockout plan: who meets whom after the group stage, set before a ball is kicked.

Each planned knockout match says where its teams come from:
    "G:A:1"               1st in group A
    "W:2:1:Semi-final"    winner of knockout round 2, tie 1 (the name is only for showing it)
Teams are filled in automatically as groups finish and ties are won, until that match has started.
"""
import random

_rng = random.SystemRandom()
ROUND_NAMES = {2: "Final", 4: "Semi-finals", 8: "Quarter-finals", 16: "Round of 16", 32: "Round of 32", 64: "Round of 64"}
SINGULAR = {"Final": "Final", "Semi-finals": "Semi-final", "Quarter-finals": "Quarter-final"}
MODES = {"cross": "Group winners play runners-up from another group (Champions League style)",
         "random": "Random draw: group winners play lower-placed teams from other groups"}


def ordinal(k):
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def label(src):
    """What a planned slot shows until its team is known, e.g. "Group A winner" or "Winner of Semi-final 1"."""
    if not src:
        return ""
    parts = src.split(":")
    if parts[0] == "G":
        k = int(parts[2])
        return f"Group {parts[1]} " + {1: "winner", 2: "runner-up"}.get(k, ordinal(k))
    if parts[0] == "W":
        name = parts[3] if len(parts) > 3 and parts[3] else f"round {parts[1]}"
        return f"Winner of {name} {parts[2]}"
    return ""


def seed_order(n):
    """Bracket positions for seeds 1..n so the top seeds only meet late: 4 → [1, 4, 2, 3]."""
    order = [1, 2]
    while len(order) < n:
        size = len(order) * 2
        order = [x for s in order for x in (s, size + 1 - s)]
    return order


def first_round(groups, q, mode):
    """The first knockout round as [(home source, away source)], in bracket order (tie 1 and 2 meet next, and so on)."""
    n = len(groups) * q
    if n < 2 or n & (n - 1):
        raise ValueError(f"A knockout needs 2, 4, 8, 16 or 32 teams. {len(groups)} group{'s' if len(groups) != 1 else ''} × "
                         f"{q} going through = {n}. Change “Qualify from each group” in Settings.")
    g = lambda grp, k: f"G:{grp}:{k}"
    if len(groups) == 1:                                   # one group: 1st v last, 2nd v 2nd-last…
        order = seed_order(q)
        return [(g(groups[0], order[i]), g(groups[0], order[i + 1])) for i in range(0, q, 2)]
    if mode == "cross" and len(groups) % 2 == 0:
        top, bottom = [], []
        for i in range(0, len(groups), 2):
            x, y = groups[i], groups[i + 1]
            for k in range(1, q + 1):
                a, b = (x, k), (y, q + 1 - k)
                home, away = (a, b) if a[1] <= b[1] else (b, a)            # the better-placed team plays at home
                (top if k % 2 else bottom).append((g(*home), g(*away)))   # same-group teams only meet in the final
        return top + bottom
    # random draw (also used when the groups can't be paired up evenly)
    if q == 1:
        pool = [(grp, 1) for grp in groups]
        _rng.shuffle(pool)
        return [(g(*pool[i]), g(*pool[i + 1])) for i in range(0, len(pool), 2)]
    tops = [(grp, k) for grp in groups for k in range(1, q // 2 + 1)]
    rest = [(grp, k) for grp in groups for k in range(q // 2 + 1, q + 1)]
    for _ in range(500):
        _rng.shuffle(rest)
        if all(t[0] != r[0] for t, r in zip(tops, rest)):
            break
    ties = [(g(*t), g(*r)) for t, r in zip(tops, rest)]
    _rng.shuffle(ties)
    return ties


def plan_rounds(first, legs):
    """[(round number, round name, [(home source, away source)], legs)] from the first round to the final."""
    rounds, ties, r = [], first, 1
    while True:
        size = len(ties) * 2
        name = ROUND_NAMES.get(size, f"Round of {size}")
        rounds.append((r, name, ties, 1 if size == 2 else legs))
        if len(ties) == 1:
            return rounds
        one = SINGULAR.get(name, name)
        ties = [(f"W:{r}:{2 * i + 1}:{one}", f"W:{r}:{2 * i + 2}:{one}") for i in range(len(ties) // 2)]
        r += 1


def tie_winner(legs):
    """The entry id that won a tie (one or two legs: aggregate, then penalties in the last leg), or None if not decided."""
    legs = sorted(legs, key=lambda m: m.leg)
    if not legs or any(m.status != "finished" or m.home_score is None or m.away_score is None for m in legs):
        return None
    a, b = legs[0].home_id, legs[0].away_id
    ga = gb = 0
    for m in legs:
        if m.home_id == a:
            ga, gb = ga + m.home_score, gb + m.away_score
        else:
            ga, gb = ga + m.away_score, gb + m.home_score
    if ga != gb:
        return a if ga > gb else b
    last = legs[-1]
    if last.home_pens is None or last.away_pens is None or last.home_pens == last.away_pens:
        return None
    pa, pb = (last.home_pens, last.away_pens) if last.home_id == a else (last.away_pens, last.home_pens)
    return a if pa > pb else b


def resolve(c):
    """Fill in planned knockout matches whose teams are now known (and clear ones whose source changed back).
    Matches that have started are never touched. Returns how many matches changed."""
    from . import engine
    ko = list(c.matches.filter(stage="knockout").order_by("round", "slot", "leg", "id"))
    planned = [m for m in ko if m.home_from or m.away_from]
    if not planned:
        return 0
    league = list(c.matches.filter(stage="league"))
    entries = list(c.entries.select_related("team"))
    tables = {}
    for grp in {e.group for e in entries}:
        games = [m for m in league if m.group == grp]
        if games and all(m.status in ("finished", "cancelled") for m in games):
            rows = engine.standings(c, [e for e in entries if e.group == grp], [m for m in games if m.status == "finished"])
            tables[grp] = [r["entryId"] for r in rows]
    ties = {}
    for m in ko:
        ties.setdefault((m.round, m.slot), []).append(m)

    def source(src):
        parts = src.split(":")
        if parts[0] == "G":
            table, k = tables.get(parts[1]), int(parts[2])
            return table[k - 1] if table and len(table) >= k else None
        if parts[0] == "W":
            return tie_winner(ties.get((int(parts[1]), int(parts[2])), []))
        return None

    changed = 0
    for m in planned:                                       # earlier rounds first, so winners flow forward
        if m.status not in ("scheduled", "postponed") or m.home_score is not None:
            continue
        home = source(m.home_from) if m.home_from else m.home_id
        away = source(m.away_from) if m.away_from else m.away_id
        if (home, away) != (m.home_id, m.away_id):
            m.home_id, m.away_id = home, away
            m.save(update_fields=["home", "away"])
            changed += 1
    return changed
