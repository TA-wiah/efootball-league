"""Save the league and its draws to a JSON file (works with SQLite and Postgres). Keeps the newest 14."""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from league.logic import read_state
from league.models import Draw


class Command(BaseCommand):
    help = "Write a JSON backup of the league to BACKUP_DIR (default: data/backups)."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=None)

    def handle(self, dir=None, **options):
        import os
        folder = Path(dir or os.environ.get("BACKUP_DIR") or settings.DB_FILE.parent / "backups")
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"league-{datetime.now(timezone.utc):%Y-%m-%d-%H%M}.json"
        data = {"league": read_state(), "draws": [{"id": d.id, "ts": d.ts, "by": d.by, "role": d.role, "kind": d.kind,
                                                    "result": json.loads(d.result)} for d in Draw.objects.order_by("id")]}
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        for old in sorted(folder.glob("league-*.json"))[:-14]:
            old.unlink()
        self.stdout.write(f"Backup written to {f}")
