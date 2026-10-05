from django.conf import settings
from django.db import models
from django.utils import timezone

from .permissions import ROLE_INFO, ROLES


ORG_KINDS = [("league", "League"), ("school", "School"), ("club", "Club"), ("academy", "Academy"), ("company", "Company"),
             ("community", "Community"), ("association", "Association"), ("other", "Other")]
WEBSITE_DEFAULTS = {"tagline": "", "about": "", "contact": "", "show_competitions": True, "show_teams": True, "show_players": True,
                    "show_fixtures": True, "show_results": True, "show_standings": True, "show_news": True, "show_brackets": True}


class Organization(models.Model):
    """An independent group (league, school, club, company…) that runs its own competitions."""
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=60, unique=True)
    description = models.TextField(blank=True, max_length=2000)
    country = models.CharField(max_length=60, blank=True)
    region = models.CharField(max_length=60, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created = models.DateTimeField(auto_now_add=True)
    # active: normal · pending: waiting for platform approval (works, but nothing is published) · suspended: blocked
    status = models.CharField(max_length=10, default="active", db_index=True)
    kind = models.CharField(max_length=15, choices=ORG_KINDS, default="league")
    timezone = models.CharField(max_length=60, blank=True)
    brand_color = models.CharField(max_length=7, blank=True)          # "#2f7bff"; used on its public website
    logo_data = models.BinaryField(null=True, blank=True, editable=True)
    logo_type = models.CharField(max_length=30, blank=True)
    logo_version = models.PositiveIntegerField(default=0)
    website = models.JSONField(default=dict, blank=True)              # public website settings, see WEBSITE_DEFAULTS
    permissions = models.JSONField(default=dict, blank=True)          # owner's overrides: {permission: [roles]}

    def __str__(self):
        return self.name

    def site(self):
        return {**WEBSITE_DEFAULTS, **(self.website or {})}


class Membership(models.Model):
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=[(r, ROLE_INFO[r][0]) for r in ROLES])
    joined = models.DateTimeField(auto_now_add=True)
    teams = models.ManyToManyField("competitions.Team", blank=True, related_name="assigned_staff")   # team managers & coaches
    last_active = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "user"], name="one_membership_per_org"),
            models.UniqueConstraint(fields=["org"], condition=models.Q(role="owner"), name="one_owner_per_org"),
        ]


class Invitation(models.Model):
    """An invitation to join an organization, by email or as a shareable link. Only the token's hash is stored."""
    PENDING, ACCEPTED, REVOKED, EXPIRED = "pending", "accepted", "revoked", "expired"
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField(blank=True)          # empty for link invitations
    role = models.CharField(max_length=20, choices=[(r, ROLE_INFO[r][0]) for r in ROLES])
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created = models.DateTimeField(auto_now_add=True)
    expires = models.DateTimeField()
    status = models.CharField(max_length=10, default=PENDING)   # pending / accepted / revoked ("expired" is worked out)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    accepted_at = models.DateTimeField(null=True, blank=True)
    teams = models.ManyToManyField("competitions.Team", blank=True, related_name="invitations")   # joins these teams on accepting

    @property
    def state(self):
        if self.status == self.PENDING and self.expires < timezone.now():
            return self.EXPIRED
        return self.status


class OrgEvent(models.Model):
    """The organization's activity log, shown on its dashboard."""
    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="events")
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=300)
