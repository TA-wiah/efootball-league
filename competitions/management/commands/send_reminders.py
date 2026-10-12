from django.core.management.base import BaseCommand

from competitions.reminders import send_due, send_squad_due


class Command(BaseCommand):
    help = "Send match reminders (email, and SMS where organizations switched it on) for matches starting soon."

    def handle(self, *args, **opts):
        self.stdout.write(f"{send_due(force=True)} match reminder(s) sent.")
        self.stdout.write(f"{send_squad_due(force=True)} squad reminder(s) sent.")
