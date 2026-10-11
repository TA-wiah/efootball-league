from django.db import migrations


def friendlies_out(apps, schema_editor):
    """Friendlies now stay out of the rankings by default (they have their own friendly tables)."""
    PlatformSetting = apps.get_model("superadmin", "PlatformSetting")
    row = PlatformSetting.objects.filter(key="rankings").first()
    if row and (row.value or {}).get("friendly_weight") == "0.5":
        row.value = {**row.value, "friendly_weight": "0"}
        row.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0001_initial")]
    operations = [migrations.RunPython(friendlies_out, migrations.RunPython.noop)]
