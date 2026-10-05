"""The platform's front door: the landing page, competition discovery and search. Only public competitions (and the
teams, matches and organizations in them) ever appear here; unlisted and private ones are never listed."""
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.utils import timezone

from orgs.models import Organization

from .models import KINDS, Competition, Match, Team
from .public import KIND_L, MATCHES, logo, match_view, page, team_view

PER_PAGE = 24
SORTS = {"featured": "Featured", "popular": "Most popular", "upcoming": "Starting soon", "new": "Newest", "name": "A–Z"}
STATUSES = {"active": "In progress", "upcoming": "Starting soon", "completed": "Completed"}


def public_competitions():
    return (Competition.objects.filter(visibility="public").select_related("org")
            .annotate(team_count=Count("entries", distinct=True),
                      played_count=Count("matches", filter=Q(matches__status="finished"), distinct=True)))


def card(c):
    return {"name": c.name, "slug": c.slug, "logo": logo("competition", c), "kind": KIND_L.get(c.kind, "Competition"), "season": c.season,
            "where": ", ".join(x for x in (c.region, c.country) if x), "org": c.org.name, "teams": c.team_count, "played": c.played_count,
            "status": c.status, "start": c.start_date, "featured": c.featured}


def public_team_ids():
    return set(Team.objects.filter(entries__competition__visibility="public").values_list("id", flat=True))


def upcoming_q():
    today = timezone.localdate()
    return Q(start_date__gte=today) | Q(status="draft")


# ---------- landing page ----------
def landing(request):
    comps = public_competitions()
    featured = list(comps.filter(featured=True).order_by("-updated")[:6])
    if len(featured) < 6:            # top up with the liveliest competitions
        featured += list(comps.filter(status="active").exclude(id__in=[c.id for c in featured])
                         .order_by("-views", "-played_count", "-updated")[:6 - len(featured)])
    now = timezone.now()
    linkable = public_team_ids()
    upcoming = MATCHES.filter(competition__visibility="public", status__in=["scheduled", "live"], kickoff__gte=now).order_by("kickoff")[:6]
    countries = (Competition.objects.filter(visibility="public").exclude(country="").values("country")
                 .annotate(n=Count("id")).order_by("-n", "country")[:12])
    stats = {"competitions": Competition.objects.filter(visibility="public").count(), "teams": len(linkable),
             "matches": Match.objects.filter(competition__visibility="public", status="finished").count()}
    ctx = {"featured": [card(c) for c in featured],
           "popular": [card(c) for c in comps.order_by("-views", "-played_count")[:6]],
           "starting": [card(c) for c in comps.filter(upcoming_q()).order_by(F("start_date").asc(nulls_last=True), "-created")[:6]],
           "upcoming": [match_view(m, linkable) for m in upcoming], "countries": list(countries),
           "stats": stats if stats["competitions"] >= 3 else None,
           "title": "Run your league or tournament online",
           "description": "Create a competition, register teams, generate fixtures, enter results and publish live league tables "
                          "and match pages. Free for organizers, open to fans."}
    return page(request, "landing.html", ctx)


def home(request):
    if settings.HOME_PAGE == "league":
        from league.views import index
        return index(request)
    return landing(request)


# ---------- browse competitions ----------
def competitions(request):
    g = request.GET
    q = (g.get("q") or "").strip()[:80]
    kind = g.get("type") if g.get("type") in dict(KINDS) else ""
    status = g.get("status") if g.get("status") in STATUSES else ""
    sort = g.get("sort") if g.get("sort") in SORTS else "featured"
    country, region = (g.get("country") or "").strip()[:60], (g.get("region") or "").strip()[:60]
    qs = public_competitions()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(org__name__icontains=q) | Q(description__icontains=q) | Q(season__icontains=q)
                       | Q(region__icontains=q) | Q(country__icontains=q))
    if kind:
        qs = qs.filter(kind=kind)
    if country:
        qs = qs.filter(country__iexact=country)
    if region:
        qs = qs.filter(region__iexact=region)
    if status == "upcoming":
        qs = qs.filter(upcoming_q())
    elif status:
        qs = qs.filter(status=status)
    qs = qs.order_by(*{"featured": ["-featured", "-views", "-played_count", "-updated"], "popular": ["-views", "-played_count"],
                       "upcoming": [F("start_date").asc(nulls_last=True), "-created"], "new": ["-created"], "name": ["name"]}[sort])
    pager = Paginator(qs, PER_PAGE)
    pg = pager.get_page(g.get("page"))
    base = Competition.objects.filter(visibility="public")
    countries = base.exclude(country="").values("country").annotate(n=Count("id")).order_by("country")
    regions = base.filter(country__iexact=country).exclude(region="").values("region").annotate(n=Count("id")).order_by("region") if country else []
    params = {k: v for k, v in (("q", q), ("type", kind), ("status", status), ("sort", sort if sort != "featured" else ""),
                                 ("country", country), ("region", region)) if v}
    from urllib.parse import urlencode
    ctx = {"cards": [card(c) for c in pg.object_list], "pg": pg, "q": q, "kind": kind, "status": status, "sort": sort, "country": country,
           "region": region, "kinds": [(k, KIND_L.get(k, v)) for k, v in KINDS], "statuses": list(STATUSES.items()), "sorts": list(SORTS.items()),
           "countries": list(countries), "regions": list(regions), "total": pager.count, "query": urlencode(params),
           "filtered": bool(params.keys() - {"sort"}),
           "title": " · ".join(x for x in (q and f"“{q}”", region, country, KIND_L.get(kind) if kind else "") if x) + " competitions" if params.keys() - {"sort"} else "Browse competitions",
           "description": "Find leagues, tournaments, cups and school competitions: live tables, fixtures and results."}
    return page(request, "competitions.html", ctx, index=not (params.keys() - {"sort", "country", "region", "type"}))


# ---------- search ----------
def search(request):
    q = (request.GET.get("q") or "").strip()[:80]
    ctx = {"q": q, "title": f"Search: {q}" if q else "Search", "description": "Search competitions, teams, matches and organizations."}
    if len(q) >= 2:
        linkable = public_team_ids()
        ctx["comps"] = [card(c) for c in public_competitions().filter(Q(name__icontains=q) | Q(org__name__icontains=q) | Q(season__icontains=q)
                                                                        | Q(region__icontains=q) | Q(country__icontains=q)).order_by("-views")[:12]]
        ctx["teams"] = [{**team_view(t, linkable), "city": t.city} for t in
                        Team.objects.filter(id__in=linkable).filter(Q(name__icontains=q) | Q(short_name__icontains=q) | Q(city__icontains=q)).order_by("name")[:12]]
        words = [w.strip() for w in q.replace(" vs ", "|").replace(" v ", "|").split("|") if w.strip()]
        mq = MATCHES.filter(competition__visibility="public")
        if len(words) == 2:      # "Kasoa vs Winneba"
            a, b = words
            mq = mq.filter((Q(home__team__name__icontains=a) & Q(away__team__name__icontains=b)) | (Q(home__team__name__icontains=b) & Q(away__team__name__icontains=a)))
        else:
            mq = mq.filter(Q(home__team__name__icontains=q) | Q(away__team__name__icontains=q))
        ctx["matches"] = [match_view(m, linkable) for m in mq.order_by(F("kickoff").desc(nulls_last=True))[:12]]
        ctx["orgs"] = list(Organization.objects.filter(competitions__visibility="public", name__icontains=q).distinct().order_by("name")[:8])
        ctx["count"] = len(ctx["comps"]) + len(ctx["teams"]) + len(ctx["matches"]) + len(ctx["orgs"])
    return page(request, "search.html", ctx, index=False)
