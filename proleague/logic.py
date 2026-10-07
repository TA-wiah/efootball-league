"""The Pro League: the platform brings the best-ranked teams of every organization together in divisions.

A season goes: propose (from the rankings, or from last season with promotion and relegation) → invite (each team's
organization accepts, and pays the entry fee if there is one) → start (confirmed teams are split into divisions by
strength, one league per division, fixtures dated by the schedule) → finish (final tables decide next season).
"""
import math
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from competitions import engine
from competitions.models import Competition, Entry, Match, Team
from competitions.rankings import compute, settings_ as ranking_settings
from orgs.models import Membership, Organization
from superadmin import store

MIN_SIZE = 4
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def config():
    s = store.get("proleague")
    names = [n.strip()[:40] for n in str(s.get("divisions") or "").split(",") if n.strip()] or [f"Division {x}" for x in "ABCD"]
    try:
        fee = Decimal(str(s.get("fee") or "0"))
    except InvalidOperation:
        fee = Decimal("0")
    return {"enabled": bool(s.get("enabled")), "name": (s.get("name") or "eFootball Pro League")[:80], "divisions": names[:26],
            "size": min(6, max(MIN_SIZE, int(s.get("size") or 6))), "move": min(3, max(0, int(s.get("move") or 1))),
            "legs": 2 if int(s.get("legs") or 2) == 2 else 1, "country": (s.get("country") or "").strip(), "fee": fee,
            "currency": s.get("currency") or "GHS", "whatsapp": re.sub(r"\D", "", str(s.get("whatsapp") or "")), "org_id": int(s.get("org_id") or 0)}


def pro_org(user=None):
    """The Pro League's own organization (created the first time it's needed, owned by the super admin)."""
    cfg = config()
    org = Organization.objects.filter(id=cfg["org_id"]).first() if cfg["org_id"] else None
    if org:
        return org
    from orgs.api import unique_slug
    org = Organization.objects.create(name=cfg["name"], slug=unique_slug(cfg["name"]), kind="league", created_by=user, in_rankings=True,
                                      description="The platform's league for the best-ranked teams of every organization.")
    if user:
        Membership.objects.create(org=org, user=user, role="owner", last_active=timezone.now())
    s = store.get("proleague")
    s["org_id"] = org.id
    from superadmin.models import PlatformSetting
    PlatformSetting.objects.update_or_create(key="proleague", defaults={"value": s})
    return org


def whatsapp_link(match):
    """wa.me link to the results number, with the message filled in (empty when no number is set)."""
    cfg = config()
    if not cfg["whatsapp"]:
        return ""
    from urllib.parse import quote
    h = match.home.team.name if match.home else "?"
    a = match.away.team.name if match.away else "?"
    text = f"Result for {cfg['name']}: {h} __ - __ {a} ({match.competition.name}, match #{match.id})"
    return f"https://wa.me/{cfg['whatsapp']}?text={quote(text)}"


def is_pro(competition):
    cfg = config()
    return bool(cfg["org_id"]) and competition.org_id == cfg["org_id"]


def ranked_teams(country=""):
    """[(team, position, rating)] of ranked teams, best first (organizations that opted out never appear)."""
    rs = ranking_settings()
    out = []
    for r in compute()["teams"]:
        t = r["team"]
        if r["played"] < rs["min_matches"] or t.suspended or (country and t.org.country.lower() != country.lower()):
            continue
        out.append((t, len(out) + 1, round(r["rating"])))
    return out


def capacity(cfg_or_season):
    names = cfg_or_season["divisions"] if isinstance(cfg_or_season, dict) else cfg_or_season.divisions
    size = cfg_or_season["size"] if isinstance(cfg_or_season, dict) else cfg_or_season.size
    return len(names) * size


