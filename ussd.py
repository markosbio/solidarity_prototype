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
                "1. Check balance & draw ceiling\n"
                "2. Simulate round-up\n"
                "3. Request care funds\n"
                "4. My trust score\n"
                + mpesa_line +
                "6. Provider payment check\n"
                "7. Help / FAQ\n"
                "0. Exit"
            )
        else:
            return (
                "CON Welcome to SolidarityPool\n"
                "1. Register\n"
                "2. Provider payment check\n"
                "7. Help / FAQ\n"
                "0. Exit"
            )

    top = steps[0]

    # ── Exit ─────────────────────────────────────────────────────────────────
    if top == '0':
        return "END Thank you for using SolidarityPool."

    # ── Unregistered user flows ───────────────────────────────────────────────
    if not user:
        if top == '2':
            return _provider_check_flow(steps, level)
        if top == '7':
            return _help_faq(steps, level)
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
    if top == '6':
        return _provider_check_flow(steps, level)
    if top == '7':
        return _help_faq(steps, level)

    return "END Invalid option. Please dial again and choose 1–7 or 0 to exit."


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
    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError:
        ceiling = 0.0
    return (
        f"END Your SolidarityPool Balance\n"
        f"Sub-wallet: KES {user.sub_wallet_balance:.2f}\n"
        f"Draw ceiling: KES {ceiling:.2f}\n"
        f"Social credit: KES {user.total_social_credit:.2f}\n"
        f"Communal pool: KES {pool:.2f}"
    )


