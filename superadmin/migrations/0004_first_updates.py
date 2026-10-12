"""The first What's new entries: the updates made so far (super admins can edit or remove them in the admin panel)."""
from datetime import date

from django.db import migrations

STAFF = ["owner", "admin", "organizer"]
RESULTS = ["owner", "admin", "organizer", "scorekeeper"]
UPDATES = [
    ("2026-10-10", ["public"], "Friendlies: find a team and play",
     "Arrange friendlies with teams in your organization or in other organizations.\n"
     "- Find a friendly: look for teams and send a challenge, or post that your team is looking for a game.\n"
     "- Team managers and coaches accept or decline challenges sent to their team, from their dashboard.\n"
     "- Friendlies have their own table and their own ranking. They never change league standings.\n"
     "- Messages about a friendly go only to the two teams playing it."),
    ("2026-10-10", ["public"], "Your team's own view for players, coaches and team managers",
     "Players, coaches and team managers see their own team: fixtures, results, standings and rankings.\n"
     "- Send your match result with a screenshot of the result screen. The organization checks it and approves it.\n"
     "- You can't edit a sent result. If you typed the wrong score, delete your screenshot and send it again before it's approved."),
    ("2026-10-10", STAFF, "Results sent by teams, approved by you",
     "Teams now send their own results with a screenshot. You see them waiting on your dashboard and approve them; "
     "only then do they count in standings and rankings."),
    ("2026-10-10", STAFF, "Leagues with promotion and relegation",
     "When you create a competition you can now run a league with divisions, promotion and relegation places, and start the next season from the final tables."),
    ("2026-10-11", ["public"], "Rankings, awards and the Team of the Season",
     "- Team and player rankings across the whole platform, with your world, country and region places on your dashboard.\n"
     "- Awards: Player of the Year, Golden Boot, Best Playmaker, Best Defence, Team of the Year and Fair Play.\n"
     "- The Team of the Season: the year's best XI on a pitch.\n"
     "- Friendlies don't count in the main rankings."),
    ("2026-10-11", STAFF, "Reminders go to each team only",
     "Match reminders, friendly requests and result confirmations by email and text message now go only to the teams involved. "
     "Text messages are sent when your organization has turned on automatic text messages, and use your SMS credits."),
    ("2026-10-11", ["public"], "Design your team's logo",
     "The logo studio can design a crest, use a country flag, pick a club-style badge, or generate ideas from your team's name."),
    ("2026-10-12", ["public"], "The same player, however the name is typed",
     "\"Messi\", \"Lionel Messi\" and \"Messi Lionel\" for the same team now count as one player in the scorers, the rankings and the awards. "
     "A player with the same name on another team stays a different player."),
    ("2026-10-12", RESULTS, "Entering goals: no more duplicate names",
     "- The name box suggests your team's squad and the names already used for its goals.\n"
     "- A typed name that matches a squad player is linked to that player, and the form shows the name it will be saved as.\n"
     "- A new name can be added to the squad with a position, straight from the goal form.\n"
     "- The team page lists names that look like the same player, so you can tidy them for good."),
    ("2026-10-12", ["public"], "eFootball positions",
     "Squads now use eFootball positions: GK, CB, LB, RB, DMF, CMF, LMF, RMF, AMF, LWF, RWF, SS and CF.\n"
     "- Players can set their own squad's positions.\n"
     "- When a position isn't set, one is worked out from real data when it's clear (the same player's position on other teams, "
     "or clearly a scorer or a provider) and marked \"auto\". A position someone sets always wins.\n"
     "- Positions decide who makes the Team of the Season."),
    ("2026-10-12", ["public"], "Awards and the Team of the Season on phones",
     "On a phone the awards show three to a row, and the pitch turns sideways so the whole XI fits on one screen."),
    ("2026-10-12", ["public"], "Fill your squad from screenshots",
     "On your team page, upload up to 10 eFootball screenshots at once: your Game Plan (who starts and who's on the bench) "
     "and your players list (it shows the names).\n"
     "- Positions, ratings, the starting XI and the subs are read for you, and names from the players list are matched to your lineup.\n"
     "- You check the list before saving. Next time, players already in your squad are recognised.\n"
     "- Teams in a competition get a \"Complete your squad\" reminder until they have a goalkeeper and 11 players with positions."),
    ("2026-10-12", ["superadmin"], "AI for lineup screenshots: OpenAI or Google Gemini",
     "Platform settings → AI: reading lineup screenshots can use OpenAI or Google Gemini (free tier). "
     "The daily limit applies per person; each use can be up to 10 screenshots."),
]


def add(apps, schema_editor):
    PlatformUpdate = apps.get_model("superadmin", "PlatformUpdate")
    for day, audience, title, body in UPDATES:
        PlatformUpdate.objects.create(date=date.fromisoformat(day), audience=audience, title=title, body=body, published=True)


def remove(apps, schema_editor):
    apps.get_model("superadmin", "PlatformUpdate").objects.filter(title__in=[u[2] for u in UPDATES], author__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0003_platform_updates")]
    operations = [migrations.RunPython(add, remove)]
