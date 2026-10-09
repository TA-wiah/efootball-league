"""The knockout plan: who meets whom after the group stage, set before a ball is kicked.

Each planned knockout match says where its teams come from:
    "G:A:1"               1st in group A
    "W:2:1:Semi-final"    winner of knockout round 2, tie 1 (the name is only for showing it)
Teams are filled in automatically as groups finish and ties are won, until that match has started.
"""
import random

_rng = random.SystemRandom()
OUT = "out"            # a place nobody fills: a bye, or a 3rd place without enough points
ROUND_NAMES = {2: "Final", 4: "Semi-finals", 8: "Quarter-finals", 16: "Round of 16", 32: "Round of 32", 64: "Round of 64"}
SINGULAR = {"Final": "Final", "Semi-finals": "Semi-final", "Quarter-finals": "Quarter-final"}
MODES = {"ranked": "After the groups: ranked by results; places are topped up with the best of the rest so everyone plays",
         "cross": "Group winners play runners-up from another group (Champions League style)",
         "random": "Random draw: group winners play lower-placed teams from other groups"}


def ordinal(k):
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def label(src):
    """What a planned slot shows until its team is known, e.g. "Group A winner" or "Winner of Semi-final 1"."""
    if not src:
        return ""
    parts = src.split(":")
    if parts[0] == "BYE":
        return "Bye"
    if parts[0] == "S":
        return f"Seed {parts[1]} (by group results)"
    if parts[0] == "T":
        return f"Best {ordinal(int(parts[2]))}-placed team {parts[1]}"
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


def first_round(groups, q, mode, thirds=0):
    """The first knockout round as [(home source, away source)], in bracket order (tie 1 and 2 meet next, and so on).
    "ranked": the places are filled once every group has finished, best record first ("S:1", "S:2"…). The bracket is
    rounded up to 4, 8, 16… and topped up with the best teams that didn't qualify, so everyone plays (no byes)."""
    n = len(groups) * q + thirds
    if n < 2:
        raise ValueError("A knockout needs at least 2 teams. Change “Qualify from each group” in Settings.")
    if mode == "ranked":
        size = 1
        while size < n:
            size *= 2
        return build([(f"S:{i}", None, i) for i in range(1, size + 1)])
    g = lambda grp, k: f"G:{grp}:{k}"
    if thirds or n & (n - 1):
        return build([(g(grp, k), grp, k) for k in range(1, q + 1) for grp in groups] +
                     [(f"T:{i}:{q + 1}", None, q + 1) for i in range(1, thirds + 1)])
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


def build(seeds):
    """A seeded first round from [(source, group or None, rank)], best first. The bracket is rounded up to 2, 4, 8, 16…
    and the best seeds get byes ("BYE"). Teams from the same group are kept apart in the first round where possible."""
    size = 1
    while size < len(seeds):
        size *= 2
    order = seed_order(size)
    ties = [[seeds[a - 1] if a <= len(seeds) else None, seeds[b - 1] if b <= len(seeds) else None] for a, b in zip(order[::2], order[1::2])]
    for t in ties:                                                         # same group: swap with an opponent of the same rank
        if t[0] and t[1] and t[0][1] and t[0][1] == t[1][1]:
            for u in ties:
                if u is not t and u[0] and u[1] and u[1][2] == t[1][2] and u[0][1] != t[1][1] and t[0][1] != u[1][1]:
                    t[1], u[1] = u[1], t[1]
                    break
    return [(a[0] if a else "BYE", b[0] if b else "BYE") for a, b in ties]


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
    tables, rows_by_group = {}, {}
    groups = {e.group for e in entries}
    for grp in groups:
        games = [m for m in league if m.group == grp]
        if games and all(m.status in ("finished", "cancelled") for m in games):
            rows = engine.standings(c, [e for e in entries if e.group == grp], [m for m in games if m.status == "finished"])
            rows_by_group[grp] = rows
            tables[grp] = [r["entryId"] if engine.goes_through(c, r["position"], r["points"]) else OUT for r in rows]
    done = bool(groups) and len(rows_by_group) == len(groups)      # rankings across groups wait for every group
    wild = engine.best_next(c, rows_by_group) if done else None
    seedmap = None
    if done and any(m.home_from.startswith("S:") for m in planned if m.home_from):
        slots = max(int(src[2:]) for m in planned for src in (m.home_from, m.away_from) if (src or "").startswith("S:"))
        seeds = engine.seeding(c, rows_by_group, fill=slots)
        seedmap = {i + 1: s for i, s in enumerate(seeds)}
        first = min(m.round for m in planned)
        pairs = {tuple(sorted((int(m.home_from[2:]), int(m.away_from[2:]))))
                 for m in planned if m.round == first and (m.home_from or "").startswith("S:") and (m.away_from or "").startswith("S:")}
        pairs = sorted(pairs)
        for a, b in pairs:                                             # keep teams from the same group apart
            if a in seedmap and b in seedmap and seedmap[a][1] == seedmap[b][1]:
                for x, y in pairs:
                    if (x, y) != (a, b) and x in seedmap and y in seedmap and seedmap[y][1] != seedmap[a][1] and seedmap[b][1] != seedmap[x][1]:
                        seedmap[b], seedmap[y] = seedmap[y], seedmap[b]
                        break
    ties = {}
    for m in ko:
        ties.setdefault((m.round, m.slot), []).append(m)

    def source(src):
        parts = src.split(":")
        if parts[0] == "BYE":
            return OUT
        if parts[0] == "S":
            return None if seedmap is None else (seedmap[int(parts[1])][0] if int(parts[1]) in seedmap else OUT)
        if parts[0] == "T":
            return None if wild is None else (wild[int(parts[1]) - 1] if len(wild) >= int(parts[1]) else OUT)
        if parts[0] == "G":
            table, k = tables.get(parts[1]), int(parts[2])
            return table[k - 1] if table and len(table) >= k else None
        if parts[0] == "W":
            legs = ties.get((int(parts[1]), int(parts[2])), [])
            if legs and all(x.decided == "bye" for x in legs):    # nobody to play: whoever is there goes through
                return legs[0].home_id or legs[0].away_id or OUT
            return tie_winner(legs)
        return None

    names = {e.id: e.team.name for e in entries}
    changed = 0
    for m in planned:                                       # earlier rounds first, so winners flow forward
        if m.decided != "bye" and (m.status not in ("scheduled", "postponed") or m.home_score is not None):
            continue
        home = source(m.home_from) if m.home_from else m.home_id
        away = source(m.away_from) if m.away_from else m.away_id
        bye = OUT in (home, away) and (home not in (None,) and away not in (None,))
        home, away = (None if home == OUT else home), (None if away == OUT else away)
        if bye:
            who = names.get(home or away)
            state = ("cancelled", "bye", f"Bye: {who} goes straight through." if who else "Bye: neither place was filled.")
        else:
            state = ("scheduled", "", "") if m.decided == "bye" else (m.status, m.decided, m.notes)
        if (home, away, *state) != (m.home_id, m.away_id, m.status, m.decided, m.notes):
            m.home_id, m.away_id = home, away
            m.status, m.decided, m.notes = state
            m.save(update_fields=["home", "away", "status", "decided", "notes"])
            changed += 1
    return changed
