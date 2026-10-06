from django.conf import settings
from django.db import models


class PlatformSetting(models.Model):
    """Platform-wide settings edited by super admins (email, sign-ups, approvals…). Secrets are never sent back to the browser."""
    key = models.CharField(max_length=60, primary_key=True)
    value = models.JSONField(default=dict, blank=True)
    updated = models.DateTimeField(auto_now=True)


class SupportTicket(models.Model):
    KINDS = [("support", "Support request"), ("complaint", "Complaint"), ("organizer_request", "Organizer request")]
    STATUSES = [("open", "Open"), ("in_progress", "In progress"), ("closed", "Closed")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets")
    kind = models.CharField(max_length=20, choices=KINDS, default="support")
    subject = models.CharField(max_length=140)
    body = models.TextField(max_length=5000)
    status = models.CharField(max_length=12, choices=STATUSES, default="open", db_index=True)
    reply = models.TextField(blank=True, max_length=5000)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)


class PlatformAnnouncement(models.Model):
    """Shown to every logged-in user on their dashboard."""
    title = models.CharField(max_length=140)
    body = models.TextField(blank=True, max_length=3000)
    level = models.CharField(max_length=10, default="info")    # info / warning
    active = models.BooleanField(default=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created = models.DateTimeField(auto_now_add=True)
