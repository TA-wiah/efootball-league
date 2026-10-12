"""The phone menu bar update is for everyone (it was listed under every role instead of as public)."""
from django.db import migrations


def fix(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title="A menu bar at the bottom on phones", author__isnull=True).update(audience=["public"])


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0008_share_tabs_ties")]
    operations = [migrations.RunPython(fix, migrations.RunPython.noop)]
