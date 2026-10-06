"""Make an account a platform super admin (or remove that).

    python manage.py superadmin kofi
    python manage.py superadmin kofi --off
"""
from django.core.management.base import BaseCommand, CommandError

from league.logic import audit
from league.models import Admin


class Command(BaseCommand):
    help = "Give (or remove) platform super admin rights."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--off", action="store_true")

    def handle(self, username, off, **options):
        u = Admin.objects.filter(username__iexact=username).first()
        if not u:
            raise CommandError(f"No account called {username!r}.")
        if off and Admin.objects.filter(is_superuser=True, is_active=True).exclude(pk=u.pk).count() == 0:
            raise CommandError("That's the last super admin; make someone else super admin first.")
        Admin.objects.filter(pk=u.pk).update(is_superuser=not off, is_staff=not off)
        audit("server", f"{'removed' if off else 'granted'} super admin", resource=f"user:{u.username}", old=u.is_superuser, new=not off)
        self.stdout.write(f"{u.username} is {'no longer ' if off else 'now '}a super admin.")
