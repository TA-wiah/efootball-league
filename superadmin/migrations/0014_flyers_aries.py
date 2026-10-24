"""What's new: flyers for the big knockout matches and lineups; Aries writes the captions; faster lineup reading."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-24", ["public"], "Flyers for finals, semi-finals, quarter-finals and your lineup",
     "- Your dashboard now has a Flyers card for every quarter-final, semi-final and final your team (or organization) plays: "
     "download a square image for WhatsApp, or a wide one for PC (1920×1080).\n"
     "- Played matches get a full-time flyer with the score.\n"
     "- On your team page, download a lineup flyer: your starting XI on a pitch with positions and ratings, and the subs.\n"
     "- Reading lineup screenshots is faster: up to three are read at the same time."),
    ("2026-10-24", ["superadmin"], "Aries writes the flyer captions",
     "Platform settings → AI → Writing: Aries (free, no key) is now the default text AI. It writes the one-line caption on match flyers. "
     "Aries can't read images, so lineup and result screenshots still use the screenshot reader (Gemini or OpenAI). "
     "Only public match details are sent to Aries; when it doesn't answer, built-in wording is used. Set Writing to Off to stop using it."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0013_tabbar_everywhere")]
    operations = [migrations.RunPython(add, remove)]