def propose(user):
    """Create the next season as a draft: last season's teams with promotion and relegation, then the best-ranked
    teams not yet in it, up to the number of places."""
    cfg = config()
    org = pro_org(user)
    last = Season.objects.filter(org=org).order_by("-number").first()
    if last and last.status in (Season.DRAFT, Season.INVITING, Season.RUNNING):
        raise ValueError(f"{last.name} isn't finished yet.")
    number = (last.number + 1) if last else 1
    places = capacity(cfg)
    picks = []                                                  # (team, reason)
    if last:
        names = last.divisions
        divs = [list(last.teams.filter(division=d, status=SeasonTeam.CONFIRMED).select_related("team").order_by("final_position", "seed"))
                for d in range(len(names))]
        divs = [rows for rows in divs if rows]
        nxt = [[] for _ in divs]                                # next season's divisions, top first
        for d, rows in enumerate(divs):
            move = min(last.move, len(rows) // 2)
            up = rows[:move] if d > 0 else []                   # top of a lower division goes up
            down = rows[len(rows) - move:] if d + 1 < len(divs) and move else []   # bottom of a higher division goes down
            for st in rows:
                if st in up:
                    nxt[d - 1].append((st.team, f"Promoted from {names[d]}"))
                elif st in down:
                    nxt[d + 1].append((st.team, f"Relegated from {names[d]}"))
                else:
                    nxt[d].append((st.team, f"Stayed in {names[d]}"))
        for rows in nxt:                                        # within a division: stayers first, then promoted, then relegated
            rows.sort(key=lambda x: (0 if x[1].startswith("Stayed") else 1 if x[1].startswith("Promoted") else 2))
            picks += rows
    taken = {t.id for t, _ in picks}
    ratings = {t.id: (pos, rating) for t, pos, rating in ranked_teams(cfg["country"])}
    for t, pos, rating in ranked_teams(cfg["country"]):
        if len(picks) >= places:
            break
        if t.id not in taken and t.org_id != org.id:
            picks.append((t, f"Ranked #{pos}"))
            taken.add(t.id)
    picks = picks[:places]
    if len(picks) < MIN_SIZE:
        raise ValueError(f"Only {len(picks)} teams qualify. A division needs at least {MIN_SIZE}: more teams must play {ranking_settings()['min_matches']} public matches first.")
    with transaction.atomic():
        season = Season.objects.create(org=org, number=number, name=f"{cfg['name']} · Season {number}", divisions=cfg["divisions"], size=cfg["size"],
                                       move=cfg["move"], legs=cfg["legs"], country=cfg["country"], fee=cfg["fee"], currency=cfg["currency"])
        for i, (t, reason) in enumerate(picks, 1):
            SeasonTeam.objects.create(season=season, team=t, seed=i, rating=ratings.get(t.id, (0, 1500))[1], reason=reason)
    return season


def add_next(season, count):
    """Propose the next-best ranked teams that aren't in this season yet (to fill places of teams that declined)."""
    have = set(season.teams.values_list("team_id", flat=True))
    seed = (season.teams.order_by("-seed").values_list("seed", flat=True).first() or 0)
    added = []
    for t, pos, rating in ranked_teams(season.country):
        if len(added) >= count:
            break
        if t.id in have or t.org_id == season.org_id:
            continue
        seed += 1
        added.append(SeasonTeam.objects.create(season=season, team=t, seed=seed, rating=rating, reason=f"Ranked #{pos}",
                                               status=SeasonTeam.INVITED if season.status == Season.INVITING else SeasonTeam.PROPOSED))
    return added


def division_sizes(n, names, size):
    """How many teams go in each division: as even as possible, 4 to `size` each, the top divisions filled first."""
    d = min(len(names), math.ceil(n / size)) if n else 0
    while d and n / d < MIN_SIZE:
        d -= 1
    if not d:
        return []
    placed = min(n, d * size)
    base, extra = divmod(placed, d)
    return [base + (1 if i < extra else 0) for i in range(d)]


def start(season, user):
    """Split the confirmed teams into divisions by seed, create one league per division, and generate fixtures."""
    from competitions import api as capi
    teams = list(season.teams.filter(status=SeasonTeam.CONFIRMED).select_related("team").order_by("seed"))
    sizes = division_sizes(len(teams), season.divisions, season.size)
    if not sizes:
        raise ValueError(f"At least {MIN_SIZE} teams must confirm before the season can start.")
    org = season.org
    today = timezone.localdate()
    made = []
    with transaction.atomic():
        i = 0
        for d, n in enumerate(sizes):
            name = f"{season.divisions[d]} · Season {season.number}"
            c = Competition.objects.create(org=org, name=name, slug=engine.unique_slug(Competition, f"{org.slug}-{season.divisions[d]}-s{season.number}", "division"),
                                           season=f"Season {season.number}", kind="league", format="league", visibility="public", status="active",
                                           legs=season.legs, start_date=today + timezone.timedelta(days=2),
                                           description=f"{season.divisions[d]} of the {config()['name']}, season {season.number}. "
                                                       f"{season.move} team{'s' if season.move != 1 else ''} go up and down at the end of the season.")
            entries = []
            for st in teams[i:i + n]:
                entries.append(Entry.objects.create(competition=c, team=st.team))
                st.division, st.competition = d, c
                st.save(update_fields=["division", "competition"])
            i += n
            by_id = {e.id: e for e in entries}
            pairs = sorted(((rnd, k, by_id[h], by_id[a]) for k, (rnd, h, a) in enumerate(engine.round_robin(by_id, season.legs))), key=lambda x: (x[0], x[1]))
            sched = {**engine.SCHEDULE_DEFAULT, **(c.schedule or {})}
            try:
                kickoffs = capi.plan(c, org, len(pairs), capi.first_free_day(c, org, sched, after_existing=False), sched, {})
            except Exception:
                kickoffs = [None] * len(pairs)
            for (rnd, _, home, away), ko in zip(pairs, kickoffs):
                Match.objects.create(competition=c, stage="league", round=rnd, round_name=f"Matchday {rnd}", home=home, away=away, kickoff=ko,
                                     venue=home.team.venue, slug=engine.match_slug(Match, home, away, ko, c.season))
            made.append(c)
        for st in teams[i:]:                                       # more confirmed teams than places: they wait
            st.reason = "No place this season (divisions full)"
            st.save(update_fields=["reason"])
        season.status, season.started = Season.RUNNING, timezone.now()
        season.save(update_fields=["status", "started"])
    return made


def finish(season):
    """Save every team's final position (from the division tables) and close the season."""
    for d in range(len(season.divisions)):
        rows = list(season.teams.filter(division=d).select_related("competition"))
        if not rows:
            continue
        c = rows[0].competition
        if c is None:
            continue
        entries = list(c.entries.select_related("team"))
        table = engine.standings(c, entries, list(c.matches.filter(stage="league", status="finished")))
        pos = {r["team"].id: r["position"] for r in table}
        for st in rows:
            st.final_position = pos.get(st.team_id)
            st.save(update_fields=["final_position"])
        c.status = "completed"
        c.save(update_fields=["status"])
    season.status, season.finished = Season.FINISHED, timezone.now()
    season.save(update_fields=["status", "finished"])


from .models import Season, SeasonTeam  # noqa: E402  (models import logic-free helpers above)
