"""
Notification helpers — AT SMS and in-app alerts.
Sends SMS via Africa's Talking when AT_USERNAME + AT_API_KEY are configured.
Falls back to structured logging only when credentials are absent.
"""
import os
from loguru import logger


def notify_ceiling_increase(user, new_ceiling: float, old_ceiling: float) -> None:
    """SMS user when their draw ceiling increases by more than 5%."""
    if new_ceiling <= old_ceiling or new_ceiling <= old_ceiling * 1.05:
        return
    msg = (
        f"Good news {user.name.split()[0]}! "
        f"Your SolidarityPool care limit is now ${new_ceiling:.0f}. "
        f"Dial our shortcode or open the app to request care funds anytime."
    )
    logger.info(
        "Ceiling increase notification: user_id={} ${:.0f} -> ${:.0f}",
        user.id, old_ceiling, new_ceiling,
    )
    _send_sms(user.phone, msg)


def notify_pool_low(community, pct: float) -> None:
    """Warn community admin when pool health drops below 30%."""
    if pct >= 30:
        return
    logger.warning(
        "Pool low alert: community='{}' id={} balance={:.2f} health={:.0f}%",
        community.name, community.id, community.pool_balance, pct,
    )
    if not community.admin_user_id:
        return
    from models import User
    admin = User.query.get(community.admin_user_id)
    if not admin:
        return
    msg = (
        f"[SolidarityPool] Pool alert for '{community.name}': "
        f"{pct:.0f}% health (${community.pool_balance:.0f} left). "
        f"Encourage members to contribute round-ups to restore the pool."
    )
    _send_sms(admin.phone, msg)


def _send_sms(phone: str, message: str) -> None:
    at_username = os.getenv('AT_USERNAME')
    at_api_key = os.getenv('AT_API_KEY')
    if not (at_username and at_api_key):
        logger.info("AT not configured — SMS skipped for {}: {!r}", phone, message[:70])
        return
    try:
        import africastalking
        africastalking.initialize(at_username, at_api_key)
        sms = africastalking.SMS
        recipient = '+' + phone.lstrip('+')
        response = sms.send(message, [recipient])
        logger.info("SMS sent to {}: {}", phone, response)
    except ImportError:
        logger.warning("africastalking package not installed — SMS skipped")
    except Exception as exc:
        logger.error("SMS send failed to {}: {}", phone, exc)
