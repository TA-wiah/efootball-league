"""Generic competitions: the same models run a Champions League, a school cup or a Sunday league.
Everything a competition does differently (format, points, tie-breakers, legs…) is configuration, not code."""
from django.conf import settings
from django.db import models

from orgs.models import Organization

KINDS = [("league", "League"), ("tournament", "Tournament"), ("cup", "Cup"), ("championship", "Championship"),
         ("friendly", "Friendly series"), ("other", "Other")]
FORMATS = [("league", "League: everyone plays everyone"), ("groups_knockout", "Groups, then knockouts"), ("knockout", "Knockout only")]
VISIBILITY = [("public", "Public: listed and shareable"), ("unlisted", "Unlisted: only people with the link"), ("private", "Private: members only")]
COMP_STATUS = [("draft", "Draft"), ("active", "In progress"), ("completed", "Completed")]
MATCH_STATUS = [("scheduled", "Scheduled"), ("live", "Live"), ("finished", "Finished"), ("postponed", "Postponed"), ("cancelled", "Cancelled")]
EVENT_KINDS = [("goal", "Goal"), ("penalty_goal", "Penalty goal"), ("own_goal", "Own goal"), ("yellow", "Yellow card"),
               ("second_yellow", "Second yellow"), ("red", "Red card"), ("substitution", "Substitution")]
POSITIONS = [("", "—"), ("GK", "Goalkeeper"), ("DF", "Defender"), ("MF", "Midfielder"), ("FW", "Forward")]


class Logo(models.Model):
    """Small uploaded images, stored in the database so they survive hosts that wipe their disk."""
    data = models.BinaryField(null=True, blank=True, editable=True)
    content_type = models.CharField(max_length=30, blank=True)
    version = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True


class Competition(Logo):
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="competitions")
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True, max_length=5000)
    country = models.CharField(max_length=60, blank=True)
    region = models.CharField(max_length=60, blank=True)
    season = models.CharField(max_length=30, blank=True)
    kind = models.CharField(max_length=15, choices=KINDS, default="league")
    format = models.CharField(max_length=20, choices=FORMATS, default="league")
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default="public")
    status = models.CharField(max_length=10, choices=COMP_STATUS, default="draft")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    schedule = models.JSONField(default=dict, blank=True)
    money = models.JSONField(default=dict, blank=True)       # {"currency", "entryFee", "prizes": [{"label", "amount"}]}   # how fixtures get dates: see engine.SCHEDULE_DEFAULT
    rules = models.TextField(blank=True, max_length=10000)
    points_win = models.PositiveSmallIntegerField(default=3)
    points_draw = models.PositiveSmallIntegerField(default=1)
    points_loss = models.PositiveSmallIntegerField(default=0)
    tiebreakers = models.JSONField(default=list, blank=True)     # ordered keys from engine.CRITERIA
    legs = models.PositiveSmallIntegerField(default=1)            # how often teams meet in the league stage
    max_teams = models.PositiveSmallIntegerField(null=True, blank=True)
    qualifiers_per_group = models.PositiveSmallIntegerField(default=2)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    featured = models.BooleanField(default=False)
    suspended = models.BooleanField(default=False)          # set by a super admin: hidden publicly and read-only          # set by the platform owner: python manage.py feature <slug>
    views = models.PositiveIntegerField(default=0)          # public page views, for "popular"

    def __str__(self):
        return self.name


class Team(Logo):
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=80)
    short_name = models.CharField(max_length=12, blank=True)
    slug = models.SlugField(max_length=80, unique=True)
    city = models.CharField(max_length=60, blank=True)
    venue = models.CharField(max_length=100, blank=True)          # home ground
    founded = models.PositiveSmallIntegerField(null=True, blank=True)
    colors = models.CharField(max_length=40, blank=True)
    description = models.TextField(blank=True, max_length=2000)
    created = models.DateTimeField(auto_now_add=True)
    suspended = models.BooleanField(default=False)          # set by a super admin: no public page

    def __str__(self):
        return self.name


class Player(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="players")
    name = models.CharField(max_length=80)
    number = models.PositiveSmallIntegerField(null=True, blank=True)
    position = models.CharField(max_length=2, choices=POSITIONS, blank=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)


class Entry(models.Model):
    """A team taking part in a competition (a team can be in several competitions)."""
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="entries")
    team = models.ForeignKey(Team, on_delete=models.RESTRICT, related_name="entries")
    group = models.CharField(max_length=10, blank=True)
    points_adjustment = models.SmallIntegerField(default=0)       # e.g. −3 for a sanction
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["competition", "team"], name="team_once_per_competition")]


class Match(models.Model):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="matches")
    stage = models.CharField(max_length=10, default="league")     # "league" (counts in tables) or "knockout"
    group = models.CharField(max_length=10, blank=True)
    round = models.PositiveSmallIntegerField(default=1)            # matchday / round number
    round_name = models.CharField(max_length=40, blank=True)
    leg = models.PositiveSmallIntegerField(default=1)
    slot = models.PositiveSmallIntegerField(default=0)             # knockout plan: tie number within its round
    home_from = models.CharField(max_length=60, blank=True)        # knockout plan: where the team comes from (see bracket.py)
    away_from = models.CharField(max_length=60, blank=True)
    home = models.ForeignKey(Entry, null=True, blank=True, on_delete=models.RESTRICT, related_name="+")
    away = models.ForeignKey(Entry, null=True, blank=True, on_delete=models.RESTRICT, related_name="+")
    kickoff = models.DateTimeField(null=True, blank=True)
    venue = models.CharField(max_length=100, blank=True)
    referee = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=MATCH_STATUS, default="scheduled")
    home_score = models.PositiveSmallIntegerField(null=True, blank=True)
    away_score = models.PositiveSmallIntegerField(null=True, blank=True)
    home_pens = models.PositiveSmallIntegerField(null=True, blank=True)
    away_pens = models.PositiveSmallIntegerField(null=True, blank=True)
    slug = models.SlugField(max_length=140, unique=True)
    notes = models.TextField(blank=True, max_length=2000)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)   # when the result was entered (for analytics)

    class Meta:
        ordering = ["round", "kickoff", "id"]


class MatchEvent(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="events")
    minute = models.PositiveSmallIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=15, choices=EVENT_KINDS)
    side = models.CharField(max_length=4)                          # "home" or "away"
    player = models.ForeignKey(Player, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    player_name = models.CharField(max_length=80, blank=True)
    assist = models.ForeignKey(Player, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    assist_name = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=120, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["minute", "id"]


class Announcement(models.Model):
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="announcements")
    competition = models.ForeignKey(Competition, null=True, blank=True, on_delete=models.CASCADE, related_name="announcements")
    title = models.CharField(max_length=140)
    body = models.TextField(max_length=5000, blank=True)
    published = models.BooleanField(default=True)
    pinned = models.BooleanField(default=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
