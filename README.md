# eFootball Champions League

Groups, fixtures, knockouts, top scorers, assists, Ballon d'Or and awards.
**Python / Django** server, a single-page front end (`public/index.html`), and a Postgres or SQLite database.

## Where the league data lives
Everything (players, groups, scores, match details, awards, settings, draws and admins) is stored in the database,
and editors change it from the site. Nothing about your league is written in the code.
- **Database**: Postgres when `DATABASE_URL` is set (recommended online), otherwise a local SQLite file
  (`data/league.sqlite3`).
- `seed.json` is the **starting league**: the groups, player names and settings a brand-new database begins with.
  It is only read when the database is empty, and when an editor taps "Reset everything to the starting league".
  Edit it before your first start (1–8 groups named A–H, up to 6 names each). Set `SEED_FILE` to use another file.

## Run it on your computer
1. Install **Python 3.12** (https://python.org).
2. Open a terminal in this folder:

```
python -m venv .venv
.venv\Scripts\activate          (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          (Mac/Linux: cp .env.example .env)
```

3. In `.env`, set `ADMIN_USER` and `ADMIN_PASSWORD` (10+ characters), then:

```
python manage.py migrate
python manage.py ensure_admin
python manage.py runserver 3000
```

4. Open http://localhost:3000 and log in. Then remove `ADMIN_PASSWORD` from `.env`.

Without `ADMIN_PASSWORD`, the very first start creates a user `admin` with a random one-time password, printed by
`ensure_admin`; you must choose a new password after logging in. The first account becomes the **owner**.

## Put it online
The host's disk is often wiped on restart (free Koyeb wipes it whenever the app sleeps). So use a **Postgres database**
for the league, for example a free one from **Neon** (neon.tech, no credit card):

1. **Neon**: sign up → create a project → copy the **connection string**
   (`postgresql://…neon.tech/…?sslmode=require`).
2. **Email (optional)**: sign up at **Brevo** (brevo.com, free 300 emails/day) → add and verify your sender email →
   create an **API key**. Brevo sends over HTTPS, so it works on hosts that block email ports.
3. **Your host** (Koyeb, Railway, Render, Fly…): deploy this repository with the **Dockerfile**, port **3000**,
   health check path **`/api/health`**, and these environment variables:

| Name | Value |
|---|---|
| `SECRET_KEY` | a long random string: `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `APP_URL` | your site's address, e.g. `https://your-app.koyeb.app` |
| `DATABASE_URL` | the Neon connection string |
| `ADMIN_USER` / `ADMIN_PASSWORD` | your login (10+ characters). Remove `ADMIN_PASSWORD` after the first login. |
| `ADMIN_EMAIL` | your email (for password resets) |
| `EMAIL_PROVIDER` / `EMAIL_API_KEY` / `EMAIL_FROM` | `brevo`, your Brevo API key, `League <you@gmail.com>` |

`TRUST_PROXY=1` is already set in the Dockerfile. Every start runs the database migrations and `ensure_admin`
automatically (`start.sh`), then serves the site with gunicorn. Without Docker, hosts that read the `Procfile` do the same.

## Using it
- **Everyone** sees tables, fixtures, knockouts, stats, awards and the current draw (the page refreshes itself).
- **Admins** tap the login button at the bottom of the page and sign in with username **or** email. Then they can
  enter scores, goals and assists, edit players and groups, set awards and reset. Everything saves automatically.
  The **Guide** tab explains every step for new editors.
- **Admins tab**: invite admins by email (or share the link if email isn't set up), re-send or cancel invites
  (the **owner** can remove anyone), set your email, change your password, log out on all devices, send a test email,
  and edit the site title, subtitle and login button text. The owner also sees a recent-activity log.
- **Qualification** (Players tab): Top 2 or Top 3 per group; with Top 2, optional wildcards for the best third-placed
  teams (ranked World Cup style by points, goal difference and goals per game) and an optional **minimum points** a
  third needs. The bracket builds itself, with byes when needed.
- **Match details**: log each goal with minute, in-game scorer and assist; they count automatically in Stats.
- **Undo** (button or Ctrl+Z) takes back your last changes.
- Ballon d'Or points: goal 3, assist 2, champion +10, runner-up +5.

## The platform (/app): organizations, members and invitations
Anyone can sign up at **/app** and create an **organization** (a league, school, club, academy or company). One account
can belong to many organizations with a different role in each, and each organization's data is private to its members.

| Role | What they can do |
|---|---|
| **Owner** | Everything, including settings, transferring ownership and deleting the organization |
| **Organizer** | Run competitions (teams, fixtures, results, tables) and manage admins, editors, moderators and viewers |
| **Admin** | Like an organizer, but can only manage editors, moderators and viewers |
| **Editor** | Update scores, matches, team information and competition content |
| **Moderator** | Look after published content, match information, teams and users |
| **Viewer** | Read-only access |

- **Invitations**: by email or as a shareable link, with a role. They're single-use, expire after 7 days, and show as
  pending, accepted, expired or revoked. Email invitations can only be accepted by the account with that email.
- People can only give roles below their own; ownership moves only through "Transfer ownership".
- Every action is checked **on the server** (`orgs/permissions.py` is the single list of who can do what). Non-members
  get "not found" for an organization, so private organizations stay invisible.
- The original league site at `/` keeps working; only its existing editors can edit it.

### Competitions (inside an organization)
One generic system runs any competition: a Champions League, a school cup or a Sunday league. Each competition sets:
name, logo, description, country/region, season, type, dates, visibility (public / unlisted / private), status, format
(league, groups then knockouts, or knockout only), points for a win/draw/loss, tie-breakers in order (goal difference,
goals scored, fewest conceded, wins, away goals, head-to-head points / goal difference / goals), how often teams meet,
maximum teams and how many qualify from each group.

- **Teams and players** belong to the organization and can play in several competitions. Logos are stored in the
  database (PNG/JPG/GIF/WebP up to 256 KB; SVG is refused because it can carry scripts).
- **Fixtures**: generated automatically (everyone plays everyone, once or home and away, per group), shuffled by the
  server, with dates spaced from a first kick-off. Knockout rounds are drawn at random between the teams you tick.
  Single matches can also be added by hand. Fixtures with results can't be regenerated.
- **Results**: score, penalties, status, kick-off, venue, referee and match events (goals, cards, substitutions)
  with players from the squad or typed names. Tables, form and top scorers update automatically.
- **Points adjustments** (e.g. −3 for a sanction) and **announcements** (pinned, hidden, per competition).
- Who can do what: organizers/admins build competitions and fixtures; editors enter results, update teams and
  content; moderators hide or delete announcements; viewers read. All checked on the server.
- **Bring your original league in**: `python manage.py import_league <organization-address>` copies its groups,
  teams, group fixtures (same order as the old page), results and logged goals into a new competition.
  Knockout rounds aren't copied; draw them again once the group stage is complete.

### Public pages (no login needed)
Every competition gets its own website, rendered on the server so WhatsApp, Facebook, X and Telegram show a proper
preview and search engines can index it:

| Address | What's there |
|---|---|
| `/competition/{slug}` | logo, name, description, season, table, upcoming matches, recent results, top scorers, news, facts and rules |
| `/competition/{slug}/table` | the shareable league table (position, played, won, drawn, lost, goals for/against, goal difference, points, form) |
| `/competition/{slug}/fixtures`, `/results`, `/teams` | fixtures by round, results by day, all teams |
| `/match/{slug}` | e.g. `/match/kasoa-stars-vs-winneba-lions-2026-10-11`: teams, score, status, date and kick-off (in each visitor's time zone), venue, referee, goals, cards, substitutions, aggregate over two legs, the group table and the rest of the round |
| `/team/{slug}` | form, upcoming matches, results, competitions and squad |
| `/organization/{slug}` | its public competitions, teams, upcoming matches, results and news |

- Every page has **Copy public link**, **Share** (on phones) and WhatsApp / Facebook / X / Telegram buttons, and
  the dashboard has "Public page" and "Copy public link" buttons wherever something is public.
- **Visibility**: public pages are listed and indexed; **unlisted** ones work for anyone with the link but are
  marked "noindex" and never listed; **private** ones return "not found" except to the organization's members, who
  see a marked preview. A team gets a public page once it plays in a public competition.
- `/league/{slug}/…` links redirect to `/competition/{slug}/…`. `/sitemap.xml` and `/robots.txt` are generated.
- Set `TIME_ZONE` (e.g. `Africa/Accra`) to group results by your local day. Visitors always see times in their own zone.

## Fair random draws
- **Group draw** (Players tab): the **server** shuffles the players into groups with the operating system's
  cryptographic random generator, so the organizer can't pick or predict groups. A player's place in the group also
  sets the fixture order. Optional **pots** keep the strongest players apart. Running a draw clears all scores.
- **Knockout draw** (Knockouts tab, when every group game has a score): winners, runners-up and thirds are each
  shuffled by the server; players from the same group are kept apart in the first round. It can't be redone unless the
  rules change, and the server refuses any other attempt to change the pairings.
- The **Draw** tab shows the current draw, who started it (Owner/Admin badge) and when, with a replay.
- Prefer fixed pairings? Set "Knockout pairings" to "Fixed by group position".

## Several editors at once
Every save carries the version it was based on. If two editors save at the same moment, the second save is refused
instead of silently overwriting the first; that editor gets the latest version and a message to redo the change.
Editors who are only looking receive other editors' changes automatically within a few seconds.

## Security
- Passwords hashed with scrypt; minimum 10 characters; common and username-like passwords refused.
- Login: 5 wrong tries lock that account for 15 minutes; 10 wrong tries from one IP block it for 15 minutes.
  The error never reveals whether a username exists.
- Sessions: HttpOnly, SameSite=Strict cookies, 7 days; every change needs Django's CSRF token and a same-origin check.
  Changing a password logs out other devices.
- Invite and reset links are single-use, expire (48 h / 1 h) and are stored hashed.
- Strict Content-Security-Policy with a nonce, no framing, HSTS on https, and league data is validated on the server.

## Forgot the admin password?
Nobody can read a password: only a one-way hash is stored. Set `ADMIN_USER` and a new `ADMIN_PASSWORD` on your host
(or in `.env`) and restart. Admins with an email address can also use "Forgot password?" (needs email and `APP_URL`).

## Backups
- With Neon (Postgres), Neon keeps its own history for restoring.
- `python manage.py backup` writes the league and draws to a JSON file in `data/backups/` (newest 14 kept).
- Keep extra copies somewhere else, and never commit the database or `.env` to git.

## Checks
- `python manage.py test` runs the automated tests (login, lockouts, CSRF, two editors, validation, draws, invites,
  email, and every organization role and invitation state). Run it after every change, before you deploy.
- `GET /api/health` answers `{"ok": true}` when the server and database are working.

## Files
- `competitions/public.py` + `templates/public/`: the public pages
- `competitions/`: competitions, teams, players, matches: `models.py`, `engine.py` (fixtures, standings, tie-breakers), `api.py`, `tests.py`, `management/commands/import_league.py`
- `orgs/`: the platform: `models.py` (organizations, memberships, invitations), `permissions.py` (roles), `api.py`, `tests.py`
- `public/app.html`: the platform pages (sign-up, dashboards, members, invitations)
- `league/`: the original league app: `views.py` (the API), `logic.py` (rules, draws, validation), `models.py` (database tables),
  `emailer.py` (Brevo/Resend/SMTP), `management/commands/` (`ensure_admin`, `backup`), `tests.py`
- `league_site/settings.py`: all settings, read from environment variables / `.env`
- `public/index.html`: the whole page (HTML, CSS and the JavaScript that runs in the browser)
- `seed.json`: the starting league for a new database
- `start.sh`, `Dockerfile`, `Procfile`: how hosts start the site
- `.env.example`: every setting explained; copy it to `.env`
