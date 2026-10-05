"""Bring the league, draws and admins over from the old Node version's data/league.db."""
import base64
import json
import sqlite3
from datetime import datetime, timezone as tz

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.logic import valid_state
from league.models import Admin, Draw, League


class Command(BaseCommand):
    help = "Import data/league.db from the Node version (league, draws and admins with their passwords)."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", default="data/league.db")
        parser.add_argument("--force", action="store_true", help="replace the league even if this database already has one")

    def handle(self, path, force, **options):
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            raise CommandError(f"Can't open {path}: {e}")
        src.row_factory = sqlite3.Row
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with transaction.atomic():
            row = src.execute("SELECT json FROM state WHERE id=1").fetchone() if "state" in tables else None
            if row:
                s = json.loads(row["json"])
                rev = s.pop("rev", 0) or 0
                if not valid_state(s):
                    raise CommandError("The league in that file isn't valid.")
                existing = League.objects.filter(pk=1).first()
                if existing and existing.rev > 1 and not force:
                    self.stdout.write("This database already has a league; use --force to replace it.")
                else:
                    League.objects.update_or_create(pk=1, defaults={"data": json.dumps(s), "rev": max(rev, (existing.rev if existing else 0)) + 1})
                    self.stdout.write("League imported.")
            if "draws" in tables and not Draw.objects.exists():
                cols = {r[1] for r in src.execute("PRAGMA table_info(draws)")}
                for d in src.execute("SELECT * FROM draws ORDER BY id"):
                    Draw.objects.create(ts=d["ts"], by=d["by"], role=d["role"] if "role" in cols and d["role"] else "", kind=d["kind"], result=d["result"])
                self.stdout.write(f"{Draw.objects.count()} draws imported.")
            if "admins" in tables:
                n = 0
                for a in src.execute("SELECT * FROM admins"):
                    if Admin.objects.filter(username__iexact=a["username"]).exists():
                        continue
                    u = Admin(username=a["username"], email=a["email"] or "", role=a["role"], must_change=bool(a["must_change"]),
                              invited_by=a["invited_by"] or "", date_joined=datetime.fromtimestamp(a["created"] / 1000, tz.utc))
                    if a["hash"]:
                        # Node used scrypt(N=16384, r=8, p=1, 64 bytes) with the hex salt as text: Django's scrypt format.
                        u.password = f"scrypt$16384${a['salt']}$8$1${base64.b64encode(bytes.fromhex(a['hash'])).decode()}"
                    else:
                        u.set_unusable_password()
                    u.save()
                    n += 1
                self.stdout.write(f"{n} admins imported (they keep their passwords).")
