"""Pro League endpoints: the super admin runs seasons; each team's organization accepts or declines its invitation."""
from decimal import InvalidOperation

from django.db import transaction
from django.utils import timezone

from league.http import ApiError, body, endpoint, ms, site_url, text
from league.logic import audit, hit
from orgs.api import access, log
from orgs.permissions import can
from payments import paynova
from payments.notify import _send, managers
from superadmin.api import admin

from . import logic
from .models import Season, SeasonTeam


def team_row(st, admin_view=False):
    t = st.team
    d = {"id": st.id, "seed": st.seed, "rating": st.rating, "reason": st.reason, "status": st.status, "paid": st.paid,
         "division": st.season.divisions[st.division] if st.division is not None and st.division < len(st.season.divisions) else None,
         "finalPosition": st.final_position, "respondedAt": ms(st.responded_at),
         "team": {"id": t.id, "name": t.name, "slug": t.slug}, "org": {"id": t.org_id, "name": t.org.name, "slug": t.org.slug}}
    if not admin_view:
        d["checkoutUrl"] = st.checkout_url if st.status == SeasonTeam.ACCEPTED and not st.paid else None
    return d


def season_json(s, teams=True):
    d = {"id": s.id, "number": s.number, "name": s.name, "status": s.status, "divisions": s.divisions, "size": s.size, "move": s.move,
         "legs": s.legs, "country": s.country or None, "fee": f"{s.fee:.2f}", "currency": s.currency, "created": ms(s.created),
         "started": ms(s.started), "finished": ms(s.finished)}
    if teams:
        rows = list(s.teams.select_related("team__org", "season").order_by("seed"))
        d["teams"] = [team_row(st, True) for st in rows]
        counts = {}
        for st in rows:
            counts[st.status] = counts.get(st.status, 0) + 1
        d["counts"] = counts
        d["plan"] = logic.division_sizes(counts.get(SeasonTeam.CONFIRMED, 0), s.divisions, s.size)
        comps = {}
        for st in rows:
            if st.competition_id and st.division is not None:
                comps[st.division] = {"name": st.competition.name, "slug": st.competition.slug, "division": s.divisions[st.division]}
        d["competitions"] = [comps[k] for k in sorted(comps)]
    return d


def notify_invited(season, rows, base):
    by_org = {}
    for st in rows:
        by_org.setdefault(st.team.org, []).append(st)
    for org, sts in by_org.items():
        names = ", ".join(st.team.name for st in sts)
        fee = f" The entry fee is {season.currency} {season.fee:.2f} per team." if season.fee else ""
        for to in set(managers(org)) | {m.user.email for m in org.memberships.select_related("user").filter(role="owner") if m.user.email}:
            _send(to, f"{names} invited to the {logic.config()['name']}", "You're invited to the Pro League",
                  [f"{names} {'has' if len(sts) == 1 else 'have'} been invited to {season.name}, where the best-ranked teams of the platform play each other in divisions.",
                   f"Accept or decline on your organization's dashboard.{fee}"],
                  f"{base}/app/org/{org.slug}", "Answer the invitation")


# ---------- super admin ----------
@admin("GET")
def admin_overview(request, user, ip):
    from competitions.reports import settle_overdue
    settle_overdue()
    cfg = logic.config()
    org = None
    from orgs.models import Organization
    if cfg["org_id"]:
        org = Organization.objects.filter(id=cfg["org_id"]).first()
    seasons = list(Season.objects.filter(org=org)) if org else []
    from competitions.rankings import settings_ as rs
    return {"settings": {**{k: v for k, v in cfg.items() if k != "fee"}, "fee": f"{cfg['fee']:.2f}", "divisions": ", ".join(cfg["divisions"])},
            "rankingsOn": rs()["enabled"], "paymentsReady": paynova.ready(), "capacity": logic.capacity(cfg),
            "org": {"name": org.name, "slug": org.slug} if org else None,
            "seasons": [season_json(s, teams=(i == 0)) for i, s in enumerate(seasons[:10])]}


@admin("POST")
def admin_new_season(request, user, ip):
    cfg = logic.config()
    if not cfg["enabled"]:
        raise ApiError(400, "Switch the Pro League on in its settings first.")
    try:
        season = logic.propose(user)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    audit(user.username, f"proposed {season.name}", ip, resource="proleague")
    return {"ok": True, "season": season_json(season)}


@admin("POST")
def admin_season_act(request, user, ip, season_id, action):
    season = Season.objects.filter(id=season_id).first()
    if not season:
        raise ApiError(404, "Season not found.")
    b = body(request)
    if action == "delete":
        if season.status != Season.DRAFT:
            raise ApiError(400, "Only a season that hasn't sent invitations can be deleted.")
        season.delete()
        return {"ok": True}
    if action == "invite":
        if season.status not in (Season.DRAFT, Season.INVITING):
            raise ApiError(400, "Invitations can only be sent before the season starts.")
        rows = list(season.teams.select_related("team__org").filter(status=SeasonTeam.PROPOSED))
        SeasonTeam.objects.filter(id__in=[r.id for r in rows]).update(status=SeasonTeam.INVITED)
        season.status = Season.INVITING
        season.save(update_fields=["status"])
        notify_invited(season, rows, site_url(request))
        for st in rows:
            log(st.team.org, None, f"{st.team.name} was invited to {season.name}")
    elif action == "add":
        n = b.get("count")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 50:
            raise ApiError(400, "Add from 1 to 50 teams.")
        if season.status not in (Season.DRAFT, Season.INVITING):
            raise ApiError(400, "Teams can only be added before the season starts.")
        added = logic.add_next(season, n)
        if not added:
            raise ApiError(400, "No more ranked teams are available.")
        if season.status == Season.INVITING:
            notify_invited(season, added, site_url(request))
    elif action == "start":
        if season.status != Season.INVITING:
            raise ApiError(400, "Send the invitations first, then start the season once teams have confirmed.")
        try:
            logic.start(season, user)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
    elif action == "finish":
        if season.status != Season.RUNNING:
            raise ApiError(400, "Only a running season can be finished.")
        open_ = sum(c.matches.exclude(status__in=["finished", "cancelled"]).count() for c in {st.competition for st in season.teams.exclude(competition=None)})
        if open_ and b.get("force") is not True:
            raise ApiError(409, f"{open_} match{'es' if open_ != 1 else ''} haven't been played yet. Confirm to finish anyway.", needsConfirm=True)
        logic.finish(season)
    else:
        raise ApiError(404, "Unknown action.")
    audit(user.username, f"{action} {season.name}", ip, resource="proleague")
    season.refresh_from_db()
    return {"ok": True, "season": season_json(season)}


@admin("POST")
def admin_team_act(request, user, ip, st_id, action):
    """confirm (e.g. paid another way), remove, or invite one team."""
    st = SeasonTeam.objects.select_related("season", "team__org").filter(id=st_id).first()
    if not st:
        raise ApiError(404, "Team not found in a season.")
    if st.season.status not in (Season.DRAFT, Season.INVITING):
        raise ApiError(400, "The season has already started.")
    if action == "confirm":
        st.status, st.responded_at = SeasonTeam.CONFIRMED, timezone.now()
    elif action == "remove":
        st.status = SeasonTeam.REMOVED
    elif action == "restore":
        st.status = SeasonTeam.INVITED if st.season.status == Season.INVITING else SeasonTeam.PROPOSED
    else:
        raise ApiError(404, "Unknown action.")
    st.save(update_fields=["status", "responded_at"])
    audit(user.username, f"{action} {st.team.name} in {st.season.name}", ip, resource="proleague")
    return {"ok": True}


# ---------- each organization ----------
def can_answer(m, st):
    org = m.org
    return can(m.role, "org.settings", org) or can(m.role, "teams.manage", org) or \
        (can(m.role, "teams.manage_assigned", org) and m.teams.filter(id=st.team_id).exists())


