"""Platform rankings: every team and player across all organizations, from finished matches in public competitions.

Teams are rated with Elo (the system used for chess and in the World Football Elo ratings):
- everyone starts at 1500;
- after each match, points move from the loser to the winner. Beating a stronger team moves more points than beating
  a weaker one, and a bigger winning margin counts a little more (×1.5 for two goals, more for three or more);
- a match decided on penalties counts as a draw;
- walkovers and forfeits don't count (a win after the opponent's connection dropped does);
- matches are taken in the order they were played;
- friendlies count half by default (a super admin can make them count fully or not at all).
Awards (Rankings → Awards) come from the current year's competitive matches; a super admin can rename them.
Organizations can keep their teams out, and a super admin can switch rankings off entirely.
"""
from django.db.models import Max, Q
from django.utils import timezone

from superadmin import store

from .models import Match, MatchEvent

START, K = 1500.0, 32.0
CARD_POINTS = {"yellow": 1, "second_yellow": 2, "red": 3}      # for the Fair Play award
AWARDS = [("poty", "Most goals and assists together"), ("boot", "Most goals"), ("assists", "Most assists"),
          ("defence", "Fewest goals conceded per match"), ("team", "Highest rating"), ("fairplay", "Fewest cards per match")]
_cache = {"key": None, "data": None}


def settings_():
    s = store.get("rankings")
    w = str(s.get("friendly_weight", "0.5"))
    return {"enabled": bool(s.get("enabled")), "min_matches": max(1, int(s.get("min_matches") or 3)),
            "friendly_weight": float(w) if w in ("0", "0.5", "1") else 0.5}


def _played():
    return (Match.objects.select_related("competition__org", "home__team__org", "away__team__org")
            .filter(status="finished", home_score__isnull=False, away_score__isnull=False, home__isnull=False, away__isnull=False,
                    competition__visibility="public", competition__suspended=False, competition__org__status="active",
                    competition__org__in_rankings=True)
            .exclude(Q(home__team__suspended=True) | Q(away__team__suspended=True))
            .exclude(decided__in=["walkover", "no_show", "forfeit", "abandoned"]))  # not played out: doesn't move ratings


def margin_factor(gd):
    gd = abs(gd)
    return 1.0 if gd <= 1 else 1.5 if gd == 2 else (11 + gd) / 8


def compute():
    """{teams: [...], players: [...]} in ranking order (cached until a match changes)."""
    qs = _played()
    weight = settings_()["friendly_weight"]                 # friendlies move ratings less (or not at all)
    key = (qs.count(), qs.aggregate(m=Max("updated"))["m"], MatchEvent.objects.filter(match__in=qs).count(), weight, timezone.localdate().year)
    if _cache["key"] == key:
        return _cache["data"]
    teams = {}

    def row(t):
        if t.id not in teams:
            teams[t.id] = {"team": t, "rating": START, "played": 0, "won": 0, "drawn": 0, "lost": 0, "gf": 0, "ga": 0, "form": [], "last": None}
        return teams[t.id]

    games = sorted(qs, key=lambda m: (m.finished_at or m.kickoff or m.updated, m.id))
    for m in games:
        h, a = row(m.home.team), row(m.away.team)
        hs, as_ = m.home_score, m.away_score
        if hs == as_ and m.home_pens is not None and m.away_pens is not None:
            score_h = 0.5                                    # decided on penalties: a draw for the rating
        else:
            score_h = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        expected_h = 1 / (1 + 10 ** ((a["rating"] - h["rating"]) / 400))
        change = K * (weight if m.competition.kind == "friendly" else 1) * margin_factor(hs - as_) * (score_h - expected_h)
        h["rating"] += change
        a["rating"] -= change
        for r, gf, ga in ((h, hs, as_), (a, as_, hs)):
            r["played"] += 1
            r["gf"] += gf
            r["ga"] += ga
            res = "W" if gf > ga else "L" if gf < ga else "D"
            r[{"W": "won", "L": "lost", "D": "drawn"}[res]] += 1
            r["form"] = (r["form"] + [res])[-5:]
            r["last"] = m.kickoff or m.finished_at
    ranked = sorted(teams.values(), key=lambda r: (-r["rating"], -r["played"], r["team"].name.lower()))
    events = list(MatchEvent.objects.select_related("player", "assist", "match__competition", "match__home__team__org", "match__away__team__org")
                  .filter(match__in=qs))
    from .engine import scorers
    players = [p for p in scorers(events) if p["goals"] or p["assists"]]
    # Player of the Year: this calendar year's competitive matches (friendlies don't count)
    year = timezone.localdate().year
    when = lambda m: m.kickoff or m.finished_at
    this_year = [e for e in events if e.match.competition.kind != "friendly" and when(e.match) and timezone.localtime(when(e.match)).year == year]
    # this year's competitive matches per team: goals conceded, clean sheets and cards (for the awards)
    season = {}
    for m in games:
        if m.competition.kind == "friendly" or not when(m) or timezone.localtime(when(m)).year != year:
            continue
        for e, ga in ((m.home, m.away_score), (m.away, m.home_score)):
            r = season.setdefault(e.team_id, {"team": e.team, "played": 0, "ga": 0, "cs": 0, "cards": 0})
            r["played"] += 1
            r["ga"] += ga
            r["cs"] += ga == 0
    for e in this_year:
        if e.kind in CARD_POINTS:
            entry = e.match.home if e.side == "home" else e.match.away
            if entry and entry.team_id in season:
                season[entry.team_id]["cards"] += CARD_POINTS[e.kind]
    data = {"teams": ranked, "players": players, "matches": len(games), "year": year, "season": season,
            "year_players": [p for p in scorers(this_year) if p["goals"] or p["assists"]]}
    _cache.update(key=key, data=data)
    return data


