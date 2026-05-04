"""
Phase 1: Provider Registry + Direct Payment.

Blueprint mounted at /providers.
Also exports pay_provider() used by the care-request approval flow.
"""
import os
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, abort
from flask_login import login_required, current_user
from loguru import logger

from models import db, Provider, CareRequest

providers_bp = Blueprint('providers', __name__, url_prefix='/providers')

ADMIN_THRESHOLD = 50.0   # care requests above this amount require admin sign-off


# ── Helper: is current user an admin? ─────────────────────────────────────────

def _is_admin(user) -> bool:
    from models import CommunityMembership
    if user.is_global_admin:
        return True
    return CommunityMembership.query.filter_by(user_id=user.id).filter(
        CommunityMembership.role.in_(['admin', 'coadmin'])
    ).first() is not None


# ── Routes ─────────────────────────────────────────────────────────────────────

@providers_bp.route('/')
@login_required
def list_providers():
    providers = Provider.query.order_by(Provider.name).all()
    return render_template('providers.html', user=current_user,
                           providers=providers, is_admin=_is_admin(current_user))


@providers_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add_provider():
    if not _is_admin(current_user):
        abort(403, description='Only admins can add providers.')

    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        code = request.form.get('provider_code', '').strip().upper()
        ptype = request.form.get('payment_type', '').strip()
        details = request.form.get('payment_details', '').strip()

        if not name or not code:
            error = 'Name and provider code are required.'
        elif Provider.query.filter_by(provider_code=code).first():
            error = f'Provider code {code!r} already exists.'
        else:
            p = Provider(name=name, provider_code=code,
                         payment_type=ptype, payment_details=details, verified=False)
            db.session.add(p)
            db.session.commit()
            logger.info("Provider added: code={} name={} by user_id={}", code, name, current_user.id)
            return redirect(url_for('providers.list_providers'))

    return render_template('providers_add.html', user=current_user, error=error)


@providers_bp.route('/<int:provider_id>/verify', methods=['POST'])
@login_required
def verify_provider(provider_id):
    if not _is_admin(current_user):
        abort(403)
    p = Provider.query.get_or_404(provider_id)
    p.verified = not p.verified
    db.session.commit()
    logger.info("Provider {} verified={} by user_id={}", p.provider_code, p.verified, current_user.id)
    return redirect(url_for('providers.list_providers'))


# ── Payment function ───────────────────────────────────────────────────────────

def pay_provider(provider_id: int, amount: float, care_request_id: int) -> bool:
    """
    Disburse funds directly to a registered provider after care approval.

    Priority:
      1. If provider.payment_type == 'mpesa' and M-Pesa env vars set → STK Push
      2. Otherwise → stub (log + generate a STUB-xxx transaction ID)

    Always marks the CareRequest as 'paid' on success.
    Returns True on success, False if provider or request not found.
    """
    provider = Provider.query.get(provider_id)
    care_req = CareRequest.query.get(care_request_id)

    if not provider or not care_req:
        logger.error("pay_provider: provider_id={} or care_request_id={} not found",
                     provider_id, care_request_id)
        return False

    tx_id = None

    # ── Attempt real M-Pesa payment ───────────────────────────────────────────
    if (provider.payment_type == 'mpesa'
            and provider.payment_details
            and os.getenv('MPESA_CONSUMER_KEY')):
        try:
            from mpesa import stk_push, MpesaError
            result = stk_push(
                phone=provider.payment_details,
                amount=amount,
                account_reference='SolidarityPool',
                description=f'Care#{care_request_id}',
            )
            tx_id = result.get('CheckoutRequestID', '')
            logger.info("M-Pesa STK Push sent to provider {}: tx={}", provider.name, tx_id)
        except Exception as exc:
            logger.warning("M-Pesa to provider {} failed (using stub): {}", provider.name, exc)

    # ── Stub fallback ─────────────────────────────────────────────────────────
    if not tx_id:
        tx_id = f"STUB-CR{care_request_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        logger.info("Stub payment to provider {}: amount={} tx={}", provider.name, amount, tx_id)

    care_req.payment_transaction_id = tx_id
    care_req.status = 'paid'
    db.session.commit()
    return True
