from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    apps.get_model("competitions", "Match").objects.filter(status="finished", finished_at__isnull=True).update(finished_at=F("updated"))


class Migration(migrations.Migration):
    dependencies = [("competitions", "0003_competition_suspended_match_finished_at_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
