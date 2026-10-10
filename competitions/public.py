"""Public, shareable pages rendered on the server (so WhatsApp/X/Facebook previews and search engines see them).

Visibility:  public   → listed, indexed, shareable
             unlisted → works for anyone with the link, but "noindex" and never listed elsewhere
             private  → 404 for everyone except the organization's members (who see a marked preview)
"""
import re
import secrets
from collections import OrderedDict

from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from league.http import base_url, session_user
from orgs.models import Membership, Organization

from . import bracket, engine
from .models import EVENT_KINDS, MATCH_STATUS, POSITIONS, Announcement, Competition, Entry, Match, MatchEvent, Player, Team

STATUS_L = dict(MATCH_STATUS) | {"finished": "Full time"}
EVENT_L = dict(EVENT_KINDS)
EVENT_ICON = {"goal": "goal", "penalty_goal": "goal", "own_goal": "goal", "yellow": "yellow", "second_yellow": "red", "red": "red", "substitution": "sub"}
POS_L = dict(POSITIONS)
FORMAT_L = {"league": "League", "groups_knockout": "Groups and knockouts", "knockout": "Knockout"}
KIND_L = {"league": "League", "tournament": "Tournament", "cup": "Cup", "championship": "Championship", "friendly": "Friendly series", "other": "Competition"}


# ---------- helpers ----------
def listed(prefix=""):
    """What may appear publicly: public visibility, not suspended, and an organization in good standing."""
    return Q(**{f"{prefix}visibility": "public", f"{prefix}suspended": False, f"{prefix}org__status": "active"})


def is_member(request, org_id):
    """Members of the organization, and platform super admins, may preview things that aren't public."""
    user = session_user(request)
    return bool(user and (user.is_superuser or Membership.objects.filter(org_id=org_id, user=user).exists()))


def page(request, template, ctx, *, index=True, private=False, banner=None):
    """`private`: never cached or indexed. `banner`: show the members-only preview notice (defaults to `private`)."""
    nonce = secrets.token_urlsafe(16)
    base = base_url(request)
    from .rankings import settings_ as ranking_settings
    from .reports import settle_overdue
    settle_overdue()
    ctx.setdefault("rankings_on", ranking_settings()["enabled"])
    from superadmin import store
    site = store.site()
    ctx.setdefault("description", site["description"])
    ctx.update(site_name=site["name"] or "Competition Manager", site_description=site["description"],
               share=ctx.get("share_image") or ctx.get("logo") or store.site_logo_url())
    ctx.update(nonce=nonce, base=base, url=base + request.path, index=index and not private, private_preview=private if banner is None else banner,
               app_url="/app")
    resp = render(request, f"public/{template}", ctx)
    resp["Content-Security-Policy"] = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                                       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                                       "form-action 'self'; frame-ancestors 'none'")
    # Visitors get a short cache; logged-in people (organizers checking their changes) always see the latest version.
    resp["Cache-Control"] = "private, no-store" if private or session_user(request) else "public, max-age=60"
    if not ctx["index"]:
        resp["X-Robots-Tag"] = "noindex"
    return resp


def competition_for(request, slug):
    c = Competition.objects.select_related("org").filter(slug=slug).first()
    if not c:
        raise Http404
    c.hidden = c.visibility == "private" or c.suspended or c.org.status != "active"
    if c.hidden and not is_member(request, c.org_id):
        raise Http404
    return c


def logo(kind, obj):
    return f"/media/{kind}/{obj.id}/logo?v={obj.version}" if obj.data else None


def org_logo(org):
    return f"/media/org/{org.id}/logo?v={org.logo_version}" if org.logo_data else None


def public_team_ids(team_ids):
    """Teams that get their own public page: those playing in at least one public competition."""
    return set(Entry.objects.filter(listed("competition__"), team_id__in=team_ids, team__suspended=False).values_list("team_id", flat=True))