def _roundup_flow(user: User, steps: list, level: int) -> str:
    wallet_pct = int(os.getenv('ROUNDUP_WALLET_PCT', 70))
    pool_pct   = int(os.getenv('ROUNDUP_POOL_PCT',   20))
    fee_pct    = 100 - wallet_pct - pool_pct

    if level == 1:
        return (
            f"CON Enter purchase amount (KES):\n"
            f"Split: {wallet_pct}% wallet, {pool_pct}% pool, {fee_pct}% fee"
        )

    try:
        amount = float(steps[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        return "END Invalid amount. Please enter a number, e.g. 500"

    round_up = round(round(amount) - amount, 4)
    if round_up <= 0:
        round_up = 0.01

    w = wallet_pct / 100
    p = pool_pct   / 100
    to_wallet = round(round_up * w, 4)
    to_pool   = round(round_up * p, 4)
    to_fee    = round(round_up - to_wallet - to_pool, 4)

    user.sub_wallet_balance += to_wallet

    from models import Transaction, Community, CommunityMembership
    primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    if primary_comm and to_pool > 0:
        primary_comm.pool_balance += to_pool

    db.session.add(Transaction(user_id=user.id, amount=to_wallet, type='roundup',
                               description=f'USSD round-up wallet share from KES {amount:.2f}'))
    if to_pool > 0 and primary_comm:
        db.session.add(Transaction(user_id=user.id, amount=to_pool, type='pool_contribution',
                                   description=f'USSD round-up pool share from KES {amount:.2f}'))
    if to_fee > 0:
        db.session.add(Transaction(user_id=user.id, amount=to_fee, type='platform_fee',
                                   description=f'USSD round-up fee from KES {amount:.2f}'))
    db.session.commit()
    logger.info("USSD round-up: user_id={} total={:.4f} wallet={} pool={} fee={}",
                user.id, round_up, to_wallet, to_pool, to_fee)
    pool_info = f"\nPool credited: KES {to_pool:.2f}" if to_pool > 0 else ""
    return (
        f"END Round-up complete!\n"
        f"Your wallet: +KES {to_wallet:.2f}{pool_info}\n"
        f"New balance: KES {user.sub_wallet_balance:.2f}"
    )


def _request_care_flow(user: User, steps: list, level: int) -> str:
    # Always compute ceiling first so we can show it
    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError as exc:
        logger.error("USSD request_care TrustGraphError: {}", exc)
        return "END Could not compute your draw ceiling. Please try again later."

    if level == 1:
        return (
            f"CON Your draw ceiling: KES {ceiling:.0f}\n"
            f"This is the max you can request from the pool.\n"
            f"Enter amount needed (KES):"
        )

    try:
        needed = float(steps[1])
        if needed <= 0:
            raise ValueError
    except ValueError:
        return "END Invalid amount. Please enter a number, e.g. 500"

    if needed > ceiling:
        return (
            f"END Amount exceeds your ceiling.\n"
            f"Your draw ceiling is KES {ceiling:.0f}.\n"
            f"Please enter a lower amount or build your trust score."
        )

    if level == 2:
        return "CON Enter provider code (e.g. MULAGO001):"

    provider_code = steps[2].strip().upper()
    from models import Provider
    provider_obj = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
    if not provider_obj:
        all_providers = Provider.query.filter_by(verified=True).limit(3).all()
        examples = ', '.join(p.provider_code for p in all_providers) or 'MULAGO001'
        return (
            f"END Invalid provider code '{provider_code}'.\n"
            f"Try: {examples}\n"
            f"Or ask your clinic for their provider code."
        )
    provider_id = provider_code

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

    ceiling_remaining = max(0.0, ceiling - from_pool)
    logger.info(
        "USSD care request: user_id={} needed={} from_sub={} from_pool={} social_credit={}",
        user.id, needed, from_sub, from_pool, social_credit,
    )
    return (
        f"END Care request submitted!\n"
        f"From your wallet: KES {from_sub:.2f}\n"
        f"From pool: KES {from_pool:.2f}\n"
        f"Remaining ceiling: KES {ceiling_remaining:.0f}\n"
        f"Request ID: {req.id}"
    )


def _provider_check_flow(steps: list, level: int) -> str:
    """Let provider staff check last 5 payments by entering their provider code."""
    if level == 1:
        return "CON Enter your provider code:"

    provider_code = steps[1].strip().upper() if len(steps) > 1 else ''
    if not provider_code:
        return "END No code entered. Try again and enter your clinic code, e.g. MULAGO001"

    from models import Provider, PaymentRecord
    provider = Provider.query.filter_by(provider_code=provider_code).first()
    if not provider:
        all_p = Provider.query.filter_by(verified=True).limit(3).all()
        examples = ', '.join(p.provider_code for p in all_p) or 'MULAGO001'
        return (
            f"END Invalid code '{provider_code}'.\n"
            f"Try: {examples}\n"
            f"Ask clinic admin for the correct code."
        )

    payments = PaymentRecord.query.filter_by(provider_id=provider.id)\
                .order_by(PaymentRecord.created_at.desc()).limit(5).all()
    if not payments:
        return f"END {provider.name}: no payment records found yet."

    lines = [f"{provider.name} — last {len(payments)} payments:"]
    for p in payments:
        lines.append(f"KES {p.amount:.0f} [{p.status}] {p.created_at.strftime('%d/%m')}")
    return "END " + "\n".join(lines)


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


def _help_faq(steps: list, level: int) -> str:
    """Multi-level help / FAQ sub-menu."""
    if level == 1:
        return (
            "CON SolidarityPool Help\n"
            "1. What is SolidarityPool?\n"
            "2. How do round-ups work?\n"
            "3. How to request care funds?\n"
            "4. What is a trust score?\n"
            "5. What is a draw ceiling?\n"
            "0. Back to main menu"
        )
    topic = steps[1] if len(steps) > 1 else ''
    if topic == '1':
        return (
            "END SolidarityPool is a community mutual-aid fund.\n"
            "Members save via micro round-ups and can access\n"
            "care funds for medical emergencies."
        )
    if topic == '2':
        return (
            "END When you buy e.g. UGX 12,500, we round up\n"
            "to UGX 13,000 and save UGX 500.\n"
            "70% → your wallet  20% → community pool\n"
            "10% → platform fee."
        )
    if topic == '3':
        return (
            "END Dial *384# → option 3 (Request care funds).\n"
            "Enter amount, then your clinic's provider code\n"
            "(e.g. MULAGO001 — ask your clinic).\n"
            "3 community members will verify your request."
        )
    if topic == '4':
        return (
            "END Your trust score (0–1) measures reliability:\n"
            "repaying social credit, accurate witness votes,\n"
            "network connections, and regular contributions."
        )
    if topic == '5':
        return (
            "END Your draw ceiling is the max you can request\n"
            "from the pool. It grows as your trust score rises\n"
            "and the pool stays healthy.\n"
            "Check it: main menu → option 1 (Balance)."
        )
    if topic == '0':
        return "END Dial *384# again to return to the main menu."
    return "END Invalid choice. Dial *384# again for help."


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
