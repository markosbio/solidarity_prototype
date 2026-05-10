"""
Mobile money webhook normaliser and processor.

Handles MTN and Airtel payload formats, unifying them into a single internal
format before calling process_fee_contribution().

Webhook route: POST /api/mobile-money/callback
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime

from loguru import logger

from models import db, User, MobileMoneyTransaction
from fee_contribution import process_fee_contribution
from notifications import notify_solidarity_contribution


# ── Payload normaliser ────────────────────────────────────────────────────────

def normalise_mtn_payload(data: dict) -> dict | None:
    """Map MTN Mobile Money callback fields to internal format."""
    try:
        return {
            'phone': str(data['msisdn']).lstrip('+'),
            'type': 'send' if data.get('transactionType', '').upper() == 'DEBIT' else 'withdraw',
            'amount': float(data['amount']),
            'normal_fee': float(data['fee']),
            'receipt_id': str(data['financialTransactionId']),
            'network': 'mtn',
        }
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("MTN payload parse error: {} | raw={}", exc, data)
        return None


def normalise_airtel_payload(data: dict) -> dict | None:
    """Map Airtel Money callback fields to internal format."""
    try:
        txn = data.get('transaction', data)
        return {
            'phone': str(txn['msisdn']).lstrip('+'),
            'type': 'send' if str(txn.get('type', '')).upper() == 'PAYMENT' else 'withdraw',
            'amount': float(txn['amount']),
            'normal_fee': float(txn.get('charges', txn.get('fee', 0))),
            'receipt_id': str(txn['id']),
            'network': 'airtel',
        }
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Airtel payload parse error: {} | raw={}", exc, data)
        return None


def normalise_payload(data: dict) -> dict | None:
    """
    Auto-detect network and normalise payload.
    Returns internal dict or None if unrecognised.
    """
    if 'financialTransactionId' in data:
        return normalise_mtn_payload(data)
    if 'transaction' in data or ('msisdn' in data and 'charges' in data):
        return normalise_airtel_payload(data)
    # Generic fallback — used by simulated / test webhooks
    try:
        return {
            'phone': str(data['phone']).lstrip('+'),
            'type': data.get('type', 'send'),
            'amount': float(data['amount']),
            'normal_fee': float(data['normal_fee']),
            'receipt_id': str(data['receipt_id']),
            'network': data.get('network', 'unknown'),
        }
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Generic payload parse error: {} | raw={}", exc, data)
        return None


# ── Signature verification ────────────────────────────────────────────────────

def verify_webhook_signature(payload_bytes: bytes, signature_header: str,
                              network: str) -> bool:
    """
    Verify HMAC-SHA256 webhook signature.
    MTN  — header: X-MTN-Signature
    Airtel — header: X-Airtel-Signature
    Returns True when secret is not configured (dev mode).
    """
    secret_key = os.getenv(f'{network.upper()}_WEBHOOK_SECRET', '')
    if not secret_key:
        logger.debug("No webhook secret for {} — skipping signature check", network)
        return True
    expected = hmac.new(
        secret_key.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    provided = signature_header.lstrip('sha256=')
    return hmac.compare_digest(expected, provided)


# ── Main processor ────────────────────────────────────────────────────────────

def process_webhook(internal: dict) -> tuple[bool, str]:
    """
    Process a normalised mobile-money webhook payload.

    1. Idempotency check on receipt_id
    2. Look up user by phone
    3. Call process_fee_contribution()
    4. Save MobileMoneyTransaction record
    5. Send SMS notification

    Returns (success, message).
    """
    receipt_id = internal['receipt_id']

    # ── Idempotency ───────────────────────────────────────────────────────────
    existing = MobileMoneyTransaction.query.filter_by(receipt_id=receipt_id).first()
    if existing:
        logger.info("Webhook duplicate ignored: receipt_id={}", receipt_id)
        return True, 'already_processed'

    phone = internal['phone']
    user = User.query.filter_by(phone=phone).first()
    if not user:
        logger.warning("Webhook: no user found for phone={}", phone)
        return False, 'user_not_found'

    normal_fee = float(internal['normal_fee'])
    if normal_fee <= 0:
        logger.info("Webhook: fee=0 for receipt_id={} — skipping contribution", receipt_id)
        return True, 'zero_fee_skipped'

    solidarity_amount = process_fee_contribution(user.id, normal_fee)

    import os as _os
    wallet_pct = float(_os.getenv('ROUNDUP_WALLET_PCT', 70)) / 100
    pool_pct = float(_os.getenv('ROUNDUP_POOL_PCT', 20)) / 100
    to_wallet = round(solidarity_amount * wallet_pct, 4)
    to_pool = round(solidarity_amount * pool_pct, 4)
    to_platform = round(solidarity_amount - to_wallet - to_pool, 4)

    record = MobileMoneyTransaction(
        user_id=user.id,
        type=internal['type'],
        amount=float(internal['amount']),
        normal_fee=normal_fee,
        solidarity_amount=solidarity_amount,
        to_wallet=to_wallet,
        to_pool=to_pool,
        to_platform=to_platform,
        receipt_id=receipt_id,
        network=internal.get('network', 'unknown'),
        processed=True,
    )
    db.session.add(record)
    db.session.commit()

    # ── SMS notification (async-safe — AT SDK handles its own I/O) ────────────
    try:
        notify_solidarity_contribution(user, solidarity_amount, to_wallet)
    except Exception as exc:
        logger.error("Notification failed for user_id={}: {}", user.id, exc)

    logger.info(
        "Webhook processed: receipt_id={} user_id={} solidarity={:.0f}",
        receipt_id, user.id, solidarity_amount,
    )
    return True, 'processed'
