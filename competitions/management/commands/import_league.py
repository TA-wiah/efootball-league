"""Copy the original single league (the page at /) into an organization as a normal competition.

    python manage.py import_league kasoa-community-league
    python manage.py import_league kasoa-community-league --name "Champions League" --season 2026
    python manage.py import_league robotics-league --into robotics-championship   (fill an existing, empty competition)
    python manage.py import_league apass-squad --into robotics-championship --file competitions/data/original_league.json

Where the league comes from: --file if given; otherwise this database's original league; otherwise the copy saved in
competitions/data/original_league.json (exported from the computer where the league was run).

Copies: groups and teams, the group-stage fixtures in the same order the old page used, their results, and the goals /
assists logged in match details. Knockout rounds aren't copied: draw them again in the new competition once the
group stage is complete. The original league page keeps working and isn't changed.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from competitions import engine
from competitions.models import Competition, Entry, Match, MatchEvent, Team
from league.models import League
from orgs.models import Organization

# The old page's fixed order for 4-team groups (pairs of positions per matchday).
FX4 = [[[0, 3], [1, 2]], [[0, 2], [3, 1]], [[0, 1], [2, 3]], [[3, 0], [2, 1]], [[2, 0], [1, 3]], [[1, 0], [3, 2]]]


def legacy_fixtures(n):
    """Exactly the old page's fx(): a list of matchdays, each a list of (home position, away position)."""
    if n == 4:
        return FX4
    a = list(range(n)) + ([-1] if n % 2 else [])
    m, rounds = len(a), []
    for r in range(m - 1):
        ms = []
        for i in range(m // 2):
            x, y = a[i], a[m - 1 - i]
            if x >= 0 and y >= 0:
                ms.append([y, x] if r % 2 else [x, y])
        rounds.append(ms)
        a.insert(1, a.pop())
    return rounds + [[[q[1], q[0]] for q in ms] for ms in rounds]


SAVED = Path(__file__).resolve().parents[2] / "data" / "original_league.json"


def load_league(file, out):
    if file:
        path = Path(file)
        if not path.is_file():
            raise CommandError(f"No file at {file}.")
    else:
        row = League.objects.filter(pk=1).first()
        data = json.loads(row.data) if row else {}
        if data.get("g"):
            return data
        path = SAVED
        if not path.is_file():
            raise CommandError("There is no original league in this database, and no saved copy. Use --file.")
        out.write(f"This database has no original league, so the saved copy is used: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise CommandError(f"{path} isn't a valid league file.") from None
    if not isinstance(data, dict) or not isinstance(data.get("g"), dict):
        raise CommandError(f"{path} doesn't contain a league (no groups).")
    return data


class Command(BaseCommand):
    help = "Copy the original league (/) into an organization as a competition."

    def add_arguments(self, parser):
        parser.add_argument("org", help="the organization's address (slug), e.g. kasoa-community-league")
        parser.add_argument("--name", default=None)
        parser.add_argument("--season", default="")
        parser.add_argument("--into", default=None, help="an existing competition (slug) in that organization with no teams yet")
        parser.add_argument("--description", default=None)
        parser.add_argument("--file", default=None, help="a saved copy of the original league (JSON)")

    def handle(self, org, name, season, into=None, description=None, file=None, **options):
        o = Organization.objects.filter(slug=org).first()
        if not o:
            raise CommandError(f"No organization with the address {org!r}.")
        s = load_league(file, self.stdout)
        groups = s.get("g") or {}
        if not groups:
            raise CommandError("The original league has no groups.")
        cfg, ui = s.get("cfg") or {}, s.get("ui") or {}
        meta = s.get("competition") or {}                   # details saved with the copy (name, description, season…)
        name = name or meta.get("name") or (ui.get("t") or "League").title()
        fields = dict(kind=meta.get("kind") or "championship", format="groups_knockout" if len(groups) > 1 else "league", status="active",
                      description=description if description is not None else (meta.get("description") or ui.get("s", "").title()),
                      qualifiers_per_group=meta.get("qualifiersPerGroup") or (3 if cfg.get("a") == 3 else 2),
                      third_min_points=meta.get("thirdMinPoints") or (max(0, min(30, int(cfg.get("mp") or 0))) if cfg.get("a") == 3 else 0),
                      tiebreakers=["points", "goal_difference", "goals_for"])
        for k, attr in (("pointsWin", "points_win"), ("pointsDraw", "points_draw"), ("pointsLoss", "points_loss"), ("legs", "legs")):
            if isinstance(meta.get(k), int):
                fields[attr] = meta[k]
        season = season or meta.get("season") or ""
        with transaction.atomic():
            if into:
                c = o.competitions.filter(slug=into).first()
                if not c:                                   # not there yet: create it with that address
                    if Competition.objects.filter(slug=into).exists():
                        raise CommandError(f"The address {into!r} is used by another organization's competition. Choose another with --into.")
                    c = Competition.objects.create(org=o, name=name, slug=into, season=season,
                                                   visibility=meta.get("visibility") or "public", **fields)
                    self.stdout.write(f"Created the competition “{c.name}” ({into}) in {o.name}.")
                if c.entries.exists() or c.matches.exists():
                    raise CommandError(f"{c.name} already has teams or matches; nothing was changed.")
                for k, v in fields.items():
                    setattr(c, k, v)
                if season:
                    c.season = season
                c.save()
            else:
                c = Competition.objects.create(org=o, name=name, slug=engine.unique_slug(Competition, name, "competition"), season=season,
                                               visibility=meta.get("visibility") or "public", **fields)
            om = s.get("organization") or {}                 # fill in the organization's text if it has none yet
            if om.get("description") and not o.description:
                o.description = om["description"][:2000]
            if om.get("tagline") and not o.site().get("tagline"):
                o.website = {**o.site(), "tagline": om["tagline"][:160]}
            o.save(update_fields=["description", "website"])
            made = results = events = 0
            for g, names in groups.items():
                entries = []
                for n in names:
                    team = o.teams.filter(name__iexact=n).first() or Team.objects.create(org=o, name=n, slug=engine.unique_slug(Team, n, "team"))
                    entries.append(Entry.objects.create(competition=c, team=team, group=g if len(groups) > 1 else ""))
                for i, matchday in enumerate(legacy_fixtures(len(names))):
                    for j, (h, a) in enumerate(matchday):
                        home, away = entries[h], entries[a]
                        score = (s.get("r") or {}).get(f"{g}{i}_{j}") or [None, None]
                        done = isinstance(score, list) and len(score) == 2 and all(isinstance(x, int) for x in score)
                        mt = Match.objects.create(
                            competition=c, stage="league", group=home.group, round=i + 1, round_name=f"Matchday {i + 1}",
                            home=home, away=away, status="finished" if done else "scheduled",
                            home_score=score[0] if done else None, away_score=score[1] if done else None,
                            slug=engine.match_slug(Match, home, away, None, season or name))
                        made += 1
                        results += done
                        for e in (s.get("ev") or {}).get(f"r.{g}{i}_{j}", []):
                            MatchEvent.objects.create(match=mt, kind="goal", side="away" if e.get("s") else "home", minute=e.get("m"),
                                                      player_name=(e.get("p") or "")[:80], assist_name=(e.get("a") or "")[:80])
                            events += 1
        self.stdout.write(f"Imported “{c.name}” into {o.name}: {sum(len(v) for v in groups.values())} teams, {made} matches "
                          f"({results} with results), {events} goals. Knockout rounds weren't copied; draw them in the new competition.")
