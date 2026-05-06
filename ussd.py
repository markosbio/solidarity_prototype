"""
USSD Integration via Africa's Talking.

Set these environment variables:
  AT_USERNAME   - your Africa's Talking username (use 'sandbox' for testing)
  AT_API_KEY    - your Africa's Talking API key

Africa's Talking sends POST requests to your callback URL with:
  sessionId, serviceCode, phoneNumber, text (cumulative user input)

Your app responds with plain text:
  CON <menu text>   → session continues, shows menu to user
  END <final text>  → session ends

Test using the AT simulator at:
  https://developers.africastalking.com/simulator
"""
import os
from flask import Blueprint, request
from loguru import logger
from models import db, User, SystemState, MpesaTopup, Transaction
from trust_graph import compute_draw_ceiling, TrustGraphError
from mpesa import stk_push, MpesaError

ussd_bp = Blueprint('ussd', __name__, url_prefix='/ussd')

# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Strip leading + or spaces; Africa's Talking sends e.g. +254712345678."""
    return phone.strip().lstrip('+')


def _get_or_none(phone: str):
    return User.query.filter_by(phone=_normalize_phone(phone)).first()


# ── main callback ─────────────────────────────────────────────────────────────

@ussd_bp.route('/callback', methods=['POST'])
def callback():
    session_id = request.form.get('sessionId', '')
    phone = request.form.get('phoneNumber', '')
    text = request.form.get('text', '')

    logger.info("USSD session={} phone={} text={!r}", session_id, phone, text)

    steps = text.split('*') if text else ['']
    level = len(steps)
    response = _route(phone, steps, level)

    logger.info("USSD response → {!r}", response[:80])
    return response, 200, {'Content-Type': 'text/plain'}


# ── menu router ───────────────────────────────────────────────────────────────

