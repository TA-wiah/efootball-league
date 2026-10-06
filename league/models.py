from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class Admin(AbstractUser):
    """A person who can edit the league. Exactly one is the owner."""
    OWNER, ADMIN = "owner", "admin"
    role = models.CharField(max_length=10, choices=[(OWNER, "Owner"), (ADMIN, "Admin")], default=ADMIN)
    must_change = models.BooleanField(default=False)        # temporary password: choose a new one first
    invited_by = models.CharField(max_length=150, blank=True)
    session_epoch = models.PositiveIntegerField(default=0)  # bumped by "log out on all devices"
    # Can edit the original single league (/). Platform sign-ups start without it.
    league_access = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)    # last request while logged in ("active users")
    phone = models.CharField(max_length=30, blank=True)
    # is_superuser (from Django) = platform super admin: sees and manages the whole platform at /admin.
    # is_active = False means suspended: can't log in, and existing sessions stop working.

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("username"), name="admin_username_ci"),
            models.UniqueConstraint(Lower("email"), condition=~models.Q(email=""), name="admin_email_ci"),
        ]

    @property
    def pending(self):
        return not self.has_usable_password()


class League(models.Model):
    """The whole league (groups, scores, settings…) as one JSON document. `rev` goes up on every save."""
    data = models.TextField(default="{}")    # kept as text so the page gets exactly what it saved
    rev = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)


class Draw(models.Model):
    """An official random draw. Written once by the server, never edited."""
    ts = models.BigIntegerField()            # milliseconds since 1970, like the page uses
    by = models.CharField(max_length=150)
    role = models.CharField(max_length=10)
    kind = models.CharField(max_length=10)   # "groups" or "knockout"
    result = models.TextField()


class Token(models.Model):
    """A single-use invite or password-reset link (only its hash is stored)."""
    hash = models.CharField(max_length=64, primary_key=True)
    admin = models.ForeignKey(Admin, on_delete=models.CASCADE)
    kind = models.CharField(max_length=10)   # "invite" or "reset"
    expires = models.DateTimeField()


class Audit(models.Model):
    """The platform audit log: who did what, to which resource, with the value before and after."""
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=300)
    ip = models.CharField(max_length=64, blank=True)
    resource = models.CharField(max_length=200, blank=True, db_index=True)   # e.g. "user:kofi", "competition:kasoa-league"
    old = models.TextField(blank=True)
    new = models.TextField(blank=True)
    device = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, default="ok")                   # ok / failed / denied


class Hit(models.Model):
    """One event for rate limiting (failed logins, invites…). Stored in the database so it works across workers."""
    key = models.CharField(max_length=300, db_index=True)
    ts = models.FloatField(db_index=True)
