# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

Mobile web first: most people arrive on a phone from a WhatsApp link. The organizer dashboard (`/app`) is a single-page app; the public site is server-rendered.

## Users

Served equally; neither comes second when needs conflict.

- **Organizers** (organization owners, admins, organizers, scorekeepers, editors, moderators) run leagues, cups and friendlies for their community or club: create competitions, schedule fixtures, approve results, manage teams and members, run the organization's public website, and pay for SMS credits.
- **Teams** (players, coaches, team managers) play in those competitions: follow their fixtures and standings, send results with game screenshots, arrange friendlies, keep their squad and lineup up to date, and watch the rankings and awards.
- **Visitors** follow public competitions, rankings and awards without signing in, usually from a shared link.
- **Platform admins** (super admins) run the whole platform: approvals, settings (email, SMS, payments, AI), What's new updates, support.

About 500 users on the platform today.

## Product Purpose

Run eFootball competitions end to end, from the draw to the trophy, for many organizations on one platform, and give every team and player a record that counts across all of them. Success: organizers spend their time on the community instead of spreadsheets, teams trust the results, and players care about their ranking.

## Positioning

- **Built for eFootball**: eFootball positions (GK, CB, LB, RB, DMF, CMF, LMF, RMF, AMF, LWF, RWF, SS, CF), card ratings, lineup and players-list screenshots read by AI, results proved with game screenshots, disconnects and forfeits as real outcomes.
- **Ghana and WhatsApp first**: phones first, links shared on WhatsApp, SMS reminders from each organization's credits, payments in GHS through PayNova.
- **Platform-wide rankings**: teams and players ranked across every organization (Elo for teams), with worldwide awards and a Team of the Season; friendlies have their own ranking.
- **Many organizations, one platform**: any league or club runs its own organization with its own members, roles and public website.

## Operating Context

- Matches are played in the eFootball game; the platform records them. Proof is a screenshot of the game's result screen, sent by either team and approved by the organization.
- Formats: leagues (with divisions, promotion and relegation), groups with knockouts (ranked draws, best third places), knockouts, friendlies (inside or across organizations), and the platform-run Pro League.
- Roles decide everything people see: team-only roles (player, coach, team manager) see only their team; staff roles see the organization; super admins see the platform.
- Communication: email (platform SMTP), SMS (per-organization credits, opt-in), WhatsApp links, in-app notifications, the What's new page (`/updates`).

## Capabilities and Constraints

- Django 5.2 app (`league`, `orgs`, `competitions`, `superadmin`, `payments`, `sms`, `proleague`); dashboard in `public/app.html` (vanilla JS); public pages in `templates/public/`. Strict Content-Security-Policy with script nonces on public pages.
- No emojis anywhere on the site; icons are inline SVG.
- Secrets (SMTP, SMS, PayNova, OpenAI and Gemini keys) never reach the browser.
- Organizations never see each other's private data; permissions are enforced on the server.
- AI is optional and switched on by platform admins; it never identifies people from faces.
- Name: undecided. "Competition Manager" is a placeholder; super admins can rename the site in Platform settings.

## Brand Commitments

- No real club badges, club names or brand marks in logos or design (avoid trademark trouble); the logo studio makes original crests, country flags and club-style badges.
- Plain, direct wording; every update is announced on What's new.

## Evidence on Hand

- Real competitions, teams, results and rankings live in the database; there are no testimonials, press or customer quotes, and none should be invented.
- Real eFootball screenshots supplied by the user were used to test lineup reading.

## Product Principles

1. **Trust the result.** Every score has proof and an approver; nothing changes silently.
2. **Each role sees its own world.** Show people what they can act on; hide what isn't theirs.
3. **Phone and WhatsApp are the main road.** Every flow must work one-handed on a small screen and survive being shared as a link.
4. **The record travels.** A team's and player's achievements count across every organization they play in.
5. **Tell people what changed.** Reminders go only to those involved, and every update lands on What's new.

## Accessibility & Inclusion

Many users are on low-cost Android phones and mobile data: keep pages light and readable on small screens and slow connections.
