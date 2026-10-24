"""What's new: screenshots are read by a chain OpenAI → Gemini → Grok."""
from datetime import date

from django.db import migrations

TITLE = "Reading screenshots: OpenAI, then Gemini, then Grok"


def add(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.create(
        date=date(2026, 10, 24), audience=["superadmin"], title=TITLE, published=True,
        body="Lineup and result screenshots are now read by a chain: OpenAI first, then Gemini, then Grok, using each one that has a key "
             "saved in Platform settings → AI. When one fails or is busy, the next takes over, so a screenshot still gets read when a "
             "service is down. Aries isn't in this chain (it can't read images); it writes flyer captions.")


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title=TITLE, author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0016_caption_chain")]
    operations = [migrations.RunPython(add, remove)]
