"""What's new: WhatsApp-first sharing with an invitation, the phone tab bar, shared places for ties, merged names shown."""
from datetime import date

from django.db import migrations

STAFF = ["owner", "admin", "organizer", "scorekeeper", "editor", "moderator", "viewer", "team_manager", "coach", "player"]
UPDATES = [
    ("2026-10-12", ["public"], "Share on WhatsApp, and invite your friends",
     "Every page now has one \"Share on WhatsApp\" button first, with Facebook, X, Telegram and Copy link under More. "
     "Each shared message invites people to join you on the platform to manage and play eFootball competitions."),
    ("2026-10-12", ["public"], "Top scorers: ties share a place, and merged names are shown",
     "- Players with the same goals and assists now share a place (=1) instead of one appearing ahead by alphabet.\n"
     "- When a player's goals were typed in different ways for the same team (like \"Messi\" and \"Lionel Messi\"), "
     "they count as one player, and the list now shows \"also typed as…\" so you can see why a total changed.\n"
     "- Friendlies don't count in the platform rankings and awards; they have their own ranking."),
    ("2026-10-12", STAFF, "A menu bar at the bottom on phones",
     "On a phone, the dashboard now has Home, Matches, Teams (or your Team) and Friendlies at the bottom of the screen, "
     "within thumb reach. More opens the full menu."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0007_critique_fixes")]
    operations = [migrations.RunPython(add, remove)]
