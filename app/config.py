"""Environment settings (secrets and deployment). Trading limits live in the DB."""
import os


def env(name: str, default: str = "") -> str:
    """Value of an env var; empty counts as unset (compose passes blanks as "")."""
    return os.environ.get(name, "").strip() or default


ETORO_API_KEY = env("ETORO_API_KEY")
ETORO_USER_KEY = env("ETORO_USER_KEY")
ETORO_ENV = env("ETORO_ENV", "demo").lower()          # "real" or "demo"
ETORO_BASE = env("ETORO_BASE", "https://public-api.etoro.com")

TA_DEEP_MODEL = env("TA_DEEP_MODEL", "claude-sonnet-5")
TA_QUICK_MODEL = env("TA_QUICK_MODEL", "claude-sonnet-5")

TWILIO_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_FROM = env("TWILIO_FROM")
ALERT_SMS_TO = env("ALERT_SMS_TO")

GMAIL_USER = env("GMAIL_USER")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = env("ALERT_EMAIL_TO") or GMAIL_USER

PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
ROOT_PATH = env("ROOT_PATH", "")                       # e.g. /trader behind Traefik
APP_SECRET = env("APP_SECRET", "change-me")
TZ = env("TZ", "Europe/London")
DATA_DIR = env("DATA_DIR", "/data")
DRY_RUN = env("DRY_RUN", "false").lower() in ("1", "true", "yes")