def check_fee(st):
    """Ask PayNova whether the entry fee is paid; confirm the team when it is (once)."""
    if st.paid or not st.reference:
        return False
    res = paynova.verify(st.reference)
    if str(res.get("status", "")).lower() != "paid":
        return False
    try:
        ok = paynova.money(res.get("amount", "0")) == st.season.fee
    except (InvalidOperation, ValueError):
        ok = False
    if not ok:
        return False
    with transaction.atomic():
        st = SeasonTeam.objects.select_for_update().get(pk=st.pk)
        if st.paid:
            return False
        st.paid, st.status = True, SeasonTeam.CONFIRMED
        st.save(update_fields=["paid", "status"])
    log(st.team.org, None, f"paid the {st.season.name} entry fee for {st.team.name}")
    return True


@endpoint("GET", login_required=True)
def org_invitations(request, user, ip, slug):
    org, m = access(user, slug, "org.view")
    rows = list(SeasonTeam.objects.select_related("season", "team__org", "competition").filter(team__org=org).exclude(status=SeasonTeam.PROPOSED)
                .exclude(season__status=Season.FINISHED).order_by("-season__number", "seed")[:50])
    for st in rows:
        if st.status == SeasonTeam.ACCEPTED and st.reference and not st.paid:
            try:
                check_fee(st)
                st.refresh_from_db()
            except paynova.PayNovaError:
                break
    mine = set(m.teams.values_list("id", flat=True))
    rows = [st for st in rows if can_answer(m, st) or st.team_id in mine]      # players see their own teams only
    cfg = logic.config()
    return {"league": cfg["name"], "whatsapp": cfg["whatsapp"] or None,
            "invitations": [{**team_row(st), "season": {"id": st.season.id, "name": st.season.name, "status": st.season.status,
                                                         "fee": f"{st.season.fee:.2f}", "currency": st.season.currency},
                             "competition": {"name": st.competition.name, "slug": st.competition.slug} if st.competition else None,
                             "canAnswer": can_answer(m, st)} for st in rows]}


@endpoint("POST", login_required=True)
def org_answer(request, user, ip, slug, st_id, action):
    org, m = access(user, slug, "org.view")
    st = SeasonTeam.objects.select_related("season", "team__org").filter(id=st_id, team__org=org).first()
    if not st:
        raise ApiError(404, "Invitation not found.")
    if not can_answer(m, st):
        raise ApiError(403, "Ask your organization's owner or administrator to answer this invitation.")
    season = st.season
    if action == "check":
        if hit(f"procheck:{st.id}", 10, 60):
            raise ApiError(429, "Checked a moment ago. Try again in a minute.")
        try:
            done = check_fee(st)
        except paynova.PayNovaError as e:
            raise ApiError(502, str(e)) from None
        return {"ok": True, "confirmed": done}
    if season.status != Season.INVITING or st.status not in (SeasonTeam.INVITED, SeasonTeam.ACCEPTED):
        raise ApiError(400, "This invitation can't be changed any more.")
    if action == "decline":
        st.status, st.responded_by, st.responded_at = SeasonTeam.DECLINED, user, timezone.now()
        st.save(update_fields=["status", "responded_by", "responded_at"])
        log(org, user, f"declined the {season.name} invitation for {st.team.name}")
        return {"ok": True, "status": st.status}
    if action != "accept":
        raise ApiError(404, "Unknown action.")
    st.responded_by, st.responded_at = user, timezone.now()
    if season.fee and not st.paid:
        st.status = SeasonTeam.ACCEPTED
        if paynova.ready():
            back = f"{site_url(request)}/app/org/{org.slug}?pro={st.id}"
            try:
                pay = paynova.initialize_payment(season.fee, season.currency, f"{season.name}: entry fee for {st.team.name}", success_url=back,
                                                 cancel_url=back, metadata={"kind": "proleague", "seasonTeam": st.id}, email=user.email or "")
                st.reference, st.checkout_url = pay["reference"], pay["checkout_url"]
            except paynova.PayNovaError as e:
                st.save(update_fields=["status", "responded_by", "responded_at"])
                raise ApiError(502, f"Accepted, but the payment link couldn't be made: {e}") from None
    else:
        st.status = SeasonTeam.CONFIRMED
    st.save(update_fields=["status", "responded_by", "responded_at", "reference", "checkout_url"])
    log(org, user, f"accepted the {season.name} invitation for {st.team.name}")
    return {"ok": True, "status": st.status, "checkoutUrl": st.checkout_url or None}