def team_view(t, linkable):
    if not t:
        return None
    return {"name": t.name, "short": t.short_name or t.name, "slug": t.slug, "logo": logo("team", t), "link": t.id in linkable,
            "initials": "".join(ch for ch in (t.short_name or t.name) if ch.isalnum())[:3].upper(), "hue": sum(map(ord, t.name)) * 31 % 360}


def match_view(m, linkable):
    home, away = m.home.team if m.home else None, m.away.team if m.away else None
    return {"slug": m.slug, "home": team_view(home, linkable), "away": team_view(away, linkable), "kickoff": m.kickoff,
            "round": m.round_name or f"Round {m.round}", "group": m.group, "leg": m.leg, "stage": m.stage, "status": m.status,
            "status_label": STATUS_L[m.status], "played": m.home_score is not None and m.away_score is not None, "notes": m.notes,
            "home_label": bracket.label(m.home_from) or "To be decided", "away_label": bracket.label(m.away_from) or "To be decided",
            "hs": m.home_score, "as": m.away_score, "hp": m.home_pens, "ap": m.away_pens, "venue": m.venue,
            "competition": {"name": m.competition.name, "slug": m.competition.slug}}


def bracket_rounds(ko, linkable):
    """[(round name, pairs)]: each pair holds the two ties that meet in the next round, each tie its legs. The page
    draws the connector lines between them."""
    out = []
    for name, games in group_by(ko, lambda m: m.round_name or f"Round {m.round}"):
        ties = [legs for _, legs in group_by(games, lambda m: m.slot or f"m{m.id}")]
        views = [[match_view(m, linkable) for m in tie] for tie in ties]
        out.append((name, [views[i:i + 2] for i in range(0, len(views), 2)]))
    return out


def group_by(items, key):
    out = OrderedDict()
    for it in items:
        out.setdefault(key(it), []).append(it)
    return list(out.items())


def day_label(m):
    if not m.kickoff:
        return "Date to be set"
    return timezone.localtime(m.kickoff).strftime("%A %d %B %Y")


def standings_view(c, linkable, only_group=None):
    entries = list(c.entries.select_related("team"))
    played = list(c.matches.filter(stage="league", status="finished"))
    groups = []
    tables = {g: engine.standings(c, [e for e in entries if e.group == g], played) for g in sorted({e.group for e in entries})}
    mk = engine.marks(c, tables)                                  # best thirds are ranked across all groups
    ko = c.format == "groups_knockout"
    for g, rows in tables.items():
        if only_group is not None and g != only_group:
            continue
        for r in rows:
            r["team"] = team_view(r["team"], linkable)
            r["qualifies"], r["wildcard"], r["short"] = (mk.get(r["entryId"]) == k for k in ("q", "wc", "short"))
        thirds = engine.thirds_for(c, len(tables))
        groups.append({"name": g, "rows": rows, "through": c.qualifiers_per_group if ko else 0, "thirds": thirds,
                       "min3": c.third_min_points if ko and (c.qualifiers_per_group >= 3 or thirds) else 0})
    return groups


MATCHES = Match.objects.select_related("competition", "home__team", "away__team")


# ---------- competition ----------
TABS = [("", "Overview"), ("table", "Table"), ("fixtures", "Fixtures"), ("results", "Results"), ("knockouts", "Knockouts"), ("teams", "Teams")]


def competition(request, slug, tab=""):
    c = competition_for(request, slug)
    if tab not in dict(TABS) or (tab == "knockouts" and c.format == "league") or (tab == "table" and c.kind == "friendly"):
        raise Http404
    entries = list(c.entries.select_related("team").order_by("team__name"))
    linkable = public_team_ids([e.team_id for e in entries])
    matches = list(MATCHES.filter(competition=c).order_by("kickoff", "round", "leg", "id"))
    upcoming = [m for m in matches if m.status in ("scheduled", "live", "postponed")]
    results = sorted([m for m in matches if m.status == "finished"], key=lambda m: (m.kickoff is not None, m.kickoff, m.round), reverse=True)
    has_table = c.format != "knockout" and c.kind != "friendly"
    ctx = {"c": c, "tab": tab, "tabs": [(t, label) for t, label in TABS if (t != "table" or has_table) and (t != "knockouts" or c.format != "league")],
           "logo": logo("competition", c), "share_image": logo("competition", c) or org_logo(c.org),
           "kind": KIND_L.get(c.kind, "Competition"), "format": FORMAT_L[c.format], "has_table": has_table,
           "team_count": len(entries), "played": len(results), "total": len(matches),
           "points": f"Win {c.points_win} · Draw {c.points_draw} · Loss {c.points_loss}",
           "tiebreakers": [engine.CRITERIA[k] for k in engine.clean_tiebreakers(c.tiebreakers or engine.DEFAULT_TIEBREAKERS)]}
    if tab in ("", "table") and has_table:
        ctx["groups"] = standings_view(c, linkable)
    if tab == "" and c.kind == "friendly":
        ctx["h2h"] = [{**p, "a": team_view(p["a"], linkable), "b": team_view(p["b"], linkable)} for p in engine.head_to_head(matches)]
    if tab == "":
        ctx["upcoming"] = [match_view(m, linkable) for m in upcoming[:6]]
        ctx["recent"] = [match_view(m, linkable) for m in results[:6]]
        events = MatchEvent.objects.select_related("player", "assist", "match__home__team", "match__away__team").filter(match__competition=c)
        ctx["scorers"] = [{**s, "team": team_view(s["team"], linkable)} for s in engine.scorers(events)[:10]]
        ctx["news"] = list(c.announcements.filter(published=True).order_by("-pinned", "-created")[:5])
    if tab == "fixtures":
        ctx["sections"] = [(k, [match_view(m, linkable) for m in v]) for k, v in group_by(upcoming, lambda m: m.round_name or f"Round {m.round}" if m.stage == "league" else (m.round_name or "Knockout"))]
    ctx["show_round"] = tab == "results"
    if tab == "results":
        ctx["sections"] = [(k, [match_view(m, linkable) for m in v]) for k, v in group_by(results, day_label)]
    if tab == "knockouts":
        ko = sorted([m for m in matches if m.stage == "knockout"], key=lambda m: (m.round, m.slot or 0, m.leg, m.id))
        ctx["rounds"] = bracket_rounds(ko, linkable)
    if tab == "teams":
        ctx["teams"] = [{**team_view(e.team, linkable), "group": e.group, "city": e.team.city} for e in entries]
    if not c.hidden and c.visibility == "public" and tab in ("", "table"):
        Competition.objects.filter(id=c.id).update(views=F("views") + 1)     # for "popular" (doesn't touch `updated`)
    title = {"": c.name, "table": f"{c.name} table", "fixtures": f"{c.name} fixtures", "results": f"{c.name} results", "teams": f"{c.name} teams", "knockouts": f"{c.name} knockouts"}[tab]
    ctx["title"] = title + (f" {c.season}" if c.season else "")
    ctx["description"] = (c.description[:180] if c.description else
                          f"{ctx['kind']} with {len(entries)} teams: live table, fixtures, results and match details.")
    return page(request, "competition.html", ctx, index=c.visibility == "public" and not c.hidden, private=c.hidden)


