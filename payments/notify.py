"""Emails about money: receipts to payers, "invoice paid" to the organization's money managers, payout decisions.
They're a courtesy: when email isn't set up, or an email fails, nothing else is affected."""
import logging

from django.conf import settings

from league import emailer
from superadmin import store

log = logging.getLogger("payments")


def _send(to, subject, title, lines, link, button):
    if not to or not emailer.ready():
        return False
    site = store.site()["name"]
    txt, html = emailer.body(title, lines, link or settings.APP_URL or "", button, f"Sent by {site}.")
    try:
        emailer.send(to, subject, txt, html)
        return True
    except emailer.MailError as e:
        log.warning("payment email to %s failed: %s", to, e)
        return False


def managers(org):
    """Emails of the people who handle this organization's money."""
    from orgs.models import Membership
    from orgs.permissions import can
    return [m.user.email for m in Membership.objects.select_related("user").filter(org=org) if m.user.email and can(m.role, "payments.manage", org)]


def invoice_paid(inv, base):
    receipt = f"{base}/receipt/{inv.receipt_token}" if base and inv.receipt_token else ""
    amount = f"{inv.currency} {inv.amount:.2f}"
    if inv.customer_email:
        _send(inv.customer_email, f"Receipt: {amount} paid to {inv.org.name}", "Payment received, thank you",
              [f"Hi {inv.customer_name},", f"{inv.org.name} received your payment of {amount} for {inv.description}.",
               "Keep this email: the link below is your receipt."], receipt, "View your receipt")
    for to in managers(inv.org):
        _send(to, f"{inv.customer_name} paid {amount}", f"{inv.customer_name} paid",
              [f"{inv.customer_name} paid {amount} for {inv.description}.",
               f"Your organization receives {inv.currency} {inv.net:.2f} after the platform fee." if inv.fee else "It's now in your organization's balance."
               if not inv.split_code else "It was paid straight to your organization's PayNova account."],
              f"{base}/app/org/{inv.org.slug}/invoices" if base else "", "See invoices")


def payout_decided(p, base):
    if not (p.requested_by and p.requested_by.email):
        return
    amount = f"{p.currency} {p.amount:.2f}"
    if p.status == "sent":
        title, lines = "Payout sent", [f"The platform approved and sent {amount} for “{p.purpose}”.", f"Reference: {p.reference}" if p.reference else ""]
    else:
        title, lines = "Payout not approved", [f"The platform didn't approve the payout of {amount} for “{p.purpose}”.", f"Reason: {p.note}" if p.note else "",
                                               "The amount is back in your organization's balance."]
    _send(p.requested_by.email, f"{title}: {amount} ({p.org.name})", title, [x for x in lines if x],
          f"{base}/app/org/{p.org.slug}/payments" if base else "", "See payouts")
