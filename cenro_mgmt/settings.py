import os
import sys
from pathlib import Path
import dj_database_url
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent

# -------------------------
# Core config
# -------------------------
SECRET_KEY = config("SECRET_KEY", default="replace-me-in-production")

def _debug():
    try:
        return config("DEBUG", default="False", cast=bool)
    except Exception:
        return False

DEBUG = _debug()

# Django's static() helper only mounts /media/ when DEBUG=True. Many local .env files use
# DEBUG=False, which breaks ImageField URLs (404). Serve uploads when DEBUG, when using
# runserver, or when SERVE_MEDIA=true (e.g. staging). Production behind nginx should
# serve MEDIA_ROOT separately and keep this False.
_serve_media_env = config("SERVE_MEDIA", default="")
if str(_serve_media_env).strip().lower() in ("1", "true", "yes", "on"):
    SERVE_MEDIA = True
elif str(_serve_media_env).strip().lower() in ("0", "false", "no", "off"):
    SERVE_MEDIA = False
else:
    SERVE_MEDIA = DEBUG or ("runserver" in sys.argv)

# -------------------------
# Hosts & CSRF (Render-safe)
# -------------------------

# Hostnames only (NO http/https). Comma-separated in env.
# Safe defaults for local dev and any *.onrender.com host.
try:
    _allowed_hosts_env = config(
        "ALLOWED_HOSTS",
        default="localhost,127.0.0.1,.onrender.com",
        cast=Csv(),
    )
except Exception:
    _allowed_hosts_env = ["localhost", "127.0.0.1", ".onrender.com"]

# Render exposes the external hostname via this env var.
# Example: cenro-management-dzeg.onrender.com
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or config(
    "RENDER_EXTERNAL_HOSTNAME", default=None
)

ALLOWED_HOSTS = list(_allowed_hosts_env)

# Always allow any subdomain of onrender.com, regardless of env var contents.
if ".onrender.com" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(".onrender.com")

# Also allow the exact Render hostname if present.
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)

# Full origins WITH https://. Comma-separated in env.
# Django supports wildcard CSRF origins like https://*.onrender.com.
try:
    _csrf_origins_env = config(
        "CSRF_TRUSTED_ORIGINS",
        default="https://*.onrender.com",
    )
except Exception:
    _csrf_origins_env = "https://*.onrender.com"

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in _csrf_origins_env.split(",")
    if origin.strip()
]

# Always trust wildcard https://*.onrender.com even if env overrides.
if "https://*.onrender.com" not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append("https://*.onrender.com")

# Also trust the exact Render external hostname, if present.
if _render_host:
    _render_origin = f"https://{_render_host}"
    if _render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_render_origin)


# Recommended when behind Render/Cloudflare proxy (helps Django know request is HTTPS)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# -------------------------
# Apps
# -------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Project apps
    "accounts",
    "services",
    "scheduling",
    "dashboard",
]


# -------------------------
# Middleware
# -------------------------
MIDDLEWARE = [
    # Must be first to log unhandled exceptions
    "cenro_mgmt.middleware.ExceptionLoggingMiddleware",

    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",

    "cenro_mgmt.middleware.LoginRequiredMiddleware",
    "cenro_mgmt.middleware.ForceStaffPasswordChangeMiddleware",
]


ROOT_URLCONF = "cenro_mgmt.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "cenro_mgmt.wsgi.application"


# -------------------------
# Database
# -------------------------
def _get_database_config():
    """
    Use DATABASE_URL if set (Render Postgres); otherwise SQLite for local dev.
    """
    try:
        database_url = config("DATABASE_URL", default=None)
    except Exception:
        database_url = None

    if database_url:
        try:
            # conn_max_age improves performance on Render
            return dj_database_url.parse(database_url, conn_max_age=600)
        except Exception:
            pass

    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }

DATABASES = {"default": _get_database_config()}


# -------------------------
# Auth
# -------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "dashboard:home"


# -------------------------
# i18n
# -------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True


# -------------------------
# Static & Media
# -------------------------
STATIC_URL = "/static/"

STATICFILES_DIRS = []

_project_static = BASE_DIR / "static"
if _project_static.exists():
    STATICFILES_DIRS.append(_project_static)

_windows_root_static = Path("C:/static")
if _windows_root_static.exists():
    STATICFILES_DIRS.append(_windows_root_static)

STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Avoid manifest-related 500s (safer while debugging)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}


# -------------------------
# Security cookies (NOW actually uses your env vars)
# -------------------------
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=False, cast=bool)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=False, cast=bool)


# -------------------------
# Default auto field
# -------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# -------------------------
# Logging (Render-friendly)
# -------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
        "verbose_tb": {
            "format": "{levelname} {asctime} {module}\n{message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "verbose",
        },
        "console_tb": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "verbose_tb",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {
            "handlers": ["console_tb"],
            "level": "ERROR",
            "propagate": False,
        },
        "accounts": {
            "handlers": ["console_tb"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}