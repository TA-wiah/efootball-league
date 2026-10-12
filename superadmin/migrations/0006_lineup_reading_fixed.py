"""What's new: lineup screenshots read again (and faster, with better name matching); the Gemini model setting."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-12", ["public"], "Lineup screenshots: faster and better at names",
     "Reading lineup screenshots works again, and a whole upload now takes under a minute.\n"
     "- Names from your players list are matched using each card's flag or club badge as well as its position and rating, "
     "so players you use out of position (like an LB card at CB) are found too.\n"
     "- Upload more players list screenshots to get more names filled in. Anything that could be two players is left for you to choose."),
    ("2026-10-12", ["superadmin"], "Gemini model: gemini-flash-latest",
     "Google retired gemini-2.5-flash for new accounts, which stopped lineup screenshots from being read. "
     "Reading now uses gemini-flash-latest (always Google's current Flash model), set in Platform settings → AI → Gemini model. "
     "When reading fails, the real reason from Google is written to the server log."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0005_updates_nav_keys")]
    operations = [migrations.RunPython(add, remove)]
