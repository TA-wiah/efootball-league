"""What's new: the phone menu bar on every dashboard page; messages above it."""
from datetime import date

from django.db import migrations

TITLE = "Phone menu bar on every page"


def add(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.create(
        date=date(2026, 10, 12), audience=["public"], title=TITLE, published=True,
        body="The menu bar at the bottom of the screen on phones is now on every dashboard page, including My organizations "
             "(Clubs, Matches, Profile, Help, More). Messages like \"Saved\" now appear above it instead of hiding under it.")


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title=TITLE, author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0012_game_names")]
    operations = [migrations.RunPython(add, remove)]
