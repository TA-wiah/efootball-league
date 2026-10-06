from decimal import Decimal

from django.conf import settings
from django.db import models


class Invoice(models.Model):
    """A bill an organization sends through the platform's PayNova account (e.g. a team's entry fee)."""
    PENDING, PAID, CANCELLED = "pending", "paid", "cancelled"
    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE, related_name="bills")
    competition = models.ForeignKey("competitions.Competition", null=True, blank=True, on_delete=models.SET_NULL, related_name="bills")
    team = models.ForeignKey("competitions.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="bills")
    customer_name = models.CharField(max_length=100)
    customer_email = models.EmailField(blank=True)                           # empty: phone only (we text the pay link)
    customer_phone = models.CharField(max_length=30, blank=True)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, default=PENDING, db_index=True)
    code = models.CharField(max_length=64, blank=True, db_index=True)        # PayNova invoice_code (invoices with an email)
    reference = models.CharField(max_length=80, blank=True, db_index=True)   # PayNova payment reference (phone-only invoices)
    sms = models.CharField(max_length=120, blank=True)                       # what happened to the text with the pay link
    reminded_at = models.DateTimeField(null=True, blank=True)                # last payment reminder by text
    split_code = models.CharField(max_length=60, blank=True)                 # paid straight to the organization's PayNova account
    receipt_token = models.CharField(max_length=40, blank=True, db_index=True)   # the private receipt link
    number = models.PositiveIntegerField(null=True, blank=True)
    pay_url = models.URLField(max_length=500, blank=True)
    mode = models.CharField(max_length=4, blank=True)                         # test / live
    fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))   # the platform fee when it was sent
    fee_fixed = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))         # worked out when paid
    net = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    paid_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-id"]


class Payout(models.Model):
    """Money leaving an organization's balance (a prize, or a withdrawal). A super admin approves it before it's sent."""
    REQUESTED, PROCESSING, SENT, REJECTED = "requested", "processing", "sent", "rejected"
    METHODS = {"paynova": "PayNova account", "mobile_money": "Mobile money", "bank": "Bank account"}
    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE, related_name="payouts")
    competition = models.ForeignKey("competitions.Competition", null=True, blank=True, on_delete=models.SET_NULL, related_name="payouts")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    method = models.CharField(max_length=15)
    destination = models.JSONField(default=dict)
    purpose = models.CharField(max_length=200)
    status = models.CharField(max_length=10, default=REQUESTED, db_index=True)
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=300, blank=True)                       # super admin's note, or PayNova's answer
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class OrgPaySettings(models.Model):
    """Set by a super admin: a PayNova split code, so this organization's invoices are paid straight into its own
    PayNova account (the split in PayNova's dashboard decides each share)."""
    org = models.OneToOneField("orgs.Organization", on_delete=models.CASCADE, related_name="pay_settings")
    split_code = models.CharField(max_length=60, blank=True)
    updated = models.DateTimeField(auto_now=True)
