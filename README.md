# eFootball Champions League

Groups, fixtures, knockouts, top scorers, assists, Ballon d'Or and awards.
Plain HTML + JS front end, Node server, SQLite `.db` file. **No npm install needed.**

## Run it
1. Install Node.js **22.13 or newer** (https://nodejs.org).
2. Unzip, open a terminal in this folder, then run:

```
ADMIN_USER=boss ADMIN_PASSWORD=pickAStrongOne node server.js
```

(Windows PowerShell: `$env:ADMIN_USER="boss"; $env:ADMIN_PASSWORD="pickAStrongOne"; node server.js`)

3. Open http://localhost:3000

If you start it without ADMIN_PASSWORD, the first login is `admin` / `admin123`.
Change it straight away by restarting with ADMIN_USER and ADMIN_PASSWORD set.
Setting them again always resets the login.

## Using it
- **Everyone** sees tables, fixtures, knockouts, stats and awards (the page refreshes itself every 15 seconds).
- **Admin**: tap "Admin login" at the bottom of the page. Then you can enter scores, goals and assists,
  edit players and groups, set awards and reset. Everything saves automatically to `data/league.db`.
- Qualification rules (Players tab, top card): top 2 or top 3 of every group go through, and with "Top 2" you can add
  1 to N wildcards for the best third-placed teams (ranked by points per game, then goal difference per game, then goals per game).
  The bracket builds itself, with byes when the number of qualifiers isn't a power of 2.
- Ballon d'Or points: goal 3, assist 2, champion +10, runner-up +5. You can override the winner on the Awards tab.

## Hosting
- Port: set `PORT` (default 3000).
- Put it behind HTTPS (Caddy, Nginx, or a host like Render, Railway or a VPS). Behind HTTPS the login cookie
  becomes Secure automatically (or set `COOKIE_SECURE=1`).
- Keep it running with `pm2 start server.js` or a systemd service.
- **Back up `data/league.db`**: it holds all scores and the admin login. To use another path set `DB_FILE`.
- On hosts with a temporary disk (some free tiers) the .db can be wiped on restart, so use a persistent disk or volume.

## Files
- `server.js`: API and login (scrypt-hashed password, 7-day session cookie, login rate limit)
- `public/index.html`: the whole site
- `data/league.db`: SQLite database (tables: state, admin, sessions)
