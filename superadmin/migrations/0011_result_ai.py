"""What's new: sending results with AI-read scores and goals; goals added on confirm; automatic confirmation."""
from datetime import date

from django.db import migrations

STAFF = ["owner", "admin", "organizer", "scorekeeper"]
UPDATES = [
    ("2026-10-12", ["public"], "Send your result: the screenshot fills it in",
     "On My matches, a match that has kicked off now shows \"Send result\".\n"
     "- Add the full-time result screen from eFootball: the score and the scorers (with minutes, penalties, own goals and assists when the screen shows them) are read for you.\n"
     "- Check them, fix anything, add goals if you like, then send.\n"
     "- When the result is confirmed, the goals are added to the match, so the top scorers update by themselves."),
    ("2026-10-12", STAFF, "Results: goals added on confirm, and automatic confirmation",
     "- When you confirm a team's result, the goals they listed are added to the match (names that match the squad are linked).\n"
     "- New in Organization settings: \"Confirm results automatically when both teams agree\". When both teams send the same score, "
     "it's confirmed straight away; when they disagree, it still waits for you."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0010_lineup_quota_fix")]
    operations = [migrations.RunPython(add, remove)]
