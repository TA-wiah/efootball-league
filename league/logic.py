"""League rules that don't depend on HTTP: validation, the starting league, saving, draws, rate limits."""
import json
import logging
import random
import re
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Audit, Draw, Hit, League, Token

log = logging.getLogger("league")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^\s@<>()\",;:]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
EV_KEY_RE = re.compile(r"^(r\.[A-H]\d+_\d+|k\.(r\d+t\d+\.l[12]|f\.s))$")
SLOT_RE = re.compile(r"^[A-H][123]$")
_rng = random.SystemRandom()   # the operating system's cryptographic random generator


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------- validation ----------
def valid_state(s):
    """Reject anything that isn't a well-formed league, so stored data can't carry markup or junk."""
    if not isinstance(s, dict) or not isinstance(s.get("g"), dict):
        return False
    g = s["g"]
    if not 1 <= len(g) <= 8:
        return False
    for k, names in g.items():
        if not re.fullmatch(r"[A-H]", k) or not isinstance(names, list) or len(names) > 6:
            return False
        if not all(isinstance(n, str) and 1 <= len(n) <= 40 for n in names):
            return False
    for f in ("r", "k", "st", "cfg", "aw", "ui", "ev", "dl"):
        if s.get(f) is not None and not isinstance(s[f], dict):
            return False
    kd = s.get("kd")
    if kd is not None and (not isinstance(kd, list) or len(kd) > 24 or not all(isinstance(x, str) and SLOT_RE.fullmatch(x) for x in kd)):
        return False
    if (s.get("cfg") or {}).get("kdr") not in (None, 0, 1):
        return False
    for v in (s.get("ui") or {}).values():
        if not isinstance(v, str) or len(v) > 40:
            return False
    for k, lst in (s.get("ev") or {}).items():
        if not EV_KEY_RE.fullmatch(k) or not isinstance(lst, list) or len(lst) > 30:
            return False
        for e in lst:
            if not isinstance(e, dict) or not is_int(e.get("s")) or e["s"] not in (0, 1):
                return False
            m = e.get("m")
            if m is not None and not (is_int(m) and 0 <= m <= 130):
                return False
            for f in ("p", "a"):
                v = e.get(f)
                if v is not None and (not isinstance(v, str) or len(v) > 30):
                    return False
    return True


def password_problem(password, user=None):
    if not isinstance(password, str) or len(password) > 200:
        return "Password is too long." if isinstance(password, str) else "Enter a password."
    try:
        validate_password(password, user)
    except ValidationError as e:
        return " ".join(e.messages)
    return None


# ---------- the league document ----------
def blank():
    return {"r": {}, "k": {}, "st": {}, "ev": {}, "aw": {"bd": "", "c": []}}


def seed():
    """The starting league from seed.json (players, groups, settings)."""
    s = {}
    try:
        s = json.loads(settings.SEED_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.error("Could not read %s: %s", settings.SEED_FILE, e)
    s = {**blank(), **(s if isinstance(s, dict) else {})}
    if not valid_state(s):
        if s.get("g"):
            log.error("%s is not a valid league (1–8 groups A–H, up to 6 names of 1–40 characters). Using a blank league.", settings.SEED_FILE)
        s = {**blank(), "g": {"A": ["Player 1", "Player 2", "Player 3", "Player 4"], "B": ["Player 5", "Player 6", "Player 7", "Player 8"]}}
    return s


def league_row():
    row = League.objects.filter(pk=1).first()
    if row is None:
        with transaction.atomic():
            row, created = League.objects.get_or_create(pk=1, defaults={"data": json.dumps(seed()), "rev": 1})
        if created:
            log.info("New league created from %s.", settings.SEED_FILE.name)
    return row


def read_state():
    row = league_row()
    s = json.loads(row.data)
    s["rev"] = row.rev
    return s


def save_if_current(s, base_rev):
    """Save only if nobody else saved since `base_rev`. Returns the new rev, or None on a conflict."""
    s.pop("rev", None)
    n = League.objects.filter(pk=1, rev=base_rev).update(data=json.dumps(s, separators=(",", ":")), rev=base_rev + 1)
    return base_rev + 1 if n else None


def force_save(s):
    """Server-made changes (draws). Always bumps rev so open editors notice."""
    league_row()
    s.pop("rev", None)
    League.objects.filter(pk=1).update(data=json.dumps(s, separators=(",", ":")), rev=F("rev") + 1)
    s["rev"] = League.objects.get(pk=1).rev
    return s


# ---------- draws ----------
def shuffle(items):
    items = list(items)
    _rng.shuffle(items)   # Fisher–Yates driven by the OS random generator: nobody can predict or steer it
    return items


def record_draw(me, kind, result):
    d = Draw.objects.create(ts=int(time.time() * 1000), by=me.username, role=me.role, kind=kind, result=json.dumps(result))
    return {"id": d.id, "ts": d.ts, "kind": kind}


def deal_groups(names, n_groups, pots):
    """Deal names into groups. With pots, each pot (n_groups names, strongest first) is spread one per group."""
    letters = "ABCDEFGH"[:n_groups]
    g = {x: [] for x in letters}
    order = []
    chunks = [names[i:i + n_groups] for i in range(0, len(names), n_groups)] if pots else [names]
    for pot in chunks:
        drawn = shuffle(pot)
        slots = []
        while len(slots) < len(drawn):   # fill the emptiest groups first, in random order: sizes never differ by more than one
            size = {x: len(g[x]) + slots.count(x) for x in letters}
            low = min(size.values())
            slots += shuffle([x for x in letters if size[x] == low])
        for name, slot in zip(drawn, slots):
            g[slot].append(name)
            order.append([slot, name])
    return g, order


# ---------- rate limits & audit ----------
def hit(key, limit, window):
    """Record an event; True when it goes over the limit."""
    now = time.time()
    Hit.objects.create(key=key, ts=now)
    if secrets.randbelow(50) == 0:
        cleanup()
    return Hit.objects.filter(key=key, ts__gte=now - window).count() > limit


def over(key, limit, window):
    """Check without recording."""
    return Hit.objects.filter(key=key, ts__gte=time.time() - window).count() >= limit


def clear(key):
    Hit.objects.filter(key=key).delete()


def cleanup():
    now = timezone.now()
    Hit.objects.filter(ts__lt=time.time() - 86400).delete()
    Token.objects.filter(expires__lt=now).delete()
    Audit.objects.filter(ts__lt=now - timedelta(days=90)).delete()


def audit(actor, action, ip="", resource="", old=None, new=None, status="ok", device=""):
    """Write one line to the platform audit log (shown to super admins)."""
    as_text = lambda v: "" if v is None else (v if isinstance(v, str) else json.dumps(v, default=str))[:2000]   # noqa: E731
    Audit.objects.create(actor=(actor or "")[:254], action=action[:300], ip=(ip or "")[:64], resource=(resource or "")[:200],
                         old=as_text(old), new=as_text(new), status=status[:10], device=(device or "")[:200])
