"""Helpers shared by every JSON endpoint: errors, request parsing, sessions, login checks and serving pages."""
import functools
import json
import re
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.hashers import make_password
from django.core.exceptions import RequestDataTooBig
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .logic import audit, clear, hit, over

BACKEND = "django.contrib.auth.backends.ModelBackend"
GENERIC = "Wrong username or password."


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def ip_of(request):
    if settings.TRUST_PROXY:
        fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.META.get("REMOTE_ADDR", "")


def body(request, max_len=10_000):
    if "application/json" not in request.META.get("CONTENT_TYPE", ""):
        raise ApiError(415, "json only")
    try:
        raw = request.body
    except RequestDataTooBig:
        raise ApiError(413, "too big") from None
    if len(raw) > max_len:
        raise ApiError(413, "too big")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ApiError(400, "bad json") from None
    if not isinstance(data, dict):
        raise ApiError(400, "bad json")
    return data


def text(b, key, limit):
    v = b.get(key)
    return v.strip()[:limit] if isinstance(v, str) else ""


def ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def session_user(request):
    """The signed-in user, or None. Also enforces "log out on all devices"."""
    u = request.user
    if not u.is_authenticated or not u.is_active or not u.has_usable_password():
        return None
    if request.session.get("epoch") != u.session_epoch:
        logout(request)
        return None
    now = timezone.now()
    if not u.last_seen or (now - u.last_seen).total_seconds() > 300:     # "last activity", at most one write per 5 minutes
        get_user_model().objects.filter(pk=u.pk).update(last_seen=now)
        u.last_seen = now
    return u


def start_session(request, user):
    login(request, user, backend=BACKEND)        # new session id + new CSRF token
    request.session["epoch"] = user.session_epoch


def check_login(ident, password, ip, eligible=lambda u: True):
    """Verify a username/email + password with lockouts. Returns the user or raises ApiError (same answer for every failure)."""
    from .models import Admin
    if over("ip:" + ip, 10, 900):
        raise ApiError(429, "Too many failed attempts. Try again in 15 minutes.")
    u = Admin.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first() if ident else None
    acct = f"acct:{u.id if u else ident.lower()}"
    if over(acct, 5, 900):
        raise ApiError(429, "This account is locked for 15 minutes after too many failed attempts.")
    if u is None or not u.has_usable_password() or not eligible(u):
        make_password(password)                  # same amount of work, so timing doesn't reveal unknown users
        good = False
    else:
        good = u.check_password(password)
    if not good:
        hit("ip:" + ip, 10, 900)
        hit(acct, 5, 900)
        audit(ident or "?", "failed login", ip, resource=f"user:{u.username}" if u else "", status="failed")
        raise ApiError(401, GENERIC)
    clear(acct)
    if not u.is_active:      # only said after the right password, so it can't be used to probe accounts
        audit(u.username, "login blocked: account suspended", ip, resource=f"user:{u.username}", status="denied")
        raise ApiError(403, "This account has been suspended. Contact the platform's support.")
    return u


def endpoint(*methods, login_required=False):
    """Wrap a view taking (request, user, ip, **url_kwargs): method check, optional login, JSON errors."""
    def wrap(view):
        @functools.wraps(view)
        def inner(request, **kwargs):
            if request.method not in methods:
                return JsonResponse({"error": "not found"}, status=404)
            user = session_user(request)
            if login_required and not user:
                return JsonResponse({"error": "Please log in."}, status=401)
            if user and user.must_change and login_required:
                return JsonResponse({"error": "Choose a new password first."}, status=403)
            try:
                result = view(request, user, ip_of(request), **kwargs)
            except ApiError as e:
                return JsonResponse({"error": e.message, **e.extra}, status=e.status)
            return result if isinstance(result, HttpResponse) else JsonResponse(result)
        return inner
    return wrap


def base_url(request):
    return settings.APP_URL or request.build_absolute_uri("/").rstrip("/")


_pages = {}


FONT_RE = re.compile(r"^[a-z0-9-]+\.(woff2|css|txt)$")
FONT_TYPES = {"woff2": "font/woff2", "css": "text/css; charset=utf-8", "txt": "text/plain; charset=utf-8"}


def font_file(request, name):
    """The site's own fonts (public/fonts): no visitor data goes to a font service."""
    from django.conf import settings
    if not FONT_RE.fullmatch(name):
        return not_found(request)
    path = settings.BASE_DIR / "public" / "fonts" / name
    if not path.is_file():
        return not_found(request)
    ext = name.rsplit(".", 1)[1]
    return HttpResponse(path.read_bytes(), content_type=FONT_TYPES[ext],
                        headers={"Cache-Control": "public, max-age=31536000, immutable" if ext == "woff2" else "public, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


def serve_page(request, path):
    """Serve an HTML file with a fresh CSP nonce on its inline scripts."""
    if request.method != "GET":
        return not_found(request)
    if path not in _pages or settings.DEBUG:
        _pages[path] = path.read_text(encoding="utf-8")
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return HttpResponse(_pages[path].replace("<script>", f'<script nonce="{nonce}">'), content_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


def csrf_failure(request, reason=""):
    return JsonResponse({"error": "Session check failed. Reload the page."}, status=403)


def not_found(request, exception=None):
    if request.path.startswith("/api/"):
        return JsonResponse({"error": "not found"}, status=404)
    return HttpResponse("<!doctype html><meta charset=utf-8><title>Not found</title><p style='font-family:system-ui;padding:40px'>Page not found. <a href='/'>Go home</a></p>", status=404)


def server_error(request):
    return JsonResponse({"error": "server error"}, status=500)
