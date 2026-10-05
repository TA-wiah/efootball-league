"""Public, shareable pages rendered on the server (so WhatsApp/X/Facebook previews and search engines see them).

Visibility:  public   → listed, indexed, shareable
             unlisted → works for anyone with the link, but "noindex" and never listed elsewhere
             private  → 404 for everyone except the organization's members (who see a marked preview)
"""
import secrets
from collections import OrderedDict

from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from league.http import base_url, session_user
from orgs.models import Membership, Organization

from . import engine
from .models import EVENT_KINDS, MATCH_STATUS, POSITIONS, Announcement, Competition, Entry, Match, MatchEvent, Team

STATUS_L = dict(MATCH_STATUS) | {"finished": "Full time"}
EVENT_L = dict(EVENT_KINDS)
EVENT_ICON = {"goal": "goal", "penalty_goal": "goal", "own_goal": "goal", "yellow": "yellow", "second_yellow": "red", "red": "red", "substitution": "sub"}
POS_L = dict(POSITIONS)
FORMAT_L = {"league": "League", "groups_knockout": "Groups and knockouts", "knockout": "Knockout"}
KIND_L = {"league": "League", "tournament": "Tournament", "cup": "Cup", "championship": "Championship", "friendly": "Friendly series", "other": "Competition"}


# ---------- helpers ----------
def is_member(request, org_id):
    user = session_user(request)
    return bool(user and Membership.objects.filter(org_id=org_id, user=user).exists())


def page(request, template, ctx, *, index=True, private=False):
    nonce = secrets.token_urlsafe(16)
    base = base_url(request)
    ctx.update(nonce=nonce, base=base, url=base + request.path, index=index and not private, private_preview=private,
               app_url="/app")
    resp = render(request, f"public/{template}", ctx)
    resp["Content-Security-Policy"] = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                                       "form-action 'self'; frame-ancestors 'none'")
    resp["Cache-Control"] = "private, no-store" if private else "public, max-age=60"
    if not ctx["index"]:
        resp["X-Robots-Tag"] = "noindex"
    return resp


def competition_for(request, slug):
    c = Competition.objects.select_related("org").filter(slug=slug).first()
    if not c or (c.visibility == "private" and not is_member(request, c.org_id)):
        raise Http404
    return c


def logo(kind, obj):
    return f"/media/{kind}/{obj.id}/logo?v={obj.version}" if obj.data else None


def public_team_ids(team_ids):
    """Teams that get their own public page: those playing in at least one public competition."""
    return set(Entry.objects.filter(team_id__in=team_ids, competition__visibility="public").values_list("team_id", flat=True))


def team_view(t, linkable):
    if not t:
        return None
    return {"name": t.name, "short": t.short_name or t.name, "slug": t.slug, "logo": logo("team", t), "link": t.id in linkable,
            "initials": "".join(ch for ch in (t.short_name or t.name) if ch.isalnum())[:3].upper(), "hue": sum(map(ord, t.name)) * 31 % 360}


def match_view(m, linkable):
    home, away = m.home.team if m.home else None, m.away.team if m.away else None
    return {"slug": m.slug, "home": team_view(home, linkable), "away": team_view(away, linkable), "kickoff": m.kickoff,
            "round": m.round_name or f"Round {m.round}", "group": m.group, "leg": m.leg, "stage": m.stage, "status": m.status,
            "status_label": STATUS_L[m.status], "played": m.home_score is not None and m.away_score is not None,
            "hs": m.home_score, "as": m.away_score, "hp": m.home_pens, "ap": m.away_pens, "venue": m.venue,
            "competition": {"name": m.competition.name, "slug": m.competition.slug}}


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
    for g in sorted({e.group for e in entries}):
        if only_group is not None and g != only_group:
            continue
        rows = engine.standings(c, [e for e in entries if e.group == g], played)
        for r in rows:
            r["team"] = team_view(r["team"], linkable)
            r["qualifies"] = c.format == "groups_knockout" and r["position"] <= c.qualifiers_per_group
        groups.append({"name": g, "rows": rows})
    return groups


MATCHES = Match.objects.select_related("competition", "home__team", "away__team")


# ---------- competition ----------
TABS = [("", "Overview"), ("table", "Table"), ("fixtures", "Fixtures"), ("results", "Results"), ("teams", "Teams")]


def competition(request, slug, tab=""):
    c = competition_for(request, slug)
    if tab not in dict(TABS):
        raise Http404
    entries = list(c.entries.select_related("team").order_by("team__name"))
    linkable = public_team_ids([e.team_id for e in entries])
    matches = list(MATCHES.filter(competition=c).order_by("kickoff", "round", "leg", "id"))
    upcoming = [m for m in matches if m.status in ("scheduled", "live", "postponed")]
    results = sorted([m for m in matches if m.status == "finished"], key=lambda m: (m.kickoff is not None, m.kickoff, m.round), reverse=True)
    has_table = c.format != "knockout"
    ctx = {"c": c, "tab": tab, "tabs": [(t, label) for t, label in TABS if t != "table" or has_table], "logo": logo("competition", c),
           "kind": KIND_L.get(c.kind, "Competition"), "format": FORMAT_L[c.format], "has_table": has_table,
           "team_count": len(entries), "played": len(results), "total": len(matches),
           "points": f"Win {c.points_win} · Draw {c.points_draw} · Loss {c.points_loss}",
           "tiebreakers": [engine.CRITERIA[k] for k in engine.clean_tiebreakers(c.tiebreakers or engine.DEFAULT_TIEBREAKERS)]}
    if tab in ("", "table") and has_table:
        ctx["groups"] = standings_view(c, linkable)
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
    if tab == "teams":
        ctx["teams"] = [{**team_view(e.team, linkable), "group": e.group, "city": e.team.city} for e in entries]
    if c.visibility == "public" and tab in ("", "table"):
        Competition.objects.filter(id=c.id).update(views=F("views") + 1)     # for "popular" (doesn't touch `updated`)
    title = {"": c.name, "table": f"{c.name} table", "fixtures": f"{c.name} fixtures", "results": f"{c.name} results", "teams": f"{c.name} teams"}[tab]
    ctx["title"] = title + (f" {c.season}" if c.season else "")
    ctx["description"] = (c.description[:180] if c.description else
                          f"{ctx['kind']} with {len(entries)} teams: live table, fixtures, results and match details.")
    return page(request, "competition.html", ctx, index=c.visibility == "public", private=c.visibility == "private")


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
    table = standings_view(c, public_team_ids(c.entries.values_list("team_id", flat=True)), only_group=m.group) if m.stage == "league" and c.format != "knockout" else []
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
    ctx = {"c": c, "m": mv, "events": events, "aggregate": aggregate, "table": table, "same_round": same_round, "referee": m.referee,
           "title": title + (f" {m.home_score}–{m.away_score}" if mv["played"] else ""), "description": desc,
           "logo": logo("competition", c), "notes": m.notes}
    return page(request, "match.html", ctx, index=c.visibility == "public", private=c.visibility == "private")


# ---------- team ----------
def team(request, slug):
    t = Team.objects.select_related("org").filter(slug=slug).first()
    if not t:
        raise Http404
    member = is_member(request, t.org_id)
    public_comps = list(Competition.objects.filter(entries__team=t, visibility="public").order_by("-created"))
    if not public_comps and not member:
        raise Http404                                          # only shown for teams that play in public competitions
    comp_ids = [c.id for c in public_comps] if not member else list(Competition.objects.filter(entries__team=t).values_list("id", flat=True))
    ms = list(MATCHES.filter(competition_id__in=comp_ids).filter(Q(home__team=t) | Q(away__team=t)).order_by("kickoff", "round", "id"))
    linkable = public_team_ids({x.home.team_id for x in ms if x.home} | {x.away.team_id for x in ms if x.away} | {t.id})
    finished = [x for x in ms if x.status == "finished"]
    form = []
    for x in finished[-5:]:
        mine, theirs = (x.home_score, x.away_score) if x.home and x.home.team_id == t.id else (x.away_score, x.home_score)
        form.append("W" if mine > theirs else "L" if mine < theirs else "D")
    players = list(t.players.filter(active=True).order_by("number", "name"))
    order = ["GK", "DF", "MF", "FW", ""]
    squad = [(POS_L[p] if p else "Squad", [pl for pl in players if pl.position == p]) for p in order]
    ctx = {"t": team_view(t, {t.id}), "team": t, "competitions": public_comps if not member else
           list(Competition.objects.filter(id__in=comp_ids)), "upcoming": [match_view(x, linkable) for x in ms if x.status in ("scheduled", "live", "postponed")][:8],
           "recent": [match_view(x, linkable) for x in reversed(finished)][:8], "form": form, "squad": [s for s in squad if s[1]],
           "title": t.name, "description": t.description[:180] or f"{t.name}: fixtures, results and squad.", "logo": logo("team", t)}
    return page(request, "team.html", ctx, index=bool(public_comps), private=not public_comps)


# ---------- organization ----------
def organization(request, slug):
    o = Organization.objects.filter(slug=slug).first()
    if not o:
        raise Http404
    comps = list(o.competitions.filter(visibility="public").order_by("-status", "-created"))
    ms = MATCHES.filter(competition__in=comps)
    teams = list(Team.objects.filter(entries__competition__in=comps).distinct().order_by("name"))
    linkable = {x.id for x in teams}
    now = timezone.now()
    ctx = {"o": o, "comps": [{"c": c, "logo": logo("competition", c), "kind": KIND_L.get(c.kind, "Competition"), "teams": c.entries.count()} for c in comps],
           "teams": [team_view(x, linkable) for x in teams],
           "upcoming": [match_view(x, linkable) for x in ms.filter(status__in=["scheduled", "live"], kickoff__gte=now).order_by("kickoff")[:6]],
           "recent": [match_view(x, linkable) for x in ms.filter(status="finished").order_by("-kickoff")[:6]],
           "news": list(Announcement.objects.filter(org=o, published=True).filter(Q(competition__isnull=True) | Q(competition__in=comps)).order_by("-pinned", "-created")[:5]),
           "title": o.name, "description": o.description[:180] or f"{o.name}: competitions, tables, fixtures and results."}
    return page(request, "organization.html", ctx, index=bool(comps))


# ---------- old addresses, robots and sitemap ----------
def league_alias(request, slug, tab=""):
    return redirect(f"/competition/{slug}" + (f"/{tab}" if tab else ""), permanent=True)


def robots(request):
    return HttpResponse(f"User-agent: *\nDisallow: /app\nDisallow: /api/\nDisallow: /invite/\nSitemap: {base_url(request)}/sitemap.xml\n",
                        content_type="text/plain")


def sitemap(request):
    base = base_url(request)
    urls = [("/", None), ("/competitions", None)]
    for c in Competition.objects.filter(visibility="public").only("slug", "updated"):
        urls += [(f"/competition/{c.slug}{t}", c.updated) for t in ("", "/table", "/fixtures", "/results", "/teams")]
    urls += [(f"/match/{s}", u) for s, u in Match.objects.filter(competition__visibility="public").values_list("slug", "updated")[:20000]]
    urls += [(f"/team/{s}", None) for s in Team.objects.filter(entries__competition__visibility="public").distinct().values_list("slug", flat=True)]
    urls += [(f"/organization/{s}", None) for s in Organization.objects.filter(competitions__visibility="public").distinct().values_list("slug", flat=True)]
    from xml.sax.saxutils import escape
    body = "".join(f"<url><loc>{escape(base + u)}</loc>{f'<lastmod>{d.date().isoformat()}</lastmod>' if d else ''}</url>" for u, d in urls)
    return HttpResponse(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                        content_type="application/xml")
