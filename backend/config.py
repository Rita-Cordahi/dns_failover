import os

# API Settings
API_TOKEN = os.getenv("API_TOKEN", "supersecretapitoken")

# Database Settings
# For testing/primary setups
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./primary.db")
FALLBACK_DATABASE_URL = os.getenv("FALLBACK_DATABASE_URL", "sqlite:///./fallback.db")
FALLBACK_DATABASE_URL_2 = os.getenv("FALLBACK_DATABASE_URL_2", "sqlite:///./fallback_2.db")
FALLBACK_DATABASE_URL_3 = os.getenv("FALLBACK_DATABASE_URL_3", "sqlite:///./fallback_3.db")
FALLBACK_DATABASE_URL_4 = os.getenv("FALLBACK_DATABASE_URL_4", "sqlite:///./fallback_4.db")
FALLBACK_DATABASE_URL_5 = os.getenv("FALLBACK_DATABASE_URL_5", "sqlite:///./fallback_5.db")
FALLBACK_DATABASE_URL_6 = os.getenv("FALLBACK_DATABASE_URL_6", "sqlite:///./fallback_6.db")


# Cloudflare Settings
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "mock_cf_token")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "mock_cf_account")
CLOUDFLARE_ZONE_ID = os.getenv("CLOUDFLARE_ZONE_ID", "mock_cf_zone")
CLOUDFLARE_EMAIL = os.getenv("CLOUDFLARE_EMAIL", "mock_cf_email")
CLOUDFLARE_DOMAIN = os.getenv("CLOUDFLARE_DOMAIN", "failover.example.com")

# Alerting configurations
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_API_URL = os.getenv("BREVO_API_URL", "https://api.brevo.com")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "admin@example.com")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "alerts@example.com")

# Rate Limiting Settings
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "True").lower() == "true"

_limit_env = os.getenv("RATE_LIMIT_LIMIT")
_max_req_env = os.getenv("RATE_LIMIT_MAX_REQUESTS")

try:
    _limit_val = int(_limit_env) if _limit_env is not None else 0
except ValueError:
    _limit_val = 0

try:
    _max_req_val = int(_max_req_env) if _max_req_env is not None else 0
except ValueError:
    _max_req_val = 0

if _limit_val > 0:
    RATE_LIMIT_MAX_REQUESTS = _limit_val
elif _max_req_val > 0:
    RATE_LIMIT_MAX_REQUESTS = _max_req_val
else:
    RATE_LIMIT_MAX_REQUESTS = 60

RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# DB Sync settings
try:
    DB_SYNC_INTERVAL = float(os.getenv("DB_SYNC_INTERVAL", "30.0"))
except ValueError:
    DB_SYNC_INTERVAL = 30.0

