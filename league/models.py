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
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=300)
    ip = models.CharField(max_length=64, blank=True)


class Hit(models.Model):
    """One event for rate limiting (failed logins, invites…). Stored in the database so it works across workers."""
    key = models.CharField(max_length=300, db_index=True)
    ts = models.FloatField(db_index=True)
