"""Sending email for invites and password resets.

Providers (EMAIL_PROVIDER):
  brevo   – Brevo's HTTPS API (free 300/day). Works on hosts that block email ports. Needs EMAIL_API_KEY.
  resend  – Resend's HTTPS API. Needs EMAIL_API_KEY and a verified domain.
  smtp    – any SMTP server (Gmail etc.). Some hosts block ports 587/465.
  console – prints emails to the server log (for testing).
If EMAIL_PROVIDER isn't set, SMTP is used when SMTP_HOST is set.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request
from email.utils import parseaddr
from html import escape

from django.core.mail import EmailMultiAlternatives, get_connection

log = logging.getLogger("league")
SITE = "eFootball Champions League"


def _env(name, default=""):
    return os.environ.get(name, default)


def config():
    """Email settings: the ones saved in the admin panel win; otherwise the environment variables."""
    try:
        from superadmin.store import get
        db = get("email")
    except Exception:                       # settings table not ready yet (first start)
        db = {}
    if db.get("provider"):
        return {"provider": db["provider"].lower(), "host": db.get("host", ""), "port": int(db.get("port") or 587),
                "user": db.get("user", ""), "password": db.get("password", ""), "from": db.get("from", ""),
                "api_key": db.get("api_key", ""), "secure": db.get("secure", ""), "source": "admin panel"}
    return {"provider": (_env("EMAIL_PROVIDER") or ("smtp" if _env("SMTP_HOST") else "")).lower(), "host": _env("SMTP_HOST"),
            "port": int(_env("SMTP_PORT", "587") or 587), "user": _env("SMTP_USER"), "password": _env("SMTP_PASS"),
            "from": _env("EMAIL_FROM") or _env("SMTP_FROM") or _env("SMTP_USER"), "api_key": _env("EMAIL_API_KEY"),
            "secure": _env("SMTP_SECURE"), "source": "environment"}


def provider():
    return config()["provider"]


def sender(cfg=None):
    cfg = cfg or config()
    name, addr = parseaddr(cfg["from"] or cfg["user"])
    return name or SITE, addr


def ready(cfg=None):
    cfg = cfg or config()
    p = cfg["provider"]
    if p in ("brevo", "resend"):
        return bool(cfg["api_key"] and sender(cfg)[1])
    if p == "smtp":
        return bool(cfg["host"] and sender(cfg)[1])
    return p == "console"


class MailError(Exception):
    pass


def _post(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise MailError(f"{e.code} from the email service: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise MailError(f"could not reach the email service ({getattr(e, 'reason', e)})") from None


def send(to, subject, text, html):
    """Send one email or raise MailError with a readable reason."""
    if not re.fullmatch(r"[^\s@<>()\",;:]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}", to or ""):
        raise MailError("bad recipient")
    subject = re.sub(r"[\r\n]+", " ", subject).strip()
    cfg = config()
    name, addr = sender(cfg)
    p = cfg["provider"]
    if not ready(cfg):
        raise MailError("email is not set up")
    if p == "brevo":
        _post("https://api.brevo.com/v3/smtp/email", {"api-key": cfg["api_key"]},
              {"sender": {"name": name, "email": addr}, "to": [{"email": to}], "subject": subject, "textContent": text, "htmlContent": html})
    elif p == "resend":
        _post("https://api.resend.com/emails", {"Authorization": f"Bearer {cfg['api_key']}"},
              {"from": f"{name} <{addr}>", "to": [to], "subject": subject, "text": text, "html": html})
    elif p == "console":
        log.info("EMAIL to %s: %s\n%s", to, subject, text)
    else:
        port = cfg["port"]
        use_ssl = str(cfg["secure"]) == "1" if cfg["secure"] not in ("", None) else port == 465
        conn = get_connection("django.core.mail.backends.smtp.EmailBackend", host=cfg["host"], port=port,
                              username=cfg["user"] or None, password=cfg["password"] or None,
                              use_ssl=use_ssl, use_tls=not use_ssl, timeout=20)   # never sends the password unencrypted
        msg = EmailMultiAlternatives(subject, text, f"{name} <{addr}>", [to], connection=conn)
        msg.attach_alternative(html, "text/html")
        try:
            msg.send()
        except Exception as e:   # smtplib raises many types; show the reason to the admin
            reason = str(e) or e.__class__.__name__
            if "timed out" in reason.lower() or isinstance(e, TimeoutError):
                reason += " (your host may block email ports; try EMAIL_PROVIDER=brevo)"
            raise MailError(f"SMTP: {reason}") from None


def body(title, lines, link, button, foot):
    text = "\n".join([title, "", *lines, "", link, "", foot])
    html = (f'<div style="font-family:system-ui,Segoe UI,Roboto,sans-serif;max-width:480px;margin:auto;padding:24px;background:#0a1a48;color:#f5f8ff;border-radius:14px">'
            f'<h2 style="color:#ffcd46;margin:0 0 12px">{escape(title)}</h2>' + "".join(f"<p>{escape(x)}</p>" for x in lines) +
            f'<p style="text-align:center;margin:24px 0"><a href="{escape(link)}" style="background:#ffcd46;color:#080e30;padding:12px 22px;border-radius:10px;font-weight:800;text-decoration:none">{escape(button)}</a></p>'
            f'<p style="font-size:12px;color:#96afeb;word-break:break-all">{escape(link)}</p><p style="font-size:12px;color:#96afeb">{escape(foot)}</p></div>')
    return text, html
