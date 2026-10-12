"""What's new: the name a team plays as in eFootball, used to read result screens the right way round."""
from datetime import date

from django.db import migrations

UPDATES = [
    ("2026-10-12", ["public"], "Your eFootball team name",
     "On the platform your team may be \"Philiss\", but in eFootball you might play as Barcelona, Madrid or your own name, "
     "and that's what the result screen shows.\n"
     "- Save the name you play as on your team page (\"In eFootball we play as\"). Anyone on the team can change it.\n"
     "- When a result screenshot is read, the score and goals are put on the right team using that name.\n"
     "- If the names on the screen don't match, you tap which one was you. The name you pick is remembered if your team hasn't saved one."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0011_result_ai")]
    operations = [migrations.RunPython(add, remove)]
