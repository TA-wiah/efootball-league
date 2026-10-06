"""Sending texts. The super admin picks the provider in the platform settings; keys stay on the server.

    moolre   - Moolre (moolre.com): POST https://api.moolre.com/open/sms/send with the X-API-VASKEY header
    arkesel  - Arkesel (sms.arkesel.com): POST /api/v2/sms/send with the api-key header
    mnotify  - mNotify / BMS (api.mnotify.com): POST /api/sms/quick?key=…
    console  - testing: nothing is sent, the text is written to the server log
"""
import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("sms")
GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€")
PROVIDERS = {"moolre": "Moolre", "arkesel": "Arkesel", "mnotify": "mNotify (SMS Bulk)", "console": "Test mode (nothing is sent)"}


class SmsError(Exception):
    pass


def config():
    from superadmin import store
    return store.get("sms")


def ready(cfg=None):
    cfg = cfg or config()
    p = cfg.get("provider")
    if not cfg.get("enabled") or p not in PROVIDERS:
        return False
    if p in ("moolre", "arkesel", "mnotify"):
        return bool(cfg.get("api_key") and cfg.get("sender"))
    return True


def parts(text):
    """How many SMS a text takes (each part costs one credit per person)."""
    if not text:
        return 0
    if all(ch in GSM for ch in text):
        return 1 if len(text) <= 160 else math.ceil(len(text) / 153)
    return 1 if len(text) <= 70 else math.ceil(len(text) / 67)        # emoji and other characters make shorter parts


def normalize(phone, country="233"):
    """+233241234567 → 233241234567; 0241234567 → 233241234567 (the platform's country code). None if it isn't a number."""
    p = re.sub(r"[\s().-]", "", str(phone or ""))
    if p.startswith("+"):
        p = p[1:]
    elif p.startswith("00"):
        p = p[2:]
    elif p.startswith("0"):
        p = (country or "233") + p[1:]
    return p if re.fullmatch(r"[1-9]\d{8,14}", p) else None


def _post(url, data, headers, form=False, timeout=20):
    raw = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    req = urllib.request.Request(url, data=raw, method="POST", headers={**headers, "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
                                                                      "Accept": "application/json", "User-Agent": "CompetitionManager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:            # noqa: S310 (fixed https addresses)
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        msg = (body.get("message") or body.get("error") or "") if isinstance(body, dict) else ""
        if e.code in (401, 403):
            msg = "the SMS provider refused the key"
        raise SmsError(f"SMS provider: {msg or 'request failed'} ({e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SmsError("Couldn't reach the SMS provider. Try again in a minute.") from None
    except ValueError:
        raise SmsError("The SMS provider sent an answer that couldn't be read.") from None


def send(numbers, text, cfg=None):
    """Send one text to normalized numbers. Returns the provider's reference (or "")."""
    cfg = cfg or config()
    p = cfg.get("provider")
    if not ready(cfg):
        raise SmsError("SMS isn't set up on this platform yet.")
    if p == "console":
        log.warning("SMS (test mode) to %s: %s", ", ".join(numbers), text)
        return "test"
    if p == "arkesel":
        res = _post("https://sms.arkesel.com/api/v2/sms/send", {"sender": cfg["sender"], "message": text, "recipients": numbers},
                    {"api-key": cfg["api_key"]})
        if str(res.get("status", "")).lower() not in ("success", "ok"):
            raise SmsError(f"SMS provider: {res.get('message') or 'not sent'}")
        return str((res.get("data") or [{}])[0].get("id", "") if isinstance(res.get("data"), list) else "")[:80]
    if p == "moolre":
        res = _post("https://api.moolre.com/open/sms/send",
                    {"type": 1, "senderid": cfg["sender"], "messages": [{"recipient": n, "message": text} for n in numbers]},
                    {"X-API-VASKEY": cfg["api_key"]})
        if str(res.get("status")) != "1":
            raise SmsError(f"SMS provider: {res.get('message') or 'not sent'} ({res.get('code', '')})")
        return str(res.get("code") or "")[:80]
    if p == "mnotify":
        # mNotify takes the key in the address and local numbers (0241234567) for Ghana
        local = ["0" + n[3:] if n.startswith("233") else n for n in numbers]
        res = _post("https://api.mnotify.com/api/sms/quick?" + urllib.parse.urlencode({"key": cfg["api_key"]}),
                    {"recipient": local, "sender": cfg["sender"], "message": text, "is_schedule": False, "schedule_date": ""}, {})
        if str(res.get("status", "")).lower() != "success" or str(res.get("code", "")) != "2000":
            raise SmsError(f"SMS provider: {res.get('message') or 'not sent'} ({res.get('code', '')})")
        return str((res.get("summary") or {}).get("_id", ""))[:80]
    raise SmsError("Choose an SMS provider in the platform settings.")
