from django.db import migrations


def promote(apps, schema_editor):
    # The owner of the original league is the person who runs the site: make them the platform super admin.
    apps.get_model("league", "Admin").objects.filter(role="owner", league_access=True).update(is_superuser=True, is_staff=True)


class Migration(migrations.Migration):
    dependencies = [("league", "0003_admin_last_seen_audit_device_audit_new_audit_old_and_more")]
    operations = [migrations.RunPython(promote, migrations.RunPython.noop)]
