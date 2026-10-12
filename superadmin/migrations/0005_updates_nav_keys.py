"""What's new: the Updates link, lineup uploads per day, backup Gemini keys and the platform's daily screenshot cap."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-12", ["public"], "Updates in the top bar",
     "Every update to the platform is listed on the What's new page. Find it under Updates in the top bar of the website."),
    ("2026-10-12", ["public"], "Lineup screenshots: one upload a day",
     "Each person can upload their lineup screenshots once a day, with up to 10 screenshots in one go "
     "(your Game Plan plus your players list). Choose them all together before uploading."),
    ("2026-10-12", ["superadmin"], "Backup Gemini keys and a daily screenshot cap",
     "Platform settings → AI:\n"
     "- Up to 5 Gemini keys. They take turns, and a busy or failing key hands over to the next one. "
     "Keys from the same Google project share one limit; for many users, turn on billing instead of adding free projects.\n"
     "- Screenshots per day for the whole platform: reading stops for the day once it's reached (0 = no cap).\n"
     "- Lineup uploads per person per day (default 1, each up to 10 screenshots), separate from AI logos per person per day."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0004_first_updates")]
    operations = [migrations.RunPython(add, remove)]