def _route(phone: str, steps: list, level: int) -> str:
    user = _get_or_none(phone)

    # ── Level 0: main menu ───────────────────────────────────────────────────
    if steps[0] == '':
        if user:
            mpesa_line = "5. Top up via M-Pesa\n" if (os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET')) else ""
            return (
                f"CON Welcome back, {user.name}\n"
                "1. Check balance\n"
                "2. Simulate round-up\n"
                "3. Request care funds\n"
                "4. My trust score\n"
                + mpesa_line +
                "0. Exit"
            )
        else:
            return (
                "CON Welcome to SolidarityPool\n"
                "1. Register\n"
                "0. Exit"
            )

    top = steps[0]

    # ── Exit ─────────────────────────────────────────────────────────────────
    if top == '0':
        return "END Thank you for using SolidarityPool."

    # ── Unregistered user: only registration allowed ─────────────────────────
    if not user:
        return _register_flow(phone, steps, level)

    # ── Registered user flows ─────────────────────────────────────────────────
    if top == '1':
        return _balance(user)
    if top == '2':
        return _roundup_flow(user, steps, level)
    if top == '3':
        return _request_care_flow(user, steps, level)
    if top == '4':
        return _trust_score(user)
    if top == '5':
        return _topup_flow(user, steps, level)

    return "END Invalid option. Please try again."


# ── sub-flows ─────────────────────────────────────────────────────────────────

def _register_flow(phone: str, steps: list, level: int) -> str:
    if steps[0] != '1':
        return "END Please register first.\nDial again and select 1."

    if level == 1:
        return "CON Enter your full name:"

    name = steps[1].strip()
    if not name:
        return "END Name cannot be blank. Please try again."

    if level == 2:
        return "CON Enter referrer phone (or 0 to skip):"

    referrer_input = steps[2].strip()
    referrer = None
    if referrer_input and referrer_input != '0':
        referrer = User.query.filter_by(phone=_normalize_phone(referrer_input)).first()

    normalized = _normalize_phone(phone)
    if User.query.filter_by(phone=normalized).first():
        return "END You are already registered. Dial again to log in."

    user = User(
        phone=normalized,
        name=name,
        sub_wallet_balance=0.0,
        trust_score=0.5,
        region_prefix=normalized[:3],
    )
    if referrer:
        user.referred_by = referrer.id

    db.session.add(user)
    db.session.commit()
    logger.info("USSD registration: phone={} name={}", normalized, name)
    return f"END Registration successful!\nWelcome, {name}.\nDial again to access your account."


def _balance(user: User) -> str:
    state = SystemState.query.first()
    pool = state.communal_pool_balance if state else 0.0
    return (
        f"END Your SolidarityPool Balance\n"
        f"Sub-wallet: KES {user.sub_wallet_balance:.2f}\n"
        f"Social credit: KES {user.total_social_credit:.2f}\n"
        f"Communal pool: KES {pool:.2f}"
    )


def _roundup_flow(user: User, steps: list, level: int) -> str:
    if level == 1:
        return "CON Enter purchase amount (KES):"

    try:
        amount = float(steps[1])
    except ValueError:
        return "END Invalid amount. Please enter a number."

    round_up = round(amount) - amount
    if round_up <= 0:
        round_up = 0.01

    user.sub_wallet_balance += round_up
    from models import Transaction
    tx = Transaction(
        user_id=user.id,
        amount=round_up,
        type='roundup',
        description=f'USSD round-up from KES {amount:.2f}',
    )
    db.session.add(tx)
    db.session.commit()
    logger.info("USSD round-up: user_id={} amount={:.4f}", user.id, round_up)
    return (
        f"END Round-up complete!\n"
        f"Added KES {round_up:.2f} to your sub-wallet.\n"
        f"New balance: KES {user.sub_wallet_balance:.2f}"
    )


def _request_care_flow(user: User, steps: list, level: int) -> str:
    if level == 1:
        return "CON Enter amount needed (KES):"

    try:
        needed = float(steps[1])
    except ValueError:
        return "END Invalid amount. Please enter a number."

    if level == 2:
        return "CON Enter provider ID (e.g. clinic name):"

    provider_id = steps[2].strip() or 'USSD-provider'

    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError as exc:
        logger.error("USSD request_care TrustGraphError: {}", exc)
        return "END Could not compute your draw ceiling. Please try again later."

    from_sub = min(user.sub_wallet_balance, needed)
    remaining = needed - from_sub
    user.sub_wallet_balance -= from_sub

    state = SystemState.query.first()
    from_pool = 0.0
    social_credit = 0.0
    if remaining > 0 and state:
        allowed = min(remaining, ceiling - from_sub, state.communal_pool_balance)
        from_pool = max(allowed, 0.0)
        state.communal_pool_balance -= from_pool
        social_credit = remaining - from_pool
        if social_credit > 0:
            user.total_social_credit += social_credit
            from recovery import update_recovery_parameters
            update_recovery_parameters(user.id, social_credit)

    from models import WitnessRequest
    from witness import select_witnesses, WitnessSelectionError
    try:
        witnesses = select_witnesses(user.id, provider_id)
    except WitnessSelectionError:
        witnesses = []

    req = WitnessRequest(
        user_id=user.id,
        needed_amount=needed,
        provider_id=provider_id,
        from_sub=from_sub,
        from_pool=from_pool,
        social_credit=social_credit,
        status='pending',
        witness_ids=','.join(str(w.id) for w in witnesses),
    )
    db.session.add(req)
    db.session.commit()

    logger.info(
        "USSD care request: user_id={} needed={} from_sub={} from_pool={} social_credit={}",
        user.id, needed, from_sub, from_pool, social_credit
    )
    return (
        f"END Care request submitted!\n"
        f"From your wallet: KES {from_sub:.2f}\n"
        f"From pool: KES {from_pool:.2f}\n"
        f"Social credit: KES {social_credit:.2f}\n"
        f"Request ID: {req.id}"
    )


def _trust_score(user: User) -> str:
    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError:
        ceiling = 0.0
    return (
        f"END Your Trust Profile\n"
        f"Trust score: {user.trust_score:.2f}\n"
        f"Witness accuracy: {user.witness_accuracy_score:.2f}\n"
        f"Draw ceiling: KES {ceiling:.2f}\n"
        f"Round-up multiplier: {user.roundup_intensifier:.2f}x"
    )


def _topup_flow(user: User, steps: list, level: int) -> str:
    """Trigger an M-Pesa STK Push top-up from the USSD menu."""
    if not (os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET')):
        return "END M-Pesa top-up is not available. Contact support."

    if level == 1:
        return "CON Enter top-up amount (KES):"

    try:
        topup_amount = float(steps[1])
        if topup_amount < 1:
            raise ValueError("Too small")
    except (ValueError, IndexError):
        return "END Invalid amount. Please enter a whole number."

    phone = user.phone
    try:
        result = stk_push(
            phone=phone,
            amount=topup_amount,
            account_reference='SolidarityPool',
            description=f'USSD top-up for {user.name}',
        )
    except MpesaError as exc:
        logger.error("USSD STK push failed for user_id={}: {}", user.id, exc)
        return "END M-Pesa prompt failed. Please try again later."

    checkout_id = result.get('CheckoutRequestID', '')
    merchant_id = result.get('MerchantRequestID', '')

    topup = MpesaTopup(
        user_id=user.id,
        amount=topup_amount,
        checkout_request_id=checkout_id,
        merchant_request_id=merchant_id,
        status='pending',
    )
    db.session.add(topup)
    db.session.commit()

    logger.info(
        "USSD STK push initiated: user_id={} phone={} amount={} checkout_id={}",
        user.id, phone, topup_amount, checkout_id,
    )
    return (
        f"END M-Pesa prompt sent to {phone}.\n"
        f"Amount: KES {int(topup_amount)}\n"
        f"Approve on your phone — your wallet\n"
        f"will be credited automatically."
    )