# ---------- match ----------
def match(request, slug):
    if slug.isdigit():                                         # /match/123 → the readable address
        m = Match.objects.filter(id=int(slug)).only("slug").first()
        if not m:
            raise Http404
        return redirect(f"/match/{m.slug}", permanent=True)
    m = MATCHES.filter(slug=slug).first()
    if not m:
        raise Http404
    c = competition_for(request, m.competition.slug)
    linkable = public_team_ids([e.team_id for e in (m.home, m.away) if e])
    mv = match_view(m, linkable)
    events = []
    for e in m.events.select_related("player", "assist"):
        events.append({"minute": e.minute, "side": e.side, "kind": e.kind, "label": EVENT_L[e.kind], "icon": EVENT_ICON.get(e.kind, "•"),
                       "player": e.player.name if e.player else e.player_name, "assist": e.assist.name if e.assist else e.assist_name,
                       "note": e.note})
    # knockout: aggregate over two legs
    aggregate = None
    if m.stage == "knockout" and m.home_id and m.away_id:
        other = MATCHES.filter(competition=c, stage="knockout", round=m.round, home_id=m.away_id, away_id=m.home_id).exclude(id=m.id).first()
        if other and m.home_score is not None and other.home_score is not None:
            aggregate = {"total": True, "home": m.home_score + other.away_score, "away": m.away_score + other.home_score,
                         "other": match_view(other, linkable)}
        elif other:
            aggregate = {"other": match_view(other, linkable)}
    table = (standings_view(c, public_team_ids(c.entries.values_list("team_id", flat=True)), only_group=m.group)
             if m.stage == "league" and c.format != "knockout" and c.kind != "friendly" else [])
    ids = {m.home_id, m.away_id}
    for g in table:
        for r in g["rows"]:
            r["highlight"] = r["entryId"] in ids
    same_round = [match_view(x, linkable) for x in MATCHES.filter(competition=c, stage=m.stage, round=m.round, group=m.group).exclude(id=m.id)[:10]]
    h, a = mv["home"], mv["away"]
    title = f"{h['name'] if h else 'TBD'} vs {a['name'] if a else 'TBD'}"
    if mv["played"]:
        desc = f"{title}: {m.home_score}–{m.away_score} ({mv['status_label']}). {c.name}, {mv['round']}."
    else:
        when = timezone.localtime(m.kickoff).strftime("%d %b %Y, %H:%M") if m.kickoff else "date to be set"
        desc = f"{title}, {when}. {c.name}, {mv['round']}."
    from proleague.logic import is_pro, whatsapp_link
    wa = whatsapp_link(m) if is_pro(c) and m.status != "finished" else ""
    report = f"/app/report/{m.id}" if is_pro(c) and m.status in ("scheduled", "live", "postponed") else ""
    ctx = {"c": c, "m": mv, "events": events, "aggregate": aggregate, "whatsapp": wa, "report": report, "decided": m.decided, "table": table, "same_round": same_round, "referee": m.referee,
           "title": title + (f" {m.home_score}–{m.away_score}" if mv["played"] else ""), "description": desc,
           "logo": logo("competition", c), "share_image": logo("competition", c) or org_logo(c.org), "notes": m.notes}
    return page(request, "match.html", ctx, index=c.visibility == "public" and not c.hidden, private=c.hidden)


def friendly_view(t, matches, linkable):
    """A team's friendlies: won-drawn-lost, the next ones and the latest results."""
    played = [x for x in matches if x.status == "finished" and x.home_score is not None]
    rec = {"W": 0, "D": 0, "L": 0}
    for x in played:
        mine, theirs = (x.home_score, x.away_score) if x.home and x.home.team_id == t.id else (x.away_score, x.home_score)
        rec["W" if mine > theirs else "L" if mine < theirs else "D"] += 1
    nxt = [x for x in matches if x.status in ("scheduled", "live", "postponed")][:4]
    return {"record": rec, "played": len(played), "next": [match_view(x, linkable) for x in nxt],
            "recent": [match_view(x, linkable) for x in reversed(played)][:8]} if matches else None


