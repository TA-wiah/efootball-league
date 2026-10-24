"""What's new: match reports, round-ups, share lines, descriptions and lineup captions written by the text AI."""
from datetime import date

from django.db import migrations

STAFF = ["owner", "admin", "organizer", "scorekeeper", "editor"]
UPDATES = [
    ("2026-10-24", ["public"], "Match reports and fresh share messages",
     "- Every played match now gets a short match report on its page, written from the result and the scorers.\n"
     "- Sharing a match or competition on WhatsApp now starts with a fresh line about it, then the invitation and the link.\n"
     "- Lineup flyers get a one-line caption too."),
    ("2026-10-24", STAFF, "Let the platform write for you",
     "- Match pages: the report is written when the result is in. Edit it, or tap \"Write again\". Your edits are kept.\n"
     "- Announcements: \"Draft a round-up\" writes the latest results, the top of the table and the next fixtures for the competition you pick. Check it, then post.\n"
     "- Competitions: \"Write it for me\" suggests a description from the name, type, format and rules.\n"
     "Only public match details are used, and nothing is invented: it writes from the results and scorers you entered."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0017_reader_chain")]
    operations = [migrations.RunPython(add, remove)]
