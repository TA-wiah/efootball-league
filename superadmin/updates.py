"""What's new: a log of every update to the platform. Each entry is for everyone (shown on the public /updates page:
what players, coaches and team managers can do) or for chosen roles only (organization owners, organizers…, or super
admins), who see it on the What's new page of their dashboard. Super admins write them in the admin panel."""
from django.utils import timezone

from league.http import ApiError, body, endpoint, ms, text
from league.logic import audit
from orgs.models import Membership
from orgs.permissions import ROLE_INFO, ROLES

from .models import PlatformUpdate

PUBLIC, SUPER = "public", "superadmin"
AUDIENCES = [PUBLIC] + list(ROLES) + [SUPER]


def label(key):
    if key == PUBLIC:
        return "Everyone"
    if key == SUPER:
        return "Super admins"
    return ROLE_INFO[key][0] + "s"


def blocks(body_text):
    """Plain text to paragraphs and bullet lists ("- " lines)."""
    out = []
    for line in (body_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line[:2] in ("- ", "* "):
            if not out or out[-1]["type"] != "list":
                out.append({"type": "list", "items": []})
            out[-1]["items"].append(line[2:].strip())
        else:
            out.append({"type": "p", "text": line})
    return out


def visible(user):
    """Published updates this person may see."""
    qs = PlatformUpdate.objects.filter(published=True).order_by("-date", "-id")
    if user and user.is_superuser:
        return list(qs[:300])
    roles = set(Membership.objects.filter(user=user).values_list("role", flat=True)) if user else set()
    return [u for u in qs[:600] if PUBLIC in u.audience or roles & set(u.audience)][:300]


def as_json(u, admin=False):
    d = {"id": u.id, "title": u.title, "body": u.body, "blocks": blocks(u.body), "date": u.date.isoformat(), "created": ms(u.created),
         "public": PUBLIC in u.audience, "audience": [label(a) for a in u.audience if a in AUDIENCES]}
    if admin:
        d.update(keys=u.audience, published=u.published, author=getattr(u.author, "username", None))
    return d


@endpoint("GET")
def updates(request, user, ip):
    return {"updates": [as_json(u) for u in visible(user)], "canManage": bool(user and user.is_superuser)}


def clean(b, u):
    if "title" in b:
        u.title = text(b, "title", 140)
        if len(u.title) < 2:
            raise ApiError(400, "Give the update a title.")
    if "body" in b:
        u.body = text(b, "body", 5000)
    if "audience" in b:
        aud = b["audience"]
        if not isinstance(aud, list) or not aud or any(a not in AUDIENCES for a in aud):
            raise ApiError(400, "Choose who sees this update.")
        u.audience = [PUBLIC] if PUBLIC in aud else [a for a in AUDIENCES if a in aud]
    if "date" in b:
        try:
            u.date = timezone.datetime.fromisoformat(str(b["date"])).date()
        except ValueError:
            raise ApiError(400, "Choose the date of the update.") from None
    if "published" in b:
        u.published = bool(b["published"])


@endpoint("GET", "POST", login_required=True)
def admin_updates(request, user, ip):
    if not user.is_superuser:
        raise ApiError(403, "Only platform admins can manage updates.")
    if request.method == "GET":
        return {"updates": [as_json(u, admin=True) for u in PlatformUpdate.objects.select_related("author").order_by("-date", "-id")[:500]],
                "audiences": [{"key": a, "label": label(a)} for a in AUDIENCES]}
    b = body(request, 20_000)
    u = PlatformUpdate(author=user, date=timezone.localdate(), published=True)
    clean({"audience": [PUBLIC], **b}, u)
    if not u.title:
        raise ApiError(400, "Give the update a title.")
    u.save()
    audit(user.username, "posted an update (What's new)", ip, resource="update", new=u.title)
    return {"ok": True, "update": as_json(u, admin=True)}


@endpoint("PATCH", "DELETE", login_required=True)
def admin_update_detail(request, user, ip, uid):
    if not user.is_superuser:
        raise ApiError(403, "Only platform admins can manage updates.")
    u = PlatformUpdate.objects.filter(id=uid).first()
    if not u:
        raise ApiError(404, "Update not found.")
    if request.method == "DELETE":
        u.delete()
        audit(user.username, "deleted an update (What's new)", ip, resource="update", old=u.title)
        return {"ok": True}
    clean(body(request, 20_000), u)
    u.save()
    return {"ok": True, "update": as_json(u, admin=True)}
