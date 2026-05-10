from models import db, PaymentRecord, Provider, VerifiedProvider, User, Community, CareRequest
from datetime import datetime
from loguru import logger


def generate_payment_reference():
    today = datetime.utcnow().strftime("%Y-%m%d")
    prefix = f"SHP-{today}-"
    last = PaymentRecord.query.filter(PaymentRecord.reference_code.startswith(prefix)).count()
    return f"{prefix}{last+1:04d}"


def pay_provider(care_request_id, amount, provider_id, user_id, community_id):
    provider = Provider.query.get(provider_id)
    if not provider:
        logger.warning("pay_provider: provider_id={} not found", provider_id)
        return False, None

    # Check VerifiedProvider table for matching phone — provider must be verified
    vp = VerifiedProvider.query.filter_by(
        phone=provider.payment_details,
        verification_status='verified',
    ).first()
    if vp is None:
        # Fall back: if provider is marked verified in Provider table and no
        # VerifiedProvider record exists (legacy data), allow payment.
        if not provider.verified:
            logger.warning(
                "pay_provider: provider_id={} is not verified — payment blocked",
                provider_id,
            )
            return False, None

    reference = generate_payment_reference()
    payment = PaymentRecord(
        reference_code=reference,
        care_request_id=care_request_id,
        user_id=user_id,
        provider_id=provider_id,
        community_id=community_id,
        amount=amount,
        status='sent',
    )
    db.session.add(payment)
    db.session.commit()
    logger.info("Payment initiated: ref={} provider={} amount={}", reference, provider.name, amount)
    return True, reference
