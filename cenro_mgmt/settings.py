import os
from pathlib import Path
import dj_database_url
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent

# Env vars with safe defaults so Render never crashes on missing vars
SECRET_KEY = config("SECRET_KEY", default="replace-me-in-production")

def _debug():
    try:
        return config("DEBUG", default="False", cast=bool)
    except Exception:
        return False
DEBUG = _debug()

# ALLOWED_HOSTS: hostnames only, no https:// (comma-separated on Render)
ALLOWED_HOSTS = config(
    "ALLOWED_HOSTS",
    default="cenro-management-7.onrender.com,localhost,127.0.0.1",
    cast=Csv(),
)

# CSRF_TRUSTED_ORIGINS: full origins with https:// (comma-separated on Render)
try:
    _csv_origins = config("CSRF_TRUSTED_ORIGINS", default="https://cenro-management-7.onrender.com")
    CSRF_TRUSTED_ORIGINS = [s.strip() for s in _csv_origins.split(",") if s.strip()]
except Exception:
    CSRF_TRUSTED_ORIGINS = ["https://cenro-management-7.onrender.com"]

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

MIDDLEWARE = [
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


# =========================
# DATABASE CONFIG
# =========================
def _get_database_config():
    try:
        database_url = config("DATABASE_URL", default=None)
    except Exception:
        database_url = None
    if database_url:
        try:
            return dj_database_url.parse(database_url, conn_max_age=600)
        except Exception:
            pass
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }


DATABASES = {
    "default": _get_database_config(),
}

AUTH_USER_MODEL = "accounts.User"


# =========================
# PASSWORD VALIDATION
# =========================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# =========================
# INTERNATIONALIZATION
# =========================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True


# =========================
# STATIC & MEDIA (FIXED)
# =========================
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Required for Django 5 + WhiteNoise (CompressedStaticFilesStorage avoids manifest 500s)
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}


# =========================
# AUTH REDIRECTS
# =========================
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "dashboard:home"


# =========================
# LOGGING (Render-friendly: full 500 tracebacks to stdout)
# =========================
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
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
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