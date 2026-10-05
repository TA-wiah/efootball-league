"""Settings for the eFootball Champions League site.

Everything is configured with environment variables (or a .env file next to manage.py).
See .env.example for the full list with explanations.
"""
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)   # variables set in the shell win over the file
E = os.environ


def flag(name):
    return E.get(name, "") == "1"


DEBUG = flag("DEBUG")
APP_URL = E.get("APP_URL", "").rstrip("/")
TRUST_PROXY = flag("TRUST_PROXY")
SECURE = flag("COOKIE_SECURE") or APP_URL.startswith("https:")

# ---------- data ----------
DB_FILE = Path(E.get("DB_FILE") or BASE_DIR / "data" / "league.sqlite3")
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
# DATABASE_URL (e.g. a free Neon Postgres) keeps the league safe on hosts whose disk is wiped on restart.
DATABASES = {"default": dj_database_url.parse(E.get("DATABASE_URL") or f"sqlite:///{DB_FILE.as_posix()}", conn_max_age=600, conn_health_checks=True)}
SEED_FILE = Path(E.get("SEED_FILE") or BASE_DIR / "seed.json")
INDEX_FILE = BASE_DIR / "public" / "index.html"
APP_FILE = BASE_DIR / "public" / "app.html"     # the platform: sign-up, organizations, dashboards


def _secret_key():
    if E.get("SECRET_KEY"):
        return E["SECRET_KEY"]
    # No SECRET_KEY set: keep one in a file so logins survive restarts on hosts with a real disk.
    f = DB_FILE.parent / "secret_key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(50))
    return f.read_text().strip()


SECRET_KEY = _secret_key()

# ---------- hosts ----------
hosts = [h.strip() for h in E.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
if APP_URL:
    hosts.append(urlparse(APP_URL).hostname)
if E.get("KOYEB_PUBLIC_DOMAIN"):
    hosts.append(E["KOYEB_PUBLIC_DOMAIN"])
ALLOWED_HOSTS = hosts + ["localhost", "127.0.0.1", "[::1]", "testserver"] if hosts else ["*"]
CSRF_TRUSTED_ORIGINS = [APP_URL] if APP_URL else []
if TRUST_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------- app ----------
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "league",
    "orgs",
    "competitions",
]
MIDDLEWARE = [
    "league.middleware.health",                       # answers /api/health before host checks
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "league.middleware.security_headers",
]
ROOT_URLCONF = "league_site.urls"
WSGI_APPLICATION = "league_site.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": False,
    "OPTIONS": {"context_processors": [], "autoescape": True},
}]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = E.get("TIME_ZONE") or "UTC"     # used to group public fixtures by day, e.g. Africa/Accra
APPEND_SLASH = False
DATA_UPLOAD_MAX_MEMORY_SIZE = 1_100_000

# ---------- accounts ----------
AUTH_USER_MODEL = "league.Admin"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.ScryptPasswordHasher",     # strong, memory-hard hashing
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# ---------- sessions & CSRF ----------
SESSION_COOKIE_NAME = "__Host-s" if SECURE else "s"
SESSION_COOKIE_AGE = 7 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = SECURE
CSRF_COOKIE_NAME = "__Host-csrf" if SECURE else "csrf"
CSRF_COOKIE_HTTPONLY = True            # the page gets the token from /api/me instead
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = SECURE
CSRF_HEADER_NAME = "HTTP_X_CSRF"
CSRF_FAILURE_VIEW = "league.http.csrf_failure"

# ---------- security headers ----------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if SECURE:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.security.DisallowedHost": {"level": "ERROR"}},
}
