"""Feature (or un-feature) a public competition on the landing and discovery pages.

    python manage.py feature kasoa-sunday-league
    python manage.py feature kasoa-sunday-league --off
"""
from django.core.management.base import BaseCommand, CommandError

from competitions.models import Competition


class Command(BaseCommand):
    help = "Feature a competition on the landing page and in discovery (platform owner only)."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--off", action="store_true")

    def handle(self, slug, off, **options):
        c = Competition.objects.filter(slug=slug).first()
        if not c:
            raise CommandError(f"No competition with the address {slug!r}.")
        if c.visibility != "public" and not off:
            self.stderr.write("Note: only public competitions appear on the landing page; this one isn't public.")
        Competition.objects.filter(id=c.id).update(featured=not off)
        self.stdout.write(f"{c.name} is {'no longer ' if off else ''}featured.")
