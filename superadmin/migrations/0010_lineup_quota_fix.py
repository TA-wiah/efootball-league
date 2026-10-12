"""What's new: players reach the lineup upload; failed reads no longer use up the daily screenshots."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-12", ["public"], "Lineup screenshots: only successful reads count",
     "- Players, coaches and team managers now go straight to their team's lineup upload from \"Add lineup\".\n"
     "- A screenshot that fails to read, or where no player cards are found, no longer uses up your daily screenshots.\n"
     "- If one screenshot in an upload fails, the others are still read and put together."),
    ("2026-10-12", ["superadmin"], "Screenshot counts: successful reads only",
     "Platform settings → AI now shows how many screenshots were read in the last 24 hours. Only successful reads count, "
     "for each person and for the optional platform-wide cap. Failed attempts from earlier no longer count. "
     "The platform cap is shared by everyone; leave it at 0 unless you need to limit costs."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0009_tabbar_update_public")]
    operations = [migrations.RunPython(add, remove)]