# ---------- team ----------
def team(request, slug):
    t = Team.objects.select_related("org").filter(slug=slug).first()
    if not t:
        raise Http404
    member = is_member(request, t.org_id)
    public_comps = [] if t.suspended else list(Competition.objects.filter(listed(), entries__team=t).order_by("-created"))
    if not public_comps and not member:
        raise Http404                                          # only shown for teams that play in public competitions
    comp_ids = [c.id for c in public_comps] if not member else list(Competition.objects.filter(entries__team=t).values_list("id", flat=True))
    ms = list(MATCHES.filter(competition_id__in=comp_ids).filter(Q(home__team=t) | Q(away__team=t)).order_by("kickoff", "round", "id"))
    linkable = public_team_ids({x.home.team_id for x in ms if x.home} | {x.away.team_id for x in ms if x.away} | {t.id})
    friendly = [x for x in ms if x.competition.kind == "friendly"]
    ms = [x for x in ms if x.competition.kind != "friendly"]              # friendlies have their own section
    finished = [x for x in ms if x.status == "finished"]
    form = []
    for x in finished[-5:]:
        mine, theirs = (x.home_score, x.away_score) if x.home and x.home.team_id == t.id else (x.away_score, x.home_score)
        form.append("W" if mine > theirs else "L" if mine < theirs else "D")
    players = list(t.players.filter(active=True).order_by("number", "name"))
    order = ["GK", "DF", "MF", "FW", ""]
    squad = [(POS_L[p] if p else "Squad", [pl for pl in players if pl.position == p]) for p in order]
    from .rankings import team_rank
    ctx = {"t": team_view(t, {t.id}), "team": t, "rank": team_rank(t.id), "competitions": public_comps if not member else
           list(Competition.objects.filter(id__in=comp_ids)), "upcoming": [match_view(x, linkable) for x in ms if x.status in ("scheduled", "live", "postponed")][:8],
           "recent": [match_view(x, linkable) for x in reversed(finished)][:8], "form": form, "squad": [s for s in squad if s[1]],
           "title": t.name, "description": t.description[:180] or f"{t.name}: fixtures, results and squad.", "logo": logo("team", t),
           "share_image": logo("team", t) or org_logo(t.org), "friendlies": friendly_view(t, friendly, linkable)}
    return page(request, "team.html", ctx, index=bool(public_comps), private=not public_comps)


# ---------- organization ----------
HEX6 = re.compile(r"^#[0-9a-f]{6}$")
LINK = re.compile(r"(https?://[^\s<>\"']+[^\s<>\"'.,;:!?)])")


def render_text(body):
    """An organization's page text → safe HTML. Everything is escaped first; only headings, lists, paragraphs and
    full http(s) links are turned into markup."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    def inline(line):
        out, last = [], 0
        for m in LINK.finditer(line):
            out.append(escape(line[last:m.start()]))
            url = m.group(1)
            out.append(f'<a href="{escape(url)}" rel="nofollow noopener noreferrer" target="_blank">{escape(url)}</a>')
            last = m.end()
        out.append(escape(line[last:]))
        return "".join(out)

    html, para, items = [], [], []

    def flush():
        if para:
            html.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
            para.clear()
        if items:
            html.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            items.clear()
    for raw in (body or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush()
        elif line.startswith("## ") or line.startswith("# "):
            flush()
            html.append(f"<h2>{inline(line.lstrip('#').strip())}</h2>")
        elif line.lstrip().startswith(("- ", "* ")):
            if para:
                flush()
            items.append(line.lstrip()[2:])
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return mark_safe("".join(html))


def org_site_context(request, o):
    s = o.site()
    color, ink = brand(o.brand_color)
    return {"o": o, "s": s, "brand": color, "brand_ink": ink, "org_logo": org_logo(o), "share_image": org_logo(o),
            "kind": ORG_KIND_L.get(o.kind, "Organization"), "pages": list(o.pages.filter(published=True).only("slug", "title"))}


def org_page(request, slug, page_slug):
    """One of the organization's own pages: /org/<slug>/p/<page>."""
    o = Organization.objects.filter(slug=slug).first()
    if not o or (o.status != "active" and not is_member(request, o.id)):
        raise Http404
    pg = o.pages.filter(slug=page_slug).first()
    if not pg or (not pg.published and not is_member(request, o.id)):
        raise Http404
    ctx = {**org_site_context(request, o), "pg": pg, "content": render_text(pg.body), "title": f"{pg.title} · {o.name}",
           "description": (pg.body.strip().split("\n")[0] if pg.body.strip() else o.description)[:180] or o.name}
    return page(request, "org_page.html", ctx, index=pg.published and o.status == "active", private=not pg.published or o.status != "active")
