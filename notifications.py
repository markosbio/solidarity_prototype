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
        f"Your SolidarityPool care limit is now UGX {new_ceiling:,.0f}. "
        f"Dial our shortcode or open the app to request care funds anytime."
    )
    logger.info(
        "Ceiling increase notification: user_id={} UGX {:.0f} -> UGX {:.0f}",
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
        f"{pct:.0f}% health (UGX {community.pool_balance:,.0f} left). "
        f"Encourage members to contribute round-ups to restore the pool."
    )
    _send_sms(admin.phone, msg)


def notify_solidarity_contribution(user, solidarity_amount: float, to_wallet: float) -> None:
    """
    Notify a member of their solidarity health contribution.
    Does NOT mention the 8% rate — just the amounts credited.
    """
    if solidarity_amount <= 0:
        return
    msg = (
        f"Solidarity Health contribution: UGX {solidarity_amount:,.0f} added. "
        f"UGX {to_wallet:,.0f} credited to your health wallet."
    )
    logger.info(
        "Solidarity contribution notification: user_id={} solidarity={:.0f} wallet={:.0f}",
        user.id, solidarity_amount, to_wallet,
    )
    _send_sms(user.phone, msg)


def notify_fraud_flagged(admin_phone: str, user_name: str, amount: float,
                         care_request_id: int, score: float) -> None:
    """Alert admin that a care request has been flagged for manual review."""
    msg = (
        f"[SolidarityPool] Fraud review needed: Care Request #{care_request_id} "
        f"by {user_name} for UGX {amount:,.0f} flagged (risk {score:.0%}). "
        f"Log in to review."
    )
    logger.warning(
        "Fraud flag notification: care_request_id={} user={} amount={:.0f} score={}",
        care_request_id, user_name, amount, score,
    )
    _send_sms(admin_phone, msg)


def notify_provider_approved(phone: str, provider_name: str, provider_code: str) -> None:
    """Notify a clinic that their provider application has been approved."""
    msg = (
        f"[SolidarityPool] Congratulations! Your provider application for '{provider_name}' "
        f"has been approved. Log in to your provider dashboard at /provider/login "
        f"using your provider code: {provider_code}. No password needed — only your code."
    )
    logger.info("Provider approved notification: phone={} code={}", phone, provider_code)
    _send_sms(phone, msg)


def notify_provider_rejected(phone: str, provider_name: str, reason: str = '') -> None:
    """Notify a clinic that their provider application was not approved."""
    reason_text = f" Reason: {reason}." if reason else ""
    msg = (
        f"[SolidarityPool] Your provider application for '{provider_name}' was not approved."
        f"{reason_text} Contact support for details or reapply with updated documents."
    )
    logger.info("Provider rejected notification: phone={} reason={!r}", phone, reason)
    _send_sms(phone, msg)


def notify_witnesses_assigned(requester_name: str, amount: float, witnesses: list) -> None:
    """
    SMS each selected witness asking them to verify a care request.
    `witnesses` is a list of User objects.
    """
    for witness in witnesses:
        msg = (
            f"[SolidarityPool] {requester_name} has listed you as a witness for a care fund "
            f"request of UGX {amount:,.0f}. Log in or dial *384# → option 5 (Witness tasks) "
            f"to approve or decline. Thank you for helping your community."
        )
        logger.info(
            "Witness assigned notification: witness_id={} phone={} requester={} amount={:.0f}",
            witness.id, witness.phone, requester_name, amount,
        )
        _send_sms(witness.phone, msg)


def notify_new_provider_application(admin_phone: str, provider_name: str,
                                    applicant_phone: str) -> None:
    """Alert admin when a new provider application is submitted via /apply-provider."""
    msg = (
        f"[SolidarityPool] New provider application received from '{provider_name}' "
        f"(phone: {applicant_phone}). Log in to the admin panel to review and approve: "
        f"/admin/verified-providers"
    )
    logger.info("New provider application notification: admin={} provider={} phone={}",
                admin_phone, provider_name, applicant_phone)
    _send_sms(admin_phone, msg)


def notify_payment_received(user, provider_name: str, amount: float) -> None:
    """SMS member when the health provider marks their payment as received."""
    msg = (
        f"[SolidarityPool] Good news {user.name.split()[0]}! "
        f"{provider_name} has confirmed receipt of your care fund payment "
        f"(UGX {amount:,.0f}). Your treatment can now begin. "
        f"Get well soon!"
    )
    logger.info("Payment received notification: user_id={} provider={} amount={:.0f}",
                user.id, provider_name, amount)
    _send_sms(user.phone, msg)


def notify_treatment_started(user, provider_name: str) -> None:
    """SMS member when the health provider marks treatment as started."""
    msg = (
        f"[SolidarityPool] {provider_name} has marked your treatment as started. "
        f"We wish you a speedy recovery, {user.name.split()[0]}! "
        f"Contact the clinic if you have any questions."
    )
    logger.info("Treatment started notification: user_id={} provider={}",
                user.id, provider_name)
    _send_sms(user.phone, msg)


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
