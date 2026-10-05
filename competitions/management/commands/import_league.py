"""Copy the original single league (the page at /) into an organization as a normal competition.

    python manage.py import_league kasoa-community-league
    python manage.py import_league kasoa-community-league --name "Champions League" --season 2026
    python manage.py import_league robotics-league --into robotics-championship   (fill an existing, empty competition)

Copies: groups and teams, the group-stage fixtures in the same order the old page used, their results, and the goals /
assists logged in match details. Knockout rounds aren't copied: draw them again in the new competition once the
group stage is complete. The original league page keeps working and isn't changed.
"""
import json

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


class Command(BaseCommand):
    help = "Copy the original league (/) into an organization as a competition."

    def add_arguments(self, parser):
        parser.add_argument("org", help="the organization's address (slug), e.g. kasoa-community-league")
        parser.add_argument("--name", default=None)
        parser.add_argument("--season", default="")
        parser.add_argument("--into", default=None, help="an existing competition (slug) in that organization with no teams yet")
        parser.add_argument("--description", default=None)

    def handle(self, org, name, season, into=None, description=None, **options):
        o = Organization.objects.filter(slug=org).first()
        if not o:
            raise CommandError(f"No organization with the address {org!r}.")
        row = League.objects.filter(pk=1).first()
        if not row:
            raise CommandError("There is no original league to import.")
        s = json.loads(row.data)
        groups = s.get("g") or {}
        if not groups:
            raise CommandError("The original league has no groups.")
        cfg, ui = s.get("cfg") or {}, s.get("ui") or {}
        name = name or (ui.get("t") or "League").title()
        fields = dict(kind="championship", format="groups_knockout" if len(groups) > 1 else "league", status="active",
                      description=description if description is not None else ui.get("s", "").title(),
                      qualifiers_per_group=3 if cfg.get("a") == 3 else 2, tiebreakers=["points", "goal_difference", "goals_for"])
        with transaction.atomic():
            if into:
                c = o.competitions.filter(slug=into).first()
                if not c:
                    raise CommandError(f"{o.name} has no competition with the address {into!r}.")
                if c.entries.exists() or c.matches.exists():
                    raise CommandError(f"{c.name} already has teams or matches; nothing was changed.")
                for k, v in fields.items():
                    setattr(c, k, v)
                if season:
                    c.season = season
                c.save()
            else:
                c = Competition.objects.create(org=o, name=name, slug=engine.unique_slug(Competition, name, "competition"), season=season, **fields)
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
