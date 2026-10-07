"""Settle Pro League matches nobody reported (walkover or "not played"). Safe to run often, e.g. every hour:

    python manage.py settle_walkovers
"""
from django.core.management.base import BaseCommand

from competitions.reports import settle_overdue


class Command(BaseCommand):
    help = "Settle overdue Pro League matches: a walkover for the team that showed up, 'not played' when neither did."

    def handle(self, **options):
        n = settle_overdue(force=True)
        self.stdout.write(f"Settled {n} match{'es' if n != 1 else ''}.")
