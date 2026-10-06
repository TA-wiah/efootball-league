"""Create the first admin, or reset a password, from ADMIN_USER / ADMIN_PASSWORD / ADMIN_EMAIL. Runs at every start."""
import os
import secrets

from django.core.management.base import BaseCommand

from league.logic import EMAIL_RE, audit, password_problem
from league.models import Admin


class Command(BaseCommand):
    help = "Create or update the admin login from ADMIN_USER / ADMIN_PASSWORD / ADMIN_EMAIL."

    def handle(self, *args, **options):
        user = os.environ.get("ADMIN_USER") or "admin"
        pw = os.environ.get("ADMIN_PASSWORD", "")
        email = os.environ.get("ADMIN_EMAIL", "")
        if pw:
            problem = password_problem(pw, Admin(username=user, email=email))
            if problem:
                self.stderr.write(f"!! ADMIN_PASSWORD was NOT applied: {problem} Fix it and restart.")
                pw = ""
        if pw:
            a = Admin.objects.filter(username__iexact=user).first()
            if not a:
                role = Admin.ADMIN if Admin.objects.filter(role=Admin.OWNER).exists() else Admin.OWNER
                a = Admin(username=user, role=role)
            a.league_access = True
            a.is_superuser = a.is_staff = True        # the person running the site is the platform super admin
            if email and EMAIL_RE.fullmatch(email) and not Admin.objects.filter(email__iexact=email).exclude(pk=a.pk).exists():
                a.email = email
            # Only reset when it's really a new password, so leaving it set doesn't log the admin out on every restart.
            if not a.pk or not a.has_usable_password() or not a.check_password(pw):
                a.set_password(pw)
                a.must_change = False
                a.session_epoch += 1
                a.save()
                audit("server", f"password for {a.username} set from ADMIN_PASSWORD")
                self.stdout.write(f'Password for "{a.username}" was set from ADMIN_PASSWORD. Remove it once you can log in.')
            else:
                a.save()
        elif not Admin.objects.exists():
            temp = secrets.token_hex(6)
            a = Admin(username="admin", role=Admin.OWNER, must_change=True, league_access=True, is_superuser=True, is_staff=True)
            a.set_password(temp)
            a.save()
            self.stdout.write(f"!! First run. Log in as  admin / {temp}  – you will be asked to choose a new password.")
