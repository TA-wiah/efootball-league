"""What's new: flyers made automatically as things happen; screenshot reading never hangs."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-28", ["public"], "Flyers made for you, automatically",
     "Your dashboard's Flyers card now fills itself as things happen: every match in the next three days, every result from the last week, "
     "quarter-finals, semi-finals, finals, champions and your team's lineup. Each one shows a preview, new ones are marked New, and the "
     "Notifications page tells you when new flyers are ready. Download them square for WhatsApp or wide for PC."),
    ("2026-10-28", ["public"], "Reading screenshots: no more waiting forever",
     "- Each screenshot now gets about a minute at most; if it can't be read in time it's marked and the next one goes.\n"
     "- You see every screenshot's status (Reading, Read, Failed) while they're read, three at a time.\n"
     "- \"Stop and use what's read\" finishes straight away with the screenshots already read.\n"
     "- A reading service that's down or has a bad key is skipped for a while instead of slowing every screenshot."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0019_xi_numbers")]
    operations = [migrations.RunPython(add, remove)]