ORG_KIND_L = {"league": "League", "school": "School", "club": "Club", "academy": "Academy", "company": "Company",
              "community": "Community", "association": "Association", "other": "Organization"}


def brand(color):
    """The organization's colour (validated again here, it goes into CSS) and a readable text colour on top of it."""
    c = color if color and HEX6.match(color) else "#2563eb"
    lin = [(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4) for v in (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return c, ("#ffffff" if 1.05 / (lum + 0.05) >= (lum + 0.05) / 0.0563 else "#061024")


def organization(request, slug):
    """The organization's own public website: /org/<slug>. What's shown follows its website settings."""
    o = Organization.objects.filter(slug=slug).first()
    if not o or (o.status != "active" and not is_member(request, o.id)):
        raise Http404
    s = o.site()
    comps = list(o.competitions.filter(listed()).order_by("-status", "-created"))
    ms = MATCHES.filter(competition__in=comps)
    teams = list(Team.objects.filter(entries__competition__in=comps, suspended=False).distinct().order_by("name"))
    linkable = {x.id for x in teams}
    now = timezone.now()
    color, ink = brand(o.brand_color)
    ctx = {"o": o, "s": s, "brand": color, "brand_ink": ink, "kind": ORG_KIND_L.get(o.kind, "Organization"),
           "org_logo": org_logo(o), "share_image": org_logo(o),
           "comps": [{"c": c, "logo": logo("competition", c), "kind": KIND_L.get(c.kind, "Competition"), "teams": c.entries.count()} for c in comps],
           "title": o.name, "description": (s["tagline"] or o.description)[:180] or f"{o.name}: competitions, tables, fixtures and results."}
    if s["show_teams"]:
        ctx["teams"] = [team_view(x, linkable) for x in teams]
    if s["show_players"] and teams:
        players = Player.objects.select_related("team").filter(team__in=teams, active=True).order_by("team__name", "number", "name")[:400]
        ctx["players"] = group_by(players, lambda p: p.team.name)
    if s["show_fixtures"]:
        soon = ms.filter(status__in=["scheduled", "live"]).filter(Q(kickoff__gte=now - timezone.timedelta(hours=3)) | Q(kickoff__isnull=True))
        ctx["upcoming"] = [match_view(x, linkable) for x in soon.order_by(F("kickoff").asc(nulls_last=True), "round", "id")[:8]]
    if s["show_results"]:
        ctx["recent"] = [match_view(x, linkable) for x in ms.filter(status="finished").order_by("-kickoff")[:8]]
    if s["show_standings"]:
        ctx["tables"] = [{"c": c, "groups": standings_view(c, linkable)} for c in comps if c.format != "knockout" and c.kind != "friendly"][:4]
    if s["show_brackets"]:
        brackets = []
        for c in comps:
            if c.format == "league":
                continue
            ko = [match_view(x, linkable) for x in ms.filter(competition=c, stage="knockout").order_by("round", "slot", "leg", "id")]
            if ko:
                brackets.append({"c": c, "rounds": group_by(ko, lambda m: m["round"])})
        ctx["brackets"] = brackets[:3]
    if s["show_news"]:
        ctx["news"] = list(Announcement.objects.filter(org=o, published=True).filter(Q(competition__isnull=True) | Q(competition__in=comps))
                           .select_related("competition").order_by("-pinned", "-created")[:6])
    nav = [("competitions", "Competitions", True), ("fixtures", "Fixtures", ctx.get("upcoming") is not None),
           ("results", "Results", ctx.get("recent") is not None), ("standings", "Standings", bool(ctx.get("tables"))),
           ("brackets", "Brackets", bool(ctx.get("brackets"))), ("teams", "Teams", bool(ctx.get("teams"))),
           ("players", "Players", bool(ctx.get("players"))), ("news", "News", bool(ctx.get("news"))),
           ("about", "About", bool(s["about"] or s["contact"] or o.description))]
    ctx["nav"] = [(k, label) for k, label, on in nav if on]
    ctx["pages"] = list(o.pages.filter(published=True).only("slug", "title"))
    return page(request, "organization.html", ctx, index=bool(comps) and o.status == "active", private=o.status != "active")


def organization_alias(request, slug):
    return redirect(f"/org/{slug}", permanent=True)


# ---------- old addresses, robots and sitemap ----------
def league_alias(request, slug, tab=""):
    return redirect(f"/competition/{slug}" + (f"/{tab}" if tab else ""), permanent=True)


def robots(request):
    return HttpResponse(f"User-agent: *\nDisallow: /app\nDisallow: /api/\nDisallow: /invite/\nSitemap: {base_url(request)}/sitemap.xml\n",
                        content_type="text/plain")


def sitemap(request):
    base = base_url(request)
    urls = [("/", None), ("/competitions", None)]
    for c in Competition.objects.filter(listed()).only("slug", "updated"):
        urls += [(f"/competition/{c.slug}{t}", c.updated) for t in ("", "/table", "/fixtures", "/results", "/teams")]
    urls += [(f"/match/{s}", u) for s, u in Match.objects.filter(listed("competition__")).values_list("slug", "updated")[:20000]]
    urls += [(f"/team/{s}", None) for s in Team.objects.filter(listed("entries__competition__"), suspended=False).distinct().values_list("slug", flat=True)]
    urls += [(f"/org/{s}", None) for s in Organization.objects.filter(listed("competitions__")).distinct().values_list("slug", flat=True)]
    from xml.sax.saxutils import escape
    body = "".join(f"<url><loc>{escape(base + u)}</loc>{f'<lastmod>{d.date().isoformat()}</lastmod>' if d else ''}</url>" for u, d in urls)
    return HttpResponse(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                        content_type="application/xml")



def rankings_page(request, tab=""):
    """/rankings (teams) and /rankings/players, across every organization. Super admins can preview while it's off."""
    from .rankings import compute, settings_
    cfg = settings_()
    user = session_user(request)
    if not cfg["enabled"] and not (user and user.is_superuser):
        raise Http404
    if tab not in ("", "players"):
        raise Http404
    data = compute()
    country = (request.GET.get("country") or "").strip()[:60]
    region = (request.GET.get("region") or "").strip()[:60] if country else ""
    where = lambda o: (not country or o.country.lower() == country.lower()) and (not region or o.region.lower() == region.lower())
    teams = [r for r in data["teams"] if r["played"] >= cfg["min_matches"]]
    countries = sorted({r["team"].org.country for r in teams if r["team"].org.country})
    regions = sorted({r["team"].org.region for r in teams if r["team"].org.region and r["team"].org.country.lower() == country.lower()}) if country else []
    rows = []
    for r in teams:                                   # positions are counted inside the chosen area
        if where(r["team"].org):
            rows.append({**r, "position": len(rows) + 1, "rating": round(r["rating"]), "t": team_view(r["team"], public_team_ids([r["team"].id])),
                         "org": r["team"].org, "gd": r["gf"] - r["ga"]})
    players = []
    for p in data["players"]:
        team = p["team"]
        if team and where(team.org) or (not team and not country):
            players.append({**p, "position": len(players) + 1, "t": team_view(team, set()) if team else None, "org": team.org if team else None})
        if len(players) >= 100:
            break
    area = ", ".join(x for x in (region, country) if x) or "Worldwide"
    ctx = {"tab": tab, "rows": rows[:200], "players": players, "countries": countries, "country": country, "regions": regions, "region": region,
           "area": area, "min_matches": cfg["min_matches"],
           "matches": data["matches"], "preview": not cfg["enabled"], "nav": "rankings",
           "title": ("Player rankings" if tab else "Team rankings") + ("" if area == "Worldwide" else f": {area}"), "description": "The best teams and players across every league and tournament on the platform."}
    return page(request, "rankings.html", ctx, index=cfg["enabled"], private=not cfg["enabled"], banner=False)
