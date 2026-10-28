"""What's new: tick the starting XI; shirt numbers from screenshots; the lineup flyer follows your formation."""
from datetime import date

from django.db import migrations

TITLE = "Pick your starting XI, and shirt numbers"


def add(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.create(
        date=date(2026, 10, 28), audience=["public"], title=TITLE, published=True,
        body="- On your team page, tick XI next to the players who start (up to 11). Anyone on the team can do it.\n"
             "- Your lineup flyer shows the ticked XI laid out in your formation, like 4-2-3-1 (for a fluid formation, the shape with the ball), "
             "with shirt numbers next to the names.\n"
             "- Shirt numbers are read from your screenshots when the cards show them, and you can type them in the lineup list.")


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title=TITLE, author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0018_aries_writes")]
    operations = [migrations.RunPython(add, remove)]
