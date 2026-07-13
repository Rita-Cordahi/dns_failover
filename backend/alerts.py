import json
import logging
import time
import re
import httpx
from backend import config
from backend.database import OutboxEvent

logger = logging.getLogger(__name__)

# Shared HTTPX client with HTTP/2 support (multiplexing)
http_client = httpx.AsyncClient(http2=True, timeout=10.0)


def sanitize_alert_error(error: Exception) -> str:
    err_str = str(error)
    if config.DISCORD_WEBHOOK_URL:
        err_str = err_str.replace(config.DISCORD_WEBHOOK_URL, "[REDACTED_DISCORD_WEBHOOK]")
        err_str = re.sub(r'discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+', 'discord.com/api/webhooks/[REDACTED]', err_str)
        parts = config.DISCORD_WEBHOOK_URL.split('/')
        if len(parts) > 1:
            token = parts[-1]
            if len(token) > 5:
                err_str = err_str.replace(token, "[REDACTED]")
    if config.BREVO_API_KEY:
        err_str = err_str.replace(config.BREVO_API_KEY, "[REDACTED_BREVO_KEY]")
    return err_str


async def send_discord_webhook(content: str) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        logger.info("Discord webhook URL not configured, skipping alert.")
        return False

    payload = {"content": content}
    try:
        resp = await http_client.post(config.DISCORD_WEBHOOK_URL, json=payload)
        return resp.status_code in (200, 204)
    except Exception as e:
        logger.error(f"Failed to send Discord webhook: {sanitize_alert_error(e)}")
        return False


async def send_brevo_email(subject: str, text_content: str) -> bool:
    if not config.BREVO_API_KEY:
        logger.info("Brevo API key not configured, skipping email alert.")
        return False

    url = f"{config.BREVO_API_URL.rstrip('/')}/v3/smtp/email"
    headers = {
        "api-key": config.BREVO_API_KEY,
        "content-type": "application/json",
        "accept": "application/json"
    }
    payload = {
        "sender": {"email": config.ALERT_EMAIL_FROM, "name": "DNS Failover Alert"},
        "to": [{"email": config.ALERT_EMAIL_TO}],
        "subject": subject,
        "textContent": text_content
    }

    try:
        resp = await http_client.post(url, json=payload, headers=headers)
        return resp.status_code in (200, 201, 204)
    except Exception as e:
        logger.error(f"Failed to send Brevo email: {sanitize_alert_error(e)}")
        return False


class AlertManager:
    _last_status = "healthy"
    _last_alert_time = 0.0
    _cooldown = 600.0  # 10 minutes

    @classmethod
    async def process_health_change(cls, new_status: str, detail_message: str, db=None):
        if new_status == cls._last_status:
            return

        cls._last_status = new_status
        now = time.time()
        cls._last_alert_time = now
        subject = f"🚨 DNS Failover Alert: System is {new_status.upper()}"
        msg = (
            f"Alert trigger at {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Status: {new_status.upper()}\n"
            f"Details: {detail_message}"
        )

        if db is not None:
            # Transactional Outbox: save events to database inside the same transaction
            discord_event = OutboxEvent(
                event_type="DISCORD",
                payload=json.dumps({"content": f"**{subject}**\n{detail_message}"})
            )
            email_event = OutboxEvent(
                event_type="EMAIL",
                payload=json.dumps({"subject": subject, "body": msg})
            )
            db.add(discord_event)
            db.add(email_event)
        else:
            # Fallback direct send
            await send_discord_webhook(f"**{subject}**\n{detail_message}")
            await send_brevo_email(subject, msg)
