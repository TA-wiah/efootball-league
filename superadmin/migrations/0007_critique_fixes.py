"""What's new: room codes, clearer results, phone fixes, safer result approval and a calmer dashboard."""
from datetime import date

from django.db import migrations

STAFF = ["owner", "admin", "organizer", "scorekeeper", "editor", "moderator", "viewer"]
RESULTS = ["owner", "admin", "organizer", "scorekeeper"]
UPDATES = [
    ("2026-10-12", ["public"], "Room codes for your matches",
     "Add the eFootball room code to a friendly challenge, or to any match on its match page, and the other team can copy it with one tap.\n"
     "- Either team can add or change the code, for example once the host has made the room.\n"
     "- Only the two teams and the organizers see it; it never appears on public pages."),
    ("2026-10-12", ["public"], "Clearer results on match pages",
     "- Matches that have kicked off but have no score yet now say \"Awaiting result\".\n"
     "- Confirmed results say who confirmed them, and when a result screenshot backs them up.\n"
     "- When some goals have no scorer yet, the match page says so."),
    ("2026-10-12", ["public"], "Better on phones",
     "- Team and competition pages no longer scroll sideways on a phone.\n"
     "- Your squad shows one card per player on a phone, with bigger buttons.\n"
     "- Uploading lineup screenshots is one big button, with progress as each screenshot is read.\n"
     "- The home page now explains the platform: eFootball, results from screenshots, rankings and WhatsApp."),
    ("2026-10-12", RESULTS, "Approving results: one check before it counts",
     "Before a team's result is used, you now see exactly what will happen (the score, and that the table and rankings update) "
     "and confirm it. Deleting a goal or card takes two taps, and the match page warns when more goals are logged than the score."),
    ("2026-10-12", STAFF, "A calmer dashboard",
     "Your dashboard now opens with what needs you: results to approve, then friendlies to answer, then matches waiting for a score. "
     "Squads to complete are one line you can open, and the tiles are down to the four that matter."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0006_lineup_reading_fixed")]
    operations = [migrations.RunPython(add, remove)]
