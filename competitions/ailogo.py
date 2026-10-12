"""AI logo images (optional): a super admin turns it on in Platform settings with an OpenAI API key; people who manage
teams or organizations can then describe a crest in the logo studio and get an image back.

The prompt always asks for an original crest that doesn't look like any real club's badge, and descriptions naming
well-known clubs or brands are refused, so nobody gets someone else's trademark. Each person has a daily limit.
"""
import json
import re
import time
import urllib.error
import urllib.request

from league.http import ApiError, body, endpoint, text
from league.logic import hit, over
from league.models import Hit
from orgs.models import Membership
from orgs.permissions import can
from superadmin import store

API = "https://api.openai.com/v1/images/generations"
# names that belong to someone else: describe your own design instead
TAKEN = ["real madrid", "barcelona", "barca", "manchester", "man united", "man city", "liverpool", "chelsea", "arsenal", "tottenham", "spurs",
         "juventus", "bayern", "dortmund", "psg", "paris saint", "inter milan", "ac milan", "napoli", "roma", "lazio", "atletico", "sevilla",
         "ajax", "benfica", "porto", "sporting", "celtic", "rangers", "galatasaray", "fenerbahce", "boca", "river plate", "flamengo",
         "al nassr", "al hilal", "inter miami", "hearts of oak", "kotoko", "nike", "adidas", "puma", "konami", "efootball", "ea sports",
         "fifa", "uefa", "premier league", "la liga", "serie a", "bundesliga", "champions league", "ballon d"]


def config():
    s = store.get("ai")
    return {"enabled": bool(s.get("enabled")) and bool(s.get("api_key")), "api_key": s.get("api_key", ""),
            "model": s.get("model") or "gpt-image-1", "daily_limit": int(s.get("daily_limit") or 5)}


def may_use(user):
    if user.is_superuser:
        return True
    return any(can(m.role, p, m.org) for m in Membership.objects.select_related("org").filter(user=user)
               for p in ("org.settings", "teams.manage", "teams.manage_assigned"))


def refused(words):
    w = " " + re.sub(r"[^a-z0-9]+", " ", (words or "").lower()) + " "
    return next((t for t in TAKEN if f" {t} " in w or (len(t) > 6 and t in w)), None)


def generate(cfg, prompt):
    payload = {"model": cfg["model"], "prompt": prompt, "size": "1024x1024", "n": 1}
    if cfg["model"].startswith("dall-e"):
        payload["response_format"] = "b64_json"
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 400 and "safety" in detail.lower():
            raise ApiError(400, "The AI service refused that description. Try describing it differently.") from None
        raise ApiError(502, "The AI image service isn't working right now. Please contact the platform administrator.") from None
    except (urllib.error.URLError, TimeoutError):
        raise ApiError(502, "The AI image service couldn't be reached. Try again in a moment.") from None
    b64 = ((data.get("data") or [{}])[0]).get("b64_json")
    if not b64:
        raise ApiError(502, "The AI image service didn't send an image back. Try again.")
    return "data:image/png;base64," + b64


@endpoint("GET", "POST", login_required=True)
def ai_logo(request, user, ip):
    cfg = config()
    key = f"ailogo:{user.id}"
    if request.method == "GET":
        used = Hit.objects.filter(key=key, ts__gte=time.time() - 86400).count()
        return {"enabled": cfg["enabled"] and may_use(user), "left": max(0, cfg["daily_limit"] - used), "limit": cfg["daily_limit"]}
    if not cfg["enabled"]:
        raise ApiError(400, "AI logo images aren't available. Please contact the platform administrator.")
    if not may_use(user):
        raise ApiError(403, "AI logo images are for people who manage teams or organizations.")
    b = body(request)
    desc, name = text(b, "prompt", 400), text(b, "name", 80)
    if len(desc) < 3:
        raise ApiError(400, "Describe the logo you'd like (colours, an animal or symbol, a style…).")
    bad = refused(desc) or refused(name)
    if bad:
        raise ApiError(400, f"“{bad.title()}” belongs to someone else. Describe your own original design instead.")
    if over(key, cfg["daily_limit"], 86400):
        raise ApiError(429, f"That's today's {cfg['daily_limit']} AI images. Try again tomorrow, or design one in the studio.")
    hit(key, cfg["daily_limit"] + 1, 86400)
    prompt = (f"An original sports team crest logo for an eFootball team called \"{name or 'the team'}\". {desc}. "
              "Flat vector emblem, bold clean shapes, centred on a plain solid background, readable at small sizes. "
              "It must be an original design that does not resemble any real football club's badge, brand or trademark, "
              "with no real club names and no words other than the team's initials.")
    return {"ok": True, "image": generate(cfg, prompt)}