def awards(where, min_matches):
    """This year's awards (friendlies don't count), for teams whose organization passes `where` (the area filter)."""
    s, data = store.get("rankings"), compute()
    name = lambda k: s.get(f"award_{k}") or store.DEFAULTS["rankings"][f"award_{k}"]
    players = [p for p in data["year_players"] if p["team"] and where(p["team"].org)]
    teams = [r for r in data["season"].values() if r["played"] >= min_matches and where(r["team"].org)]
    out = []

    def player(key, rows, stat):
        if rows:
            p = rows[0]
            out.append({"key": key, "name": name(key), "who": p["name"], "team": p["team"], "stat": stat(p)})

    def team(key, rows, stat):
        if rows:
            out.append({"key": key, "name": name(key), "who": rows[0]["team"].name, "team": rows[0]["team"], "stat": stat(rows[0])})

    per = lambda r, k: r[k] / r["played"]
    plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    player("poty", sorted(players, key=lambda p: (-(p["goals"] + p["assists"]), -p["goals"], p["name"].lower())),
           lambda p: f"{plural(p['goals'], 'goal')} · {plural(p['assists'], 'assist')}")
    player("boot", sorted([p for p in players if p["goals"]], key=lambda p: (-p["goals"], -p["assists"], p["name"].lower())), lambda p: plural(p["goals"], "goal"))
    player("assists", sorted([p for p in players if p["assists"]], key=lambda p: (-p["assists"], -p["goals"], p["name"].lower())), lambda p: plural(p["assists"], "assist"))
    team("defence", sorted(teams, key=lambda r: (per(r, "ga"), -r["cs"], r["team"].name.lower())),
         lambda r: f"{r['ga']} conceded in {plural(r['played'], 'match')} · {plural(r['cs'], 'clean sheet')}")
    rated = {r["team"].id: r["rating"] for r in data["teams"]}
    team("team", sorted([r for r in teams if r["team"].id in rated], key=lambda r: (-rated[r["team"].id], r["team"].name.lower())),
         lambda r: f"Rating {round(rated[r['team'].id])}")
    team("fairplay", sorted(teams, key=lambda r: (per(r, "cards"), r["team"].name.lower())),
         lambda r: f"{plural(r['cards'], 'card point')} in {plural(r['played'], 'match')}")
    how = dict(AWARDS)
    for a in out:
        a["how"] = how[a["key"]]
    return {"year": data["year"], "awards": out}


def team_rank(team_id):
    """(position, rating) for a team that qualifies, else None."""
    s = settings_()
    if not s["enabled"]:
        return None
    pos = 0
    for r in compute()["teams"]:
        if r["played"] >= s["min_matches"]:
            pos += 1
            if r["team"].id == team_id:
                return pos, round(r["rating"])
    return None
