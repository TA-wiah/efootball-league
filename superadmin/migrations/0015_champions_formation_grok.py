"""What's new: champions flyer, team formations (standard and fluid), Grok."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-24", ["public"], "Champions flyer and your formation",
     "- When a final is played, its flyer becomes a Champions flyer: the winners' logo in gold, \"win the\" competition, and the final score "
     "(penalties count). Finals still to play keep their match flyer.\n"
     "- Flyers use each team's own name and logo from the platform.\n"
     "- Set your formation on the team page: Standard (one shape, like 4-3-3) or Fluid (eFootball's Fluid Formation: one shape with the ball, "
     "one without). It shows on your lineup flyer, and a lineup screenshot fills it in when you haven't set one."),
    ("2026-10-24", ["superadmin"], "Grok (xAI) for screenshots and captions",
     "Platform settings → AI: Grok can now read lineup and result screenshots (choose it under Reading lineup screenshots) and write "
     "flyer captions (under Writing). It needs an xAI API key and is paid per use by xAI. Aries stays the free default for captions; "
     "Aries can't read images, even on its image endpoint, so screenshots need Gemini, Grok or OpenAI."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0014_flyers_aries")]
    operations = [migrations.RunPython(add, remove)]
