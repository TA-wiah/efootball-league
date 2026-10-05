from django.db import migrations, models


def existing_accounts_keep_access(apps, schema_editor):
    # Everyone who existed before sign-ups opened was an editor of the league.
    apps.get_model("league", "Admin").objects.update(league_access=True)


class Migration(migrations.Migration):

    dependencies = [
        ("league", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="admin",
            name="league_access",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(existing_accounts_keep_access, migrations.RunPython.noop),
    ]
