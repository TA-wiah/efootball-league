"""What's new: flyer captions fall back Grok → Aries → built-in wording."""
from datetime import date

from django.db import migrations

TITLE = "Flyer captions: Grok, then Aries, then our own words"


def add(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.create(
        date=date(2026, 10, 24), audience=["superadmin"], title=TITLE, published=True,
        body="When Writing is set to Grok, a flyer caption is written by Grok; if Grok doesn't answer, Aries writes it; "
             "if Aries is busy or down too, built-in wording is used. With Writing set to Aries, it's Aries then built-in wording.")


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title=TITLE, author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0015_champions_formation_grok")]
    operations = [migrations.RunPython(add, remove)]
