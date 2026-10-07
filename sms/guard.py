"""Stops harmful texts before they're sent: scams, requests for secrets, threats, and links to unknown sites.

The rules are deliberately strict: texts go out under the platform's sender name, so a scam sent through an
organization would look like it came from us. A blocked text costs no credits and is kept for the super admin to see.
"""
import re
from urllib.parse import urlparse

from django.conf import settings

SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rb.gy", "shorturl.at", "tiny.cc",
              "rebrand.ly", "s.id", "t.ly", "bl.ink", "lnkd.in", "v.gd"}
ALWAYS_ALLOWED = {"paynova.com"}

RULES = [
    # asking people for secrets
    ("asks for a PIN, password, code or card details",
     re.compile(r"\b(pin|password|passcode|otp|one[- ]time (pass)?code|verification code|cvv|cvc|card number|security code|"
                r"momo pin|mobile money pin)\b.{0,60}\b(send|share|give|reply|tell|enter|text|forward|provide|confirm)\b|"
                r"\b(send|share|give|reply|tell|enter|text|forward|provide|confirm)\b.{0,60}\b(pin|password|passcode|otp|"
                r"verification code|cvv|card number|security code)\b", re.I | re.S)),
    # prize and lottery scams
    ("looks like a prize or lottery scam",
     re.compile(r"\b(you('ve| have)? (won|been selected)|winner of (our|the) (promo|draw|lottery)|lottery|jackpot|claim your (prize|reward|"
                r"winnings))\b.{0,120}\b(pay|send|fee|charge|transfer|deposit|momo|airtime)\b", re.I | re.S)),
    # "send money to this number"
    ("asks people to send money to a number or account",
     re.compile(r"\b(send|transfer|momo|deposit|pay)\b.{0,40}\b(money|cash|cedis?|ghs|momo|airtime|amount)?\b.{0,20}\b(to|into|on)\b.{0,20}"
                r"(\+?\d[\d\s-]{8,}|this (number|account|line)|my (number|account|momo|wallet))", re.I | re.S)),
    ("contains a reversal or wrong-transfer trick",
     re.compile(r"\b(sent|transferred)\b.{0,40}\b(by mistake|wrongly|in error)\b.{0,80}\b(send|reverse|return|refund)\b", re.I | re.S)),
    # threats and incitement
    ("contains a threat",
     re.compile(r"\b(i|we)('ll| will| am going to| are going to| go)\s+(kill|shoot|stab|hurt|beat|burn|attack|destroy)\b|"
                r"\b(kill|shoot|stab|burn)\s+(you|him|her|them|your (family|house))\b|\b(bomb|explosive|gun)s?\b.{0,40}\b(you|your|the "
                r"(stadium|pitch|match|park))\b|\byou('re| are)? (dead|going to die)\b", re.I | re.S)),
]
URL_RE = re.compile(r"\b((?:https?://|www\.)[^\s<>\"']+|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|me|info|xyz|link|click|top|site|"
                    r"online|app|gh|ng|ke|za|uk|ly|at|gd|ws|cc|tk|ml|ga|cf|gq)(?:/[^\s]*)?)", re.I)


def allowed_hosts():
    """PayNova, and this platform's own addresses (the site address, APP_URL and the allowed host names)."""
    from league.http import site_url
    hosts = set(ALWAYS_ALLOWED)
    for url in (site_url(), settings.APP_URL):
        if url:
            hosts.add((urlparse(url).hostname or "").lower())
    for h in settings.ALLOWED_HOSTS:
        h = h.lower().lstrip(".")
        if h and h != "*" and "*" not in h:
            hosts.add(h)
    return {h for h in hosts if h}


def problems(text, running_host=""):
    """Why this text must not be sent (empty when it's fine). `running_host` is this site's address when APP_URL isn't set."""
    found = [reason for reason, rule in RULES if rule.search(text)]
    ok_hosts = allowed_hosts()
    from league.http import site_url
    if running_host and not site_url():
        ok_hosts.add(running_host.split(":")[0].lower())
    for raw in URL_RE.findall(text):
        url = raw if raw.lower().startswith("http") else "http://" + raw
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host in SHORTENERS:
            found.append(f"uses a link shortener ({host}); write the full address instead")
        elif not any(host == h or host.endswith("." + h) for h in ok_hosts):
            found.append(f"links to {host}; only links to this platform and PayNova are allowed")
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out
