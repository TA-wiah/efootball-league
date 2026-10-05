# eFootball Champions League

Groups, fixtures, knockouts, top scorers, assists, Ballon d'Or and awards.
Plain HTML + JS front end, Node server, SQLite `.db` file. **No npm install needed.**

## Where the league data lives
Everything (players, groups, scores, match details, awards, settings, draws and admins) is stored in the database
(`data/league.db`), and editors change it from the site. Nothing about your league is written in the code.
- `seed.json` is the **starting league**: the groups, player names and settings a brand-new database begins with.
  It is only read when the database is empty, and when an editor taps "Reset everything to the starting league".
  Edit it before your first start (1–8 groups named A–H, up to 6 names each). Set `SEED_FILE` to use another file.

## Settings (.env)
All settings can go in a `.env` file next to `server.js`. Copy `.env.example` to `.env`; every option is explained
inside. `.env` is ignored by git and Docker, so your passwords stay private. Variables set in the terminal win over the file.
Put `ADMIN_PASSWORD` in only to create the first login or reset a forgotten one, then remove it again.

## Run it
1. Install Node.js **22.13 or newer** (https://nodejs.org).
2. Unzip, open a terminal in this folder, then run:

```
ADMIN_USER=boss ADMIN_PASSWORD='a long password of your own' ADMIN_EMAIL=you@example.com node server.js
```

(Windows PowerShell: `$env:ADMIN_USER="boss"; $env:ADMIN_PASSWORD="a long password of your own"; $env:ADMIN_EMAIL="you@example.com"; node server.js`)

3. Open http://localhost:3000

If you start it the very first time without ADMIN_PASSWORD, a random one-time password for `admin` is printed in
the terminal, and you must choose a new password after logging in.
Setting ADMIN_USER + ADMIN_PASSWORD always resets that account's password (handy if you get locked out).
The first account becomes the **owner**. Databases from older versions keep their existing login, which becomes the owner.

## Using it
- **Everyone** sees tables, fixtures, knockouts, stats and awards (the page refreshes itself every 15 seconds).
- **Admin**: tap "Admin login" at the bottom of the page and sign in with your username **or** email. Then you can
  enter scores, goals and assists, edit players and groups, set awards and reset. Everything saves automatically to `data/league.db`.
- **Admins tab**:
  - **Add an admin**: enter a username and email. They get an email with a link to choose their own password (valid 48 hours,
    works once). If email isn't set up, the link is shown so you can send it yourself (WhatsApp etc.).
  - Pending invites can be re-sent. The **owner** can remove any admin; other admins can cancel invites they sent or leave.
  - My account: set your email (needed for password reset), change password, log out on all devices, send a test email.
  - The owner also sees a recent-activity log (logins, failed logins, invites, edits).
- **Qualification rules** (Players tab, top card):
  - Top 2 or Top 3 of every group go through.
  - With "Top 2" you can add 1 to N wildcards for the best third-placed teams, World Cup style: all thirds are ranked
    together by points per game, then goal difference per game, then goals per game (fair when groups have 4 and 5 players).
  - **Minimum points for a 3rd-placed team** (e.g. 5): a third only goes through if it reaches that many points. With
    wildcards, the best N thirds *that reach the mark* go through; in "Top 3" mode each group's 3rd needs the mark.
    Empty spots become byes. Thirds below the mark are shown faded.
  - The bracket builds itself, with byes when the number of qualifiers isn't a power of 2.
- **Match details**: in admin mode every match (group games, both knockout legs, the final) has a "Match details" button.
  Log each goal with the minute, the in-game scorer and an optional assist. Everyone sees them under the match, and they
  count automatically in Stats (plus an "In-game top scorers" board). The Stats boxes are for extra goals/assists only.
- **Undo**: the floating Undo button (or Ctrl+Z) takes back your last changes, up to 60 steps.
- **Site settings** (Admins tab): league title, subtitle and the login button text (default "Organizer sign in").
- Ballon d'Or points: goal 3, assist 2, champion +10, runner-up +5. You can override the winner on the Awards tab.
- Animations respect the device's "reduce motion" setting.

## Email (SMTP)
Set these when starting the server (no extra packages needed):

| Variable | Example | Notes |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | your mail provider's SMTP server |
| `SMTP_PORT` | `587` | `587` = STARTTLS, `465` = TLS |
| `SMTP_USER` | `league@gmail.com` | login for the SMTP server |
| `SMTP_PASS` | `abcd efgh ijkl mnop` | for Gmail use an **App Password**, not your normal password |
| `SMTP_FROM` | `League <league@gmail.com>` | sender shown in the email (defaults to SMTP_USER) |
| `APP_URL` | `https://league.example.com` | public address of the site, used in emailed links. **Required for "Forgot password".** |

The connection is always encrypted; the server refuses to send the password over a plain connection.
After starting, open Admins → "Send me a test email" to check it works.

## Security
- Passwords are hashed with scrypt; minimum 10 characters.
- Login: 5 wrong tries locks that account for 15 minutes, 10 wrong tries from one IP blocks that IP for 15 minutes.
  The error never reveals whether a username exists.
- Sessions: random token in an HttpOnly, SameSite=Strict cookie (stored hashed in the database), 7 days, and every change
  needs a per-session CSRF token plus a same-origin check. Changing a password logs out other devices.
- Invite and reset links are single-use, expire (48 h / 1 h) and are stored hashed.
- Strict Content-Security-Policy (with a nonce), no framing, and the saved league data is validated on the server.

## Fair random draws
- **Group draw** (Players tab): list the players, pick the number of groups and run the draw. The **server** shuffles
  them with Node's cryptographic random generator (`crypto.randomInt`), so the organizer can't pick or predict groups.
  A player's place in the group also decides the fixture order. Optional **pots** keep the strongest players apart.
- **Knockout draw** (Knockouts tab, once every group game has a score): winners, runners-up and thirds are each shuffled
  by the server, and players from the same group are kept apart in the first round. It can't be redrawn unless the
  qualification rules change, and the server refuses any other attempt to change the pairings.
- Every draw is stored in the `draws` table and shown to everyone in the **Draw** tab (time, who started it, result,
  replay). Draws can't be edited or deleted, so a redo is visible to everybody.
- Prefer fixed pairings? Set "Knockout pairings" to "Fixed by group position" in the qualification card.

## Hosting (putting it online)
Whatever host you pick, it needs **a persistent disk** for `league.db`, otherwise the league is wiped when the
server restarts. After it's online, set `APP_URL` to the address, `TRUST_PROXY=1`, and your SMTP settings.

**Option 1: Railway (easiest)**
1. Put this folder in a GitHub repository (without `data/league.db`).
2. On railway.com: New Project → Deploy from GitHub repo. It finds the `Dockerfile` by itself.
3. Add a **Volume** to the service, mounted at `/data`.
4. In Variables add `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_EMAIL` (first start only), `APP_URL`, and the `SMTP_*` settings.
5. Settings → Networking → Generate Domain (or add your own domain). HTTPS is automatic.

**Option 2: Fly.io**: `fly launch` (uses the Dockerfile), then `fly volumes create data --size 1` and mount it at `/data`
in `fly.toml` (`[mounts] source="data" destination="/data"`), set secrets with `fly secrets set`, and `fly deploy`.

**Option 3: Render**: New → Web Service from your repo (Docker). Add a **Persistent Disk** mounted at `/data`
(needs a paid instance; the free one has no disk and loses data).

**Option 4: your own VPS** (DigitalOcean, Hetzner, Contabo, a free Oracle Cloud VM…):
1. Install Node.js 22.13+ and copy this folder to the server.
2. Run it with pm2: `ADMIN_USER=boss ADMIN_PASSWORD='…' APP_URL=https://league.example.com TRUST_PROXY=1 pm2 start server.js`
   then `pm2 save` and `pm2 startup` so it survives reboots.
3. Install **Caddy** and use this `Caddyfile`; it gets the HTTPS certificate for you:
   ```
   league.example.com {
       reverse_proxy localhost:3000
   }
   ```
4. Point your domain's DNS A record at the server's IP.

Settings that matter online:
- `PORT` (default 3000). Hosts like Railway set it for you.
- `APP_URL`: the public https address. Used in emails and turns on Secure cookies.
- `TRUST_PROXY=1`: when behind a proxy/host (all the options above), so rate limits see the real visitor IP.
  Don't set it when the server is exposed directly, or visitors could fake their IP.
- **Your data lives in `league.db`** (`data/league.db`, or `/data/league.db` in Docker; set `DB_FILE` to change it).
  Updating the code never resets it: the server only adds missing tables and keeps what's there.
  Just don't overwrite or delete the data folder when you copy a new version in.
- **Automatic backups**: on every start and once a day the server saves a copy in a `backups` folder next to the
  database (newest 14 kept; set `BACKUP_DIR` to change the folder). To restore: stop the server, copy a backup over
  `league.db`, start again. Keep extra copies somewhere else too, and don't commit the database to a public repository.

## Forgot the admin password?
Passwords are stored only as a one-way hash, so nobody can read them, not even the server owner. To reset one,
restart the server with that username and a new password, e.g. `ADMIN_USER=boss ADMIN_PASSWORD='new long password'`.
Admins with an email address can also use "Forgot password?" on the login card (needs SMTP and `APP_URL`).

## Several editors at once
Every save carries the version it was based on. If two editors change the league at the same moment, the second save
is refused instead of silently overwriting the first. That editor gets the latest version and a message to redo the
change. Editors who are just looking get other editors' changes automatically within a few seconds.

## Checks
- `npm test` starts the real server on a throwaway database and checks login, lockouts, CSRF protection, version
  conflicts, data validation, invites and the draws. Run it after every change, before you deploy.
- `GET /api/health` answers `{"ok":true}` when the server and database are working. Point your host's health check
  at it (the Dockerfile already does).

## Files
- `server.js`: API, admin accounts, invites, password reset, SMTP client, rate limits
- `public/index.html`: the whole site
- `data/league.db`: SQLite database (tables: state, admins, sessions, tokens, draws, audit)
- `Dockerfile`: for hosts that run containers (mount a volume at `/data`)
- `test/api.test.js`: automated tests (`npm test`)
- `.env.example`: every setting explained; copy it to `.env`
- `seed.json`: the starting league for a new database
