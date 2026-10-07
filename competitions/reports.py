"""Match reports from the teams themselves: "We're here" check-ins, screenshots as proof (start, end, result…),
and settling Pro League matches nobody reported: a walkover for the team that showed up, "no show" when neither did.

Who can report for a team: people in the team's organization who manage teams (or its settings), and the team's own
assigned people (team managers, coaches, players). Screenshots are private: only the two teams and the competition's
staff can see them.
"""
import time

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

from league.http import ApiError, body, endpoint, ms, session_user, text
from league.logic import hit
from orgs.api import log
from orgs.models import Membership
from orgs.permissions import can

from . import bracket, engine
from .models import Match, MatchProof, Team

MAX_PROOFS = 12                  # per team per match
CHECKIN_EARLY = 60               # minutes before kick-off "We're here" opens


def pro_settings():
    from proleague.logic import config
    return config()


def is_pro(match):
    from proleague.logic import is_pro as _is_pro
    return _is_pro(match.competition)


def deadline(match):
    """When an unreported Pro League match gets settled (None if it never does)."""
    cfg = pro_settings()
    if not match.kickoff or not cfg["walkovers"] or not is_pro(match):
        return None
    return match.kickoff + timezone.timedelta(hours=cfg["walkover_hours"])


def sides_for(user, match):
    """Which side(s) this person can report for."""
    out = []
    for side, entry in (("home", match.home), ("away", match.away)):
        if not entry:
            continue
        team = entry.team
        m = Membership.objects.select_related("org").filter(org_id=team.org_id, user=user).first()
        if m and (can(m.role, "org.settings", m.org) or can(m.role, "teams.manage", m.org) or m.teams.filter(id=team.id).exists()):
            out.append(side)
    return out


def is_staff(user, match):
    if user.is_superuser:
        return True
    m = Membership.objects.select_related("org").filter(org_id=match.competition.org_id, user=user).first()
    return bool(m and can(m.role, "results.enter", m.org))


def match_for(user, match_id):
    m = Match.objects.select_related("competition__org", "home__team__org", "away__team__org").filter(id=match_id).first()
    if not m:
        raise ApiError(404, "Match not found.")
    sides, staff = sides_for(user, m), is_staff(user, m)
    if not sides and not staff:
        raise ApiError(404, "Match not found.")
    return m, sides, staff


def proof_json(p):
    return {"id": p.id, "side": p.side, "kind": p.kind, "kindLabel": MatchProof.KINDS.get(p.kind, p.kind), "url": f"/media/proof/{p.id}",
            "note": p.note or None, "homeScore": p.home_score, "awayScore": p.away_score, "by": getattr(p.uploaded_by, "username", None),
            "created": ms(p.created)}


def report_json(m, sides, staff):
    from proleague.logic import whatsapp_link
    dl = deadline(m)
    now = timezone.now()
    opens = m.kickoff - timezone.timedelta(minutes=CHECKIN_EARLY) if m.kickoff else None
    return {"match": {"id": m.id, "slug": m.slug, "status": m.status, "kickoff": m.kickoff.isoformat() if m.kickoff else None,
                      "home": m.home.team.name if m.home else bracket.label(m.home_from) or "To be decided",
                      "away": m.away.team.name if m.away else bracket.label(m.away_from) or "To be decided",
                      "homeScore": m.home_score, "awayScore": m.away_score, "decided": m.decided or None, "notes": m.notes or None,
                      "competition": {"name": m.competition.name, "slug": m.competition.slug, "org": m.competition.org.slug},
                      "round": m.round_name or f"Round {m.round}"},
            "sides": sides, "staff": staff, "checkin": {"home": ms(m.home_checkin), "away": ms(m.away_checkin)},
            "checkinOpen": bool(opens and opens <= now and (dl is None or now <= dl) and m.status in ("scheduled", "live")),
            "checkinOpens": opens.isoformat() if opens else None, "deadline": dl.isoformat() if dl else None,
            "whatsapp": whatsapp_link(m) if is_pro(m) else None, "kinds": MatchProof.KINDS,
            "proofs": [proof_json(p) for p in m.proofs.select_related("uploaded_by").defer("image")]}


def side_of(b, sides):
    side = b.get("side") if b.get("side") in ("home", "away") else (sides[0] if len(sides) == 1 else None)
    if side not in sides:
        raise ApiError(403, "You can only report for your own team.")
    return side


def check_in(m, side):
    field = f"{side}_checkin"
    if getattr(m, field) is None:
        setattr(m, field, timezone.now())
        m.save(update_fields=[field])
        return True
    return False


# ---------- endpoints ----------
@endpoint("GET", login_required=True)
def report(request, user, ip, match_id):
    settle_overdue()
    m, sides, staff = match_for(user, match_id)
    return report_json(m, sides, staff)


@endpoint("POST", login_required=True)
def report_checkin(request, user, ip, match_id):
    m, sides, staff = match_for(user, match_id)
    side = side_of(body(request), sides)
    data = report_json(m, sides, staff)
    if not data["checkinOpen"]:
        raise ApiError(400, "Check-in opens an hour before kick-off and closes at the deadline.")
    if check_in(m, side):
        log(m.competition.org, user, f"{(m.home if side == 'home' else m.away).team.name} checked in for match #{m.id}")
    return report_json(m, sides, staff)


@endpoint("POST", login_required=True)
def report_proof(request, user, ip, match_id):
    m, sides, staff = match_for(user, match_id)
    b = body(request, 1_090_000)
    side = side_of(b, sides)
    kind = b.get("kind") if b.get("kind") in MatchProof.KINDS else "other"
    if m.proofs.filter(side=side).count() >= MAX_PROOFS:
        raise ApiError(400, f"A team can send up to {MAX_PROOFS} screenshots for a match. Delete one first.")
    if hit(f"proof:{user.id}", 40, 3600):
        raise ApiError(429, "Too many uploads. Try again later.")
    try:
        raw, ctype = engine.decode_image(b.get("image"), engine.MAX_PROOF)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    hs = as_ = None
    if kind == "result" and b.get("homeScore") not in (None, "") and b.get("awayScore") not in (None, ""):
        try:
            hs, as_ = int(b["homeScore"]), int(b["awayScore"])
        except (TypeError, ValueError):
            raise ApiError(400, "The score must be whole numbers.") from None
        if not (0 <= hs <= 99 and 0 <= as_ <= 99):
            raise ApiError(400, "The score must be from 0 to 99.")
    p = MatchProof.objects.create(match=m, side=side, kind=kind, image=raw, content_type=ctype, note=text(b, "note", 200),
                                  home_score=hs, away_score=as_, uploaded_by=user)
    if m.status in ("scheduled", "live"):
        check_in(m, side)                                  # sending proof also counts as showing up
    team = (m.home if side == "home" else m.away).team.name
    log(m.competition.org, user, f"{team} sent a screenshot ({MatchProof.KINDS[kind].lower()}) for match #{m.id}"
                                 + (f", saying {hs}-{as_}" if hs is not None else ""))
    return {"ok": True, "proof": proof_json(p), **report_json(m, sides, staff)}


@endpoint("DELETE", login_required=True)
def report_proof_delete(request, user, ip, match_id, proof_id):
    m, sides, staff = match_for(user, match_id)
    p = m.proofs.filter(id=proof_id).first()
    if not p:
        raise ApiError(404, "Screenshot not found.")
    if not (staff or p.uploaded_by_id == user.id):
        raise ApiError(403, "Only whoever sent it (or the league) can delete it.")
    p.delete()
    return report_json(m, sides, staff)


@endpoint("GET", login_required=True)
def my_matches(request, user, ip):
    """Matches of the teams this person reports for: from 30 days ago to 60 days ahead."""
    settle_overdue()
    team_ids = set()
    for mb in Membership.objects.select_related("org").filter(user=user):
        if can(mb.role, "org.settings", mb.org) or can(mb.role, "teams.manage", mb.org):
            team_ids |= set(Team.objects.filter(org=mb.org).values_list("id", flat=True))
        team_ids |= set(mb.teams.values_list("id", flat=True))
    now = timezone.now()
    qs = (Match.objects.select_related("competition__org", "home__team", "away__team")
          .filter(Q(home__team_id__in=team_ids) | Q(away__team_id__in=team_ids))
          .filter(Q(kickoff__gte=now - timezone.timedelta(days=30), kickoff__lte=now + timezone.timedelta(days=60)) | Q(kickoff__isnull=True, status="scheduled"))
          .order_by("kickoff", "id")[:150])
    out = []
    for m in qs:
        mine = [s for s, e in (("home", m.home), ("away", m.away)) if e and e.team_id in team_ids]
        out.append({"id": m.id, "slug": m.slug, "kickoff": m.kickoff.isoformat() if m.kickoff else None, "status": m.status, "decided": m.decided or None,
                    "home": m.home.team.name if m.home else "To be decided", "away": m.away.team.name if m.away else "To be decided",
                    "homeScore": m.home_score, "awayScore": m.away_score, "competition": m.competition.name, "pro": is_pro(m), "mine": mine,
                    "checkedIn": all(getattr(m, f"{s}_checkin") for s in mine) if mine else False,
                    "proofs": m.proofs.filter(side__in=mine).count()})
    return {"matches": out}


def proof_file(request, proof_id):
    user = session_user(request)
    p = MatchProof.objects.select_related("match__competition__org", "match__home__team", "match__away__team").filter(id=proof_id).first()
    if not user or not p or not (sides_for(user, p.match) or is_staff(user, p.match)):
        return HttpResponse(status=404)
    return HttpResponse(bytes(p.image), content_type=p.content_type,
                        headers={"Cache-Control": "private, max-age=3600", "Content-Disposition": "inline",
                                 "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff"})


# ---------- settling matches nobody reported ----------
_last = {"t": 0.0}


def settle_overdue(force=False):
    """Pro League matches past their deadline with no result: a walkover for the only team that showed up,
    "not played" when neither did. Both showed up but no score: left for the league to decide. Runs at most every
    5 minutes (or on demand from the settle_walkovers command)."""
    now_t = time.monotonic()
    if not force and now_t - _last["t"] < 300:
        return 0
    _last["t"] = now_t
    cfg = pro_settings()
    if not cfg["walkovers"] or not cfg["org_id"]:
        return 0
    now = timezone.now()
    limit = now - timezone.timedelta(hours=cfg["walkover_hours"])
    todo = list(Match.objects.select_related("competition__org", "home__team", "away__team")
                .filter(competition__org_id=cfg["org_id"], status__in=["scheduled", "live"], home_score__isnull=True,
                        kickoff__isnull=False, kickoff__lt=limit, home__isnull=False, away__isnull=False))
    win = cfg["walkover_score"]
    done = 0
    for m in todo:
        with transaction.atomic():
            m = Match.objects.select_for_update().select_related("competition__org", "home__team", "away__team").get(pk=m.pk)
            if m.status not in ("scheduled", "live") or m.home_score is not None:
                continue
            h, a = bool(m.home_checkin), bool(m.away_checkin)
            if h and a:
                continue                                     # both played: the league decides from the screenshots
            if h or a:
                winner = m.home if h else m.away
                loser = m.away if h else m.home
                m.home_score, m.away_score = (win, 0) if h else (0, win)
                m.status, m.decided, m.finished_at = "finished", "walkover", now
                m.notes = (m.notes + "\n" if m.notes else "") + f"Walkover: {winner.team.name} showed up, {loser.team.name} didn't."
                msg = f"walkover to {winner.team.name} ({loser.team.name} didn't show up)"
            else:
                m.status, m.decided = "cancelled", "no_show"
                m.notes = (m.notes + "\n" if m.notes else "") + "Not played: neither team showed up."
                msg = f"{m.home.team.name} v {m.away.team.name} not played: neither team showed up"
            m.save()
        log(m.competition.org, None, msg)
        bracket.resolve(m.competition)
        done += 1
    return done
