from django.conf import settings
from django.db import models


class SmsAccount(models.Model):
    """An organization's SMS credits. One credit = one SMS part to one person."""
    org = models.OneToOneField("orgs.Organization", on_delete=models.CASCADE, related_name="sms")
    credits = models.PositiveIntegerField(default=0)


class SmsMessage(models.Model):
    """Every text an organization sent or tried to send (blocked ones included)."""
    SENT, FAILED, BLOCKED = "sent", "failed", "blocked"
    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE, related_name="texts")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=10, default="message")          # message / invoice
    body = models.TextField(max_length=1000)
    recipients = models.JSONField(default=list)
    parts = models.PositiveSmallIntegerField(default=1)
    credits = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, db_index=True)
    reasons = models.JSONField(default=list, blank=True)                 # why it was blocked
    error = models.CharField(max_length=300, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class SmsPurchase(models.Model):
    """A request to buy SMS credits: paid through PayNova, or added by a super admin."""
    PENDING, PAID, GRANTED, REJECTED, CANCELLED = "pending", "paid", "granted", "rejected", "cancelled"
    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE, related_name="sms_purchases")
    credits = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    method = models.CharField(max_length=10)                              # paynova / request
    status = models.CharField(max_length=10, default=PENDING, db_index=True)
    reference = models.CharField(max_length=80, blank=True)
    checkout_url = models.URLField(max_length=500, blank=True)
    note = models.CharField(max_length=300, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created = models.DateTimeField(auto_now_add=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
