from decimal import Decimal

from django.conf import settings
from django.db import models


class Season(models.Model):
    """One Pro League season: proposed from the rankings (or last season), invited, played in divisions, finished."""
    DRAFT, INVITING, RUNNING, FINISHED = "draft", "inviting", "running", "finished"
    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE, related_name="pro_seasons")   # the Pro League organization
    number = models.PositiveIntegerField()
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=10, default=DRAFT, db_index=True)
    divisions = models.JSONField(default=list)              # division names for this season, top first
    size = models.PositiveSmallIntegerField(default=6)      # most teams in a division (4–6)
    move = models.PositiveSmallIntegerField(default=1)      # teams promoted / relegated between divisions
    legs = models.PositiveSmallIntegerField(default=2)
    country = models.CharField(max_length=60, blank=True)   # empty: worldwide
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="GHS")
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-number"]


class SeasonTeam(models.Model):
    """A team's place in a season: proposed → invited → accepted (→ paid) → confirmed, or declined / removed."""
    PROPOSED, INVITED, ACCEPTED, CONFIRMED, DECLINED, REMOVED = "proposed", "invited", "accepted", "confirmed", "declined", "removed"
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="teams")
    team = models.ForeignKey("competitions.Team", on_delete=models.CASCADE, related_name="pro_seasons")
    seed = models.PositiveIntegerField()                    # order of strength when the season was proposed (1 = strongest)
    rating = models.PositiveIntegerField(default=1500)
    reason = models.CharField(max_length=60, blank=True)    # "Ranked #3", "Promoted from Division B"…
    status = models.CharField(max_length=10, default=PROPOSED, db_index=True)
    responded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    responded_at = models.DateTimeField(null=True, blank=True)
    paid = models.BooleanField(default=False)
    reference = models.CharField(max_length=80, blank=True)     # PayNova payment for the entry fee
    checkout_url = models.URLField(max_length=500, blank=True)
    division = models.PositiveSmallIntegerField(null=True, blank=True)    # 0 = top division
    competition = models.ForeignKey("competitions.Competition", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    final_position = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["seed"]
        constraints = [models.UniqueConstraint(fields=["season", "team"], name="one_place_per_team_per_season")]
