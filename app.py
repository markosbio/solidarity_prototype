from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from functools import wraps
from models import (db, User, Transaction, Community, CommunityMembership, Provider,
                    CareRequest, SystemState, PaymentRecord, MpesaTopup,
                    MobileMoneyTransaction, VerifiedProvider, FraudAlert, PlatformRevenue,
                    TrustEvent, AdminAuditLog)
from trust_graph import compute_draw_ceiling
from witness import select_witnesses
from recovery import update_recovery_parameters
from payments import pay_provider
from mpesa import stk_push, parse_stk_callback, MpesaError
from trust_engine import get_combined_score
from communities import communities_bp
from providers_bp import providers_bp
from ussd import ussd_bp
from fee_contribution import process_fee_contribution, _get_solidarity_percent
from fraud import calculate_fraud_risk, log_fraud_alert, is_fraud_flagged
from pool_health import enforce_pool_health, is_large_withdrawal_blocked, required_witness_approvals
from mobile_money import normalise_payload, verify_webhook_signature, process_webhook
import random
import string
import os
import io
import csv
from datetime import datetime, timedelta
from notifications import notify_ceiling_increase, notify_pool_low, notify_fraud_flagged
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', os.environ.get('SECRET_KEY', 'solidarity-dev-key-change-in-production'))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///solidarity.db')

# ── Admin access control ───────────────────────────────────────────────────────
ADMIN_PHONES = ['0769547988']

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if not user or user.phone not in ADMIN_PHONES:
            return "Access denied — admin only.", 403
        admin_secret = os.environ.get('ADMIN_SECRET', '')
        token = request.args.get('token') or request.form.get('token')
        if admin_secret:
            if token == admin_secret:
                session['admin_authed'] = True
            elif not session.get('admin_authed'):
                return "Invalid admin token. Append ?token=YOUR_SECRET to the URL.", 403
        else:
            session['admin_authed'] = True
        return f(*args, **kwargs)
    return decorated

def _log_admin_action(admin_id, action, target_user_id=None, details=''):
    log = AdminAuditLog(
        admin_id=admin_id,
        target_user_id=target_user_id,
        action=action,
        details=details[:500],
        ip=request.remote_addr,
    )
    db.session.add(log)
    db.session.commit()

db.init_app(app)
app.register_blueprint(communities_bp)
app.register_blueprint(providers_bp)
app.register_blueprint(ussd_bp)

# Create tables and seed default data
with app.app_context():
    db.create_all()
    if Community.query.count() == 0:
        default_comm = Community(name="Global Health Pool", invite_code="GLOBAL001", pool_balance=1_000_000.0, admin_user_id=None)
        db.session.add(default_comm)
        db.session.commit()
    if Provider.query.count() == 0:
        mulago = Provider(name="Mulago Hospital", provider_code="MULAGO001", payment_type="mpesa", payment_details="254700000", verified=True)
        db.session.add(mulago)
        db.session.commit()

# ------------------ Helper ------------------
def get_user_communities(user_id):
    memberships = CommunityMembership.query.filter_by(user_id=user_id).all()
    return [Community.query.get(m.community_id) for m in memberships]


_POOL_TARGET = 2_000_000.0  # "full health" baseline in UGX

def _roundup_split(amount: float) -> tuple:
    """Split a round-up into (wallet, pool, fee) using env-configurable percentages."""
    w = float(os.getenv('ROUNDUP_WALLET_PCT', 70)) / 100
    p = float(os.getenv('ROUNDUP_POOL_PCT', 20)) / 100
    to_wallet = round(amount * w, 4)
    to_pool   = round(amount * p, 4)
    to_fee    = round(amount - to_wallet - to_pool, 4)
    return to_wallet, to_pool, to_fee


def _pool_health(pool_balance: float) -> dict:
    pct = min(100.0, max(0.0, pool_balance / _POOL_TARGET * 100))
    if pct >= 60:
        label, color = 'Healthy', 'green'
    elif pct >= 30:
        label, color = 'Fair', 'amber'
    else:
        label, color = 'Low', 'red'
    return {'pct': round(pct, 1), 'label': label, 'color': color}


def _check_emergency_auto_approvals():
    """Auto-approve emergency requests older than 2 hours when no admin has acted."""
    from loguru import logger
    threshold = datetime.utcnow() - timedelta(hours=2)
    pending = CareRequest.query.filter(
        CareRequest.status == 'pending_admin',
        CareRequest.is_emergency == True,
        CareRequest.admin_approved == False,
        CareRequest.created_at <= threshold,
    ).all()
    for care_req in pending:
        logger.info("Emergency auto-approve: care_req_id={} (>2h elapsed)", care_req.id)
        care_req.admin_approved = True
        care_req.status = 'admin_approved'
        success, ref = pay_provider(
            care_request_id=care_req.id, amount=care_req.amount_from_pool,
            provider_id=care_req.provider_id, user_id=care_req.user_id,
            community_id=care_req.community_id,
        )
        if success:
            care_req.payment_transaction_id = ref
    if pending:
        db.session.commit()
    return len(pending)


# ------------------ Web Routes ------------------
@app.route('/')
def home():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
        membership = None
        if primary_comm:
            membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=primary_comm.id).first()
        is_admin = membership and membership.role in ['admin', 'coadmin']
        try:
            ceiling = round(compute_draw_ceiling(user.id), 2)
        except Exception:
            ceiling = 0.0
        pool_balance = primary_comm.pool_balance if primary_comm else 0.0
        ph = _pool_health(pool_balance)
        ceiling_multiplier = primary_comm.ceiling_multiplier if primary_comm else 1.0
        health_contributions = (
            MobileMoneyTransaction.query
            .filter_by(user_id=user.id)
            .order_by(MobileMoneyTransaction.timestamp.desc())
            .limit(10).all()
        )
        return render_template('dashboard.html', user=user, primary_comm=primary_comm,
                               is_admin=is_admin, ceiling=ceiling, pool_health=ph,
                               ceiling_multiplier=ceiling_multiplier,
                               health_contributions=health_contributions)
    return redirect(url_for('register'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        user = User.query.filter_by(phone=phone).first()
        if user:
            session['user_id'] = user.id
            return redirect(url_for('home'))
        return render_template('login.html', error='Phone number not found. Please register first.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        phone = request.form['phone']
        name = request.form['name']
        referred_by = request.form.get('referred_by')
        pin = request.form.get('pin', '1234').strip()
        confirm_pin = request.form.get('confirm_pin', '').strip()
        existing = User.query.filter_by(phone=phone).first()
        if existing:
            session['user_id'] = existing.id
            return redirect(url_for('home'))
        if not pin.isdigit() or len(pin) != 4:
            return render_template('register.html', error='PIN must be exactly 4 digits.')
        if pin != confirm_pin:
            return render_template('register.html', error='PINs do not match. Please try again.')
        user = User(phone=phone, name=name, pin=pin, sub_wallet_balance=0.0, trust_score=0.5)
        if referred_by:
            referrer = User.query.filter_by(phone=referred_by).first()
            if referrer:
                user.referred_by = referrer.id
        db.session.add(user)
        db.session.commit()
        default_comm = Community.query.first()
        if default_comm:
            membership = CommunityMembership(user_id=user.id, community_id=default_comm.id, role='member')
            db.session.add(membership)
            user.primary_community_id = default_comm.id
            db.session.commit()
        session['user_id'] = user.id
        return redirect(url_for('home'))
    return render_template('register.html')

@app.route('/register_provider', methods=['POST'])
def register_provider():
    name = request.form['name']
    provider_code = request.form['provider_code'].upper().strip()
    payment_type = request.form['payment_type']
    payment_details = request.form['payment_details']
    contact_name = request.form.get('contact_name', '')
    contact_phone = request.form.get('contact_phone', '')
    existing = Provider.query.filter_by(provider_code=provider_code).first()
    if existing:
        return f"Provider code '{provider_code}' already taken."
    new_provider = Provider(
        name=name, provider_code=provider_code, payment_type=payment_type,
        payment_details=payment_details, verified=True,
        contact_name=contact_name, contact_phone=contact_phone
    )
    db.session.add(new_provider)
    db.session.commit()
    session['provider_registered_code'] = provider_code
    return redirect(url_for('provider_login'))

@app.route('/create_community', methods=['GET', 'POST'])
def create_community():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        name = request.form['name']
        desc = request.form.get('description', '')
        invite = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        new_comm = Community(name=name, description=desc, invite_code=invite, pool_balance=0.0, admin_user_id=user.id)
        db.session.add(new_comm)
        db.session.commit()
        membership = CommunityMembership(user_id=user.id, community_id=new_comm.id, role='admin')
        db.session.add(membership)
        user.primary_community_id = new_comm.id
        db.session.commit()
        return redirect(url_for('community_dashboard', comm_id=new_comm.id))
    return '''
        <form method="post">
            Community name: <input name="name" required><br>
            Description: <textarea name="description"></textarea><br>
            <button type="submit">Create</button>
        </form>
    '''

@app.route('/join_community', methods=['GET', 'POST'])
def join_community():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        invite_code = request.form['invite_code'].strip().upper()
        comm = Community.query.filter_by(invite_code=invite_code).first()
        if not comm:
            return "Invalid invite code"
        existing = CommunityMembership.query.filter_by(user_id=user.id, community_id=comm.id).first()
        if existing:
            return "You are already a member of this community"
        membership = CommunityMembership(user_id=user.id, community_id=comm.id, role='member')
        db.session.add(membership)
        if not user.primary_community_id:
            user.primary_community_id = comm.id
            db.session.commit()
        return redirect(url_for('home'))
    return '''
        <form method="post">
            Invite code: <input name="invite_code" required><br>
            <button type="submit">Join</button>
        </form>
    '''

@app.route('/community/<int:comm_id>')
def community_dashboard(comm_id):
    if 'user_id' not in session:
        return redirect(url_for('register'))
    community = Community.query.get(comm_id)
    if not community:
        return "Community not found"
    user = User.query.get(session['user_id'])
    membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=comm_id).first()
    if not membership:
        return "You are not a member of this community"
    members = CommunityMembership.query.filter_by(community_id=comm_id).all()
    for m in members:
        m.user = User.query.get(m.user_id)
    return render_template('community_dashboard.html', community=community, members=members, user_role=membership.role)

@app.route('/simulate_roundup', methods=['GET', 'POST'])
def simulate_roundup():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    mpesa_enabled = bool(os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET'))
    wallet_pct = int(os.getenv('ROUNDUP_WALLET_PCT', 70))
    pool_pct   = int(os.getenv('ROUNDUP_POOL_PCT',   20))
    fee_pct    = 100 - wallet_pct - pool_pct
    solidarity_pct = _get_solidarity_percent()
    if request.method == 'POST':
        normal_fee = float(request.form.get('normal_fee', 0) or 0)
        if normal_fee <= 0:
            return redirect(url_for('simulate_roundup'))
        try:
            old_ceiling = compute_draw_ceiling(user.id)
        except Exception:
            old_ceiling = 0.0
        solidarity_amount = process_fee_contribution(user.id, normal_fee)
        primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
        if primary_comm:
            to_pool = round(solidarity_amount * pool_pct / 100, 4)
            primary_comm.pool_balance += to_pool
            health = enforce_pool_health(primary_comm)
            ph = _pool_health(primary_comm.pool_balance)
            notify_pool_low(primary_comm, ph['pct'])
        db.session.commit()
        try:
            new_ceiling = compute_draw_ceiling(user.id)
            notify_ceiling_increase(user, new_ceiling, old_ceiling)
        except Exception:
            pass
        return redirect(url_for('home'))
    return render_template('simulate_roundup.html', user=user, mpesa_enabled=mpesa_enabled,
                           wallet_pct=wallet_pct, pool_pct=pool_pct, fee_pct=fee_pct,
                           solidarity_pct=solidarity_pct)


@app.route('/mpesa/topup', methods=['POST'])
def mpesa_topup():
    """Initiate an M-Pesa STK Push to top up the user's sub-wallet."""
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    try:
        topup_amount = float(request.form['topup_amount'])
        if topup_amount < 1:
            raise ValueError("Minimum top-up is 1 KES")
    except (ValueError, KeyError) as exc:
        return render_template('mpesa_waiting.html', error=str(exc), user=user)

    try:
        result = stk_push(
            phone=user.phone,
            amount=topup_amount,
            account_reference='SolidarityPool',
            description=f'Sub-wallet top-up for {user.name}',
        )
    except MpesaError as exc:
        return render_template('mpesa_waiting.html', error=str(exc), user=user)

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

    return render_template(
        'mpesa_waiting.html',
        user=user,
        checkout_id=checkout_id,
        amount=topup_amount,
        error=None,
    )


@app.route('/mpesa/topup/status/<checkout_id>')
def mpesa_topup_status(checkout_id):
    """JSON polling endpoint — the waiting page calls this every few seconds."""
    topup = MpesaTopup.query.filter_by(checkout_request_id=checkout_id).first()
    if not topup:
        return jsonify({'status': 'unknown'})
    return jsonify({
        'status': topup.status,
        'amount': topup.amount,
        'receipt': topup.mpesa_receipt,
        'result_desc': topup.result_desc,
    })


@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Safaricom posts the STK Push result here."""
    try:
        data = parse_stk_callback(request.get_json(force=True))
    except MpesaError as exc:
        from loguru import logger
        logger.error("Bad M-Pesa callback: {}", exc)
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Parse error'}), 400

    topup = MpesaTopup.query.filter_by(
        checkout_request_id=data['checkout_request_id']
    ).first()
    if not topup:
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Ignored'}), 200

    if data['result_code'] == 0:
        topup.status = 'confirmed'
        topup.mpesa_receipt = data['mpesa_receipt']
        topup.result_desc = data['result_desc']
        topup.confirmed_at = datetime.utcnow()
        user = User.query.get(topup.user_id)
        user.sub_wallet_balance += topup.amount
        tx = Transaction(
            user_id=user.id,
            amount=topup.amount,
            type='mpesa_topup',
            description=f'M-Pesa top-up {data["mpesa_receipt"]}',
        )
        db.session.add(tx)
    else:
        topup.status = 'failed'
        topup.result_desc = data['result_desc']

    db.session.commit()
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

@app.route('/request_care', methods=['GET', 'POST'])
def request_care():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    if not session.get('pin_verified'):
        return redirect(url_for('verify_pin', next=url_for('request_care')))
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        needed_amount = float(request.form['needed_amount'])
        provider_id = int(request.form['provider_id'])
        is_emergency = 'is_emergency' in request.form
        community_id = int(request.form['community_id'])
        community = Community.query.get(community_id)
        if not community:
            return "Invalid community"
        ceiling = compute_draw_ceiling(user.id)
        from_sub = min(user.sub_wallet_balance, needed_amount)
        remaining = needed_amount - from_sub
        user.sub_wallet_balance -= from_sub
        from_pool = 0.0
        social_credit = 0.0
        if remaining > 0:
            allowed = min(remaining, ceiling - from_sub, community.pool_balance)
            from_pool = allowed
            community.pool_balance -= from_pool
            social_credit = remaining - from_pool
            if social_credit > 0:
                user.total_social_credit += social_credit
                update_recovery_parameters(user.id, social_credit)
            db.session.commit()
        # Pool health guard — block large withdrawals when pool is stressed
        if is_large_withdrawal_blocked(community, needed_amount):
            return render_template('request_care.html', user=user,
                                   providers=Provider.query.filter_by(verified=True).all(),
                                   communities=get_user_communities(session['user_id']),
                                   ceiling=ceiling,
                                   error='Large withdrawals are temporarily paused to protect the pool. Please try a smaller amount or wait for the pool to recover.')

        care_req = CareRequest(
            user_id=user.id, community_id=community.id, provider_id=provider_id,
            amount_needed=needed_amount, amount_from_sub=from_sub, amount_from_pool=from_pool,
            social_credit=social_credit, is_emergency=is_emergency, status='pending_witness'
        )
        db.session.add(care_req)
        db.session.commit()

        # Fraud scoring
        try:
            fraud_score, fraud_reasons = calculate_fraud_risk(user.id, care_req.id)
            if is_fraud_flagged(fraud_score):
                care_req.fraud_flagged = True
                care_req.fraud_score = fraud_score
                care_req.fraud_reasons = '; '.join(fraud_reasons)
                care_req.status = 'pending_admin'
                db.session.commit()
                alert = log_fraud_alert(user.id, care_req.id, fraud_score, fraud_reasons)
                # Notify admin if found
                admin_comm = Community.query.get(community_id)
                if admin_comm and admin_comm.admin_user_id:
                    admin_user = User.query.get(admin_comm.admin_user_id)
                    if admin_user:
                        notify_fraud_flagged(admin_user.phone, user.name,
                                             needed_amount, care_req.id, fraud_score)
        except Exception:
            pass

        if care_req.status == 'pending_witness':
            witnesses = select_witnesses(user.id, provider_id, community_id=community.id)
            witness_ids = ','.join(str(w.id) for w in witnesses)
            care_req.witness_ids = witness_ids
            db.session.commit()
        try:
            ceiling_remaining = max(0.0, round(ceiling - from_pool, 2))
        except Exception:
            ceiling_remaining = 0.0
        return render_template('request_result.html', needed=needed_amount, from_sub=from_sub,
                               from_pool=from_pool, social_credit=social_credit,
                               request_id=care_req.id, ceiling_remaining=ceiling_remaining,
                               is_emergency=is_emergency)
    providers = Provider.query.filter_by(verified=True).all()
    communities = get_user_communities(session['user_id'])
    try:
        ceiling = round(compute_draw_ceiling(user.id), 2)
    except Exception:
        ceiling = 0.0
    return render_template('request_care.html', user=user, providers=providers,
                           communities=communities, ceiling=ceiling)

@app.route('/witness_dashboard')
def witness_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    pending_care = []
    requests = CareRequest.query.filter_by(status='pending_witness').all()
    for req in requests:
        if req.witness_ids and str(user.id) in req.witness_ids.split(','):
            req.requester = User.query.get(req.user_id)
            pending_care.append(req)
    from models import WitnessRequest
    pending_legacy = []
    legacy_reqs = WitnessRequest.query.filter_by(status='pending').all()
    for req in legacy_reqs:
        if req.witness_ids and str(user.id) in req.witness_ids.split(','):
            pending_legacy.append(req)
    comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=comm.id).first() if comm else None
    is_admin = membership and membership.role in ['admin', 'coadmin']
    return render_template('witness_dashboard.html', user=user, pending_care=pending_care, pending_legacy=pending_legacy, is_admin=is_admin)

@app.route('/verify_care/<int:request_id>/<response>')
def verify_care(request_id, response):
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get(request_id)
    if not care_req or care_req.status != 'pending_witness':
        return "Invalid request", 400
    if str(user.id) not in care_req.witness_ids.split(','):
        return "Not authorized", 403
    votes = care_req.witness_votes.split(',') if care_req.witness_votes else []
    if f"{user.id}:{response}" not in votes:
        votes.append(f"{user.id}:{response}")
        care_req.witness_votes = ','.join(votes)
        db.session.commit()
    yes_count = sum(1 for v in votes if v.endswith('accept'))
    total_witnesses = len(care_req.witness_ids.split(','))
    if yes_count >= 2:
        need_admin = (care_req.amount_needed > 180000) or care_req.is_emergency
        if need_admin:
            care_req.status = 'pending_admin'
        else:
            care_req.status = 'admin_approved'
            care_req.admin_approved = True
            success, ref = pay_provider(
                care_request_id=care_req.id, amount=care_req.amount_from_pool,
                provider_id=care_req.provider_id, user_id=care_req.user_id,
                community_id=care_req.community_id
            )
            if success:
                care_req.payment_transaction_id = ref
        db.session.commit()
    elif len(votes) >= total_witnesses:
        care_req.status = 'rejected'
        db.session.commit()
    return redirect(url_for('witness_dashboard'))

@app.route('/trust_history')
def trust_history():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    from models import TrustEvent
    user = User.query.get(session['user_id'])
    events = TrustEvent.query.filter_by(user_id=user.id).order_by(TrustEvent.timestamp.desc()).limit(50).all()
    return render_template('trust_history.html', user=user, events=events)

@app.route('/admin/care')
@admin_required
def admin_care():
    user = User.query.get(session['user_id'])
    _check_emergency_auto_approvals()
    pending = CareRequest.query.filter_by(status='pending_admin', admin_approved=False).all()
    for cr in pending:
        cr.requester = User.query.get(cr.user_id)
    solidarity_pct = _get_solidarity_percent()
    fraud_count = FraudAlert.query.filter_by(resolved=False).count()
    return render_template('admin_care.html', user=user, pending=pending,
                           solidarity_pct=solidarity_pct, fraud_count=fraud_count)


@app.route('/admin/set-solidarity-percent', methods=['POST'])
@admin_required
def admin_set_solidarity_percent():
    try:
        pct = float(request.form['percent'])
        pct = max(1.0, min(25.0, pct))
    except (ValueError, KeyError):
        return redirect(url_for('admin_care'))
    state = SystemState.query.first()
    if state:
        state.solidarity_percent = pct
    else:
        db.session.add(SystemState(communal_pool_balance=0.0, solidarity_percent=pct))
    db.session.commit()
    return redirect(url_for('admin_care'))


@app.route('/api/mobile-money/callback', methods=['POST'])
def mobile_money_callback():
    """Unified mobile money webhook — MTN and Airtel both post here."""
    from loguru import logger
    raw = request.get_data()
    network = request.args.get('network', 'unknown').lower()
    sig = request.headers.get('X-MTN-Signature', '') or request.headers.get('X-Airtel-Signature', '')
    if not verify_webhook_signature(raw, sig, network):
        logger.warning("Webhook signature mismatch for network={}", network)
        return jsonify({'error': 'invalid_signature'}), 401
    data = request.get_json(force=True, silent=True) or {}
    internal = normalise_payload(data)
    if not internal:
        return jsonify({'error': 'unrecognised_payload'}), 400
    ok, msg = process_webhook(internal)
    if ok:
        return jsonify({'status': msg}), 200
    return jsonify({'error': msg}), 422


@app.route('/admin/verified-providers')
@admin_required
def admin_verified_providers():
    applications = VerifiedProvider.query.order_by(VerifiedProvider.created_at.desc()).all()
    for app_ in applications:
        if app_.reviewed_by:
            app_.resolver = User.query.get(app_.reviewed_by)
        else:
            app_.resolver = None
    return render_template('admin_verified_providers.html', applications=applications)


@app.route('/admin/verified-providers/apply', methods=['POST'])
@admin_required
def apply_verified_provider():
    vp = VerifiedProvider(
        provider_name=request.form.get('provider_name', '').strip(),
        phone=request.form.get('phone', '').strip().lstrip('+'),
        provider_wallet_number=request.form.get('provider_wallet_number', '').strip(),
        business_license=request.form.get('business_license', '').strip(),
        location=request.form.get('location', '').strip(),
        verification_status='pending',
    )
    db.session.add(vp)
    db.session.commit()
    return redirect(url_for('admin_verified_providers'))


@app.route('/admin/verified-providers/<int:app_id>/review', methods=['POST'])
@admin_required
def admin_verify_provider_application(app_id):
    vp = VerifiedProvider.query.get_or_404(app_id)
    action = request.form.get('action')
    notes = request.form.get('notes', '').strip()
    if action in ('verify', 'reject'):
        vp.verification_status = 'verified' if action == 'verify' else 'rejected'
        vp.review_notes = notes
        vp.reviewed_at = datetime.utcnow()
        vp.reviewed_by = session['user_id']
        db.session.commit()
    return redirect(url_for('admin_verified_providers'))


@app.route('/admin/fraud-alerts')
@admin_required
def admin_fraud_alerts():
    open_alerts = FraudAlert.query.filter_by(resolved=False).order_by(FraudAlert.created_at.desc()).all()
    resolved_alerts = FraudAlert.query.filter_by(resolved=True).order_by(FraudAlert.created_at.desc()).limit(20).all()
    for alert in open_alerts + resolved_alerts:
        alert.user = User.query.get(alert.user_id)
        alert.resolver = User.query.get(alert.resolved_by) if alert.resolved_by else None
    return render_template('admin_fraud_alerts.html', open_alerts=open_alerts, resolved_alerts=resolved_alerts)


@app.route('/admin/fraud-alerts/<int:alert_id>/resolve', methods=['POST'])
@admin_required
def admin_resolve_fraud_alert(alert_id):
    alert = FraudAlert.query.get_or_404(alert_id)
    alert.resolved = True
    alert.resolved_by = session['user_id']
    alert.resolved_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('admin_fraud_alerts'))

@app.route('/admin/care/<int:request_id>', methods=['POST'])
@admin_required
def admin_care_action(request_id):
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get(request_id)
    if not care_req:
        return "Request not found", 404
    action = request.form.get('action')
    if action == 'approve':
        care_req.admin_approved = True
        care_req.admin_id = user.id
        care_req.status = 'admin_approved'
        success, ref = pay_provider(
            care_request_id=care_req.id, amount=care_req.amount_from_pool,
            provider_id=care_req.provider_id, user_id=care_req.user_id,
            community_id=care_req.community_id
        )
        if success:
            care_req.payment_transaction_id = ref
        db.session.commit()
    elif action == 'deny':
        care_req.status = 'rejected'
        db.session.commit()
    return redirect(url_for('admin_care'))

@app.route('/verify_witness/<int:request_id>/<response>')
def verify_witness(request_id, response):
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get(request_id)
    if not care_req or care_req.status != 'pending_witness':
        return "Invalid request", 400
    if str(user.id) not in care_req.witness_ids.split(','):
        return "Not authorized", 403
    votes = care_req.witness_votes.split(',') if care_req.witness_votes else []
    if f"{user.id}:{response}" not in votes:
        votes.append(f"{user.id}:{response}")
        care_req.witness_votes = ','.join(votes)
        db.session.commit()
    yes_count = sum(1 for v in votes if v.endswith('accept'))
    total_witnesses = len(care_req.witness_ids.split(','))
    if yes_count >= 2:
        need_admin = (care_req.amount_needed > 180000) or care_req.is_emergency
        if need_admin:
            care_req.status = 'pending_admin'
        else:
            care_req.status = 'admin_approved'
            care_req.admin_approved = True
            success, ref = pay_provider(
                care_request_id=care_req.id, amount=care_req.amount_from_pool,
                provider_id=care_req.provider_id, user_id=care_req.user_id,
                community_id=care_req.community_id
            )
            if success:
                care_req.payment_transaction_id = ref
            db.session.commit()
    elif len(votes) >= total_witnesses:
        care_req.status = 'rejected'
        db.session.commit()
    return redirect(url_for('witness_dashboard'))

@app.route('/admin/approve/<int:request_id>/<action>')
@admin_required
def admin_approve(request_id, action):
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get(request_id)
    if not care_req:
        return "Request not found"
    community = Community.query.get(care_req.community_id)
    if care_req.status != 'pending_admin':
        return "Request not pending admin approval"
    if action == 'approve':
        care_req.admin_approved = True
        care_req.admin_id = user.id
        care_req.status = 'admin_approved'
        success, ref = pay_provider(
            care_request_id=care_req.id, amount=care_req.amount_from_pool,
            provider_id=care_req.provider_id, user_id=care_req.user_id,
            community_id=care_req.community_id
        )
        if success:
            care_req.payment_transaction_id = ref
        db.session.commit()
        msg = f"Request #{request_id} approved."
    elif action == 'reject':
        care_req.status = 'rejected'
        db.session.commit()
        msg = f"Request #{request_id} rejected."
    else:
        return "Invalid action"
    return f"<p>{msg}</p><p><a href='/'>Home</a> | <a href='/community/{community.id}'>Back to Community</a></p>"

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('pin_verified', None)
    session.pop('admin_authed', None)
    return redirect(url_for('login'))

# ── PIN verification ───────────────────────────────────────────────────────────

@app.route('/verify_pin', methods=['GET', 'POST'])
def verify_pin():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    next_url = request.args.get('next') or request.form.get('next') or url_for('home')
    if request.method == 'POST':
        user = User.query.get(session['user_id'])
        entered = request.form.get('pin', '').strip()
        if entered == (user.pin or '1234'):
            session['pin_verified'] = True
            return redirect(next_url)
        return render_template('verify_pin.html', next=next_url, error='Incorrect PIN. Please try again.')
    return render_template('verify_pin.html', next=next_url, error=None)

# ── Change PIN ────────────────────────────────────────────────────────────────

@app.route('/change_pin', methods=['GET', 'POST'])
def change_pin():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        current_pin = request.form.get('current_pin', '').strip()
        new_pin = request.form.get('new_pin', '').strip()
        confirm_pin = request.form.get('confirm_pin', '').strip()
        if current_pin != (user.pin or '1234'):
            return render_template('change_pin.html', error='Current PIN is incorrect.', success=None)
        if not new_pin.isdigit() or len(new_pin) != 4:
            return render_template('change_pin.html', error='New PIN must be exactly 4 digits.', success=None)
        if new_pin != confirm_pin:
            return render_template('change_pin.html', error='New PINs do not match.', success=None)
        user.pin = new_pin
        session.pop('pin_verified', None)
        db.session.commit()
        return render_template('change_pin.html', error=None, success='PIN updated successfully. Please re-verify when accessing sensitive features.')
    return render_template('change_pin.html', error=None, success=None)

# ── Repayment page ─────────────────────────────────────────────────────────────

@app.route('/repay', methods=['GET', 'POST'])
def repay():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if not session.get('pin_verified'):
        return redirect(url_for('verify_pin', next=url_for('repay')))
    user = User.query.get(session['user_id'])
    if user.total_social_credit <= 0:
        return render_template('repay.html', user=user, error=None, message='You have no outstanding social credit.')
    if request.method == 'POST':
        try:
            repay_amount = float(request.form['repay_amount'])
            if repay_amount <= 0:
                raise ValueError
        except (ValueError, KeyError):
            return render_template('repay.html', user=user, error='Enter a valid amount.')
        if repay_amount > user.sub_wallet_balance:
            return render_template('repay.html', user=user,
                                   error=f'Insufficient wallet balance (UGX {user.sub_wallet_balance:,.0f}).')
        actual = min(repay_amount, user.total_social_credit)
        user.sub_wallet_balance -= actual
        old_credit = user.total_social_credit
        user.total_social_credit = max(0.0, user.total_social_credit - actual)
        improvement = min(0.05, actual / 100_000 * 0.1)
        old_score = user.trust_score
        user.trust_score = min(1.0, user.trust_score + improvement)
        event = TrustEvent(user_id=user.id, old_score=old_score, new_score=user.trust_score,
                           delta=round(improvement, 6), reason='debt_repayment')
        db.session.add(event)
        tx = Transaction(user_id=user.id, amount=-actual, type='debt_repayment',
                         description=f'Social credit repayment of UGX {actual:,.0f}')
        db.session.add(tx)
        db.session.commit()
        return render_template('repay.html', user=user, error=None,
                               message=f'Repaid UGX {actual:,.0f}. Remaining social credit: UGX {user.total_social_credit:,.0f}. Trust score updated to {user.trust_score:.4f}.')
    return render_template('repay.html', user=user, error=None, message=None)

@app.route('/provider/logout')
def provider_logout():
    session.pop('provider_id', None)
    return redirect(url_for('provider_login'))

# ------------------ Provider Dashboard & Invoice ------------------
@app.route('/provider/login', methods=['GET', 'POST'])
def provider_login():
    registered_code = session.pop('provider_registered_code', None)
    if request.method == 'POST':
        code = request.form['provider_code']
        provider = Provider.query.filter_by(provider_code=code.upper(), verified=True).first()
        if provider:
            session['provider_id'] = provider.id
            return redirect(url_for('provider_dashboard'))
        else:
            return render_template('provider_login.html', error='Invalid provider code.', registered_code=None)
    return render_template('provider_login.html', error=None, registered_code=registered_code)

@app.route('/provider/dashboard')
def provider_dashboard():
    if 'provider_id' not in session:
        return redirect(url_for('provider_login'))
    provider = Provider.query.get(session['provider_id'])
    payments = PaymentRecord.query.filter_by(provider_id=provider.id).order_by(PaymentRecord.created_at.desc()).all()
    return render_template('provider_dashboard.html', provider=provider, payments=payments)

@app.route('/provider/confirm/<ref>')
def confirm_payment(ref):
    payment = PaymentRecord.query.filter_by(reference_code=ref).first()
    if payment and payment.status == 'sent':
        payment.status = 'received'
        payment.provider_confirmed_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('provider_dashboard'))

@app.route('/provider/start/<ref>')
def start_treatment(ref):
    payment = PaymentRecord.query.filter_by(reference_code=ref).first()
    if payment and payment.status == 'received':
        payment.status = 'treatment_started'
        payment.treatment_started_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('provider_dashboard'))

@app.route('/provider/invoice', methods=['POST'])
def provider_invoice():
    provider_code = request.form['provider_code']
    provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
    if not provider:
        return "Provider not found"
    patient_phone = request.form['patient_phone']
    user = User.query.filter_by(phone=patient_phone).first()
    if not user:
        return "Patient not registered in the system"
    amount = float(request.form['amount'])
    description = request.form['description']
    community = Community.query.get(user.primary_community_id)
    if not community:
        memberships = CommunityMembership.query.filter_by(user_id=user.id).first()
        if memberships:
            community = Community.query.get(memberships.community_id)
        else:
            return "Patient not in any community"
    care_req = CareRequest(
        user_id=user.id, community_id=community.id, provider_id=provider.id,
        amount_needed=amount, amount_from_sub=0, amount_from_pool=0,
        social_credit=0, is_emergency=False, status='pending_witness', witness_ids=''
    )
    db.session.add(care_req)
    db.session.commit()
    return f"Invoice submitted. Request ID: {care_req.id}. Patient will be notified."

# ------------------ USSD (Africa's Talking) ------------------
ussd_sessions = {}

def _ussd_main_menu(user, role, primary_comm, r_fn):
    menu = (f"Hi {user.name}\n"
            "1. Balance\n"
            "2. Request care\n"
            "3. Trust score\n"
            "4. Community\n"
            "5. Witness tasks\n"
            "7. Help/FAQ\n"
            "9. Contribution history\n")
    if user.total_social_credit > 0:
        menu += "8. Repay debt\n"
    if role in ['admin', 'coadmin'] and primary_comm:
        menu += "6. Admin panel\n"
    menu += "0. Exit"
    return r_fn(menu)


@app.route('/ussd', methods=['GET', 'POST'])
def ussd():
    phone = request.values.get("phoneNumber", "")
    text  = request.values.get("text", "")

    def r(msg, end=False):
        return f"{'END' if end else 'CON'} {msg}"

    # ── Parse inputs with universal back handler ──────────────────────────────
    raw_inputs = text.split('*') if text else []

    # Universal back: pressing 0 at any submenu level goes up one level.
    # We strip trailing "0"s until we reach either step 0 (main menu) or a
    # non-zero last input, then re-dispatch normally.
    inputs = raw_inputs[:]
    while len(inputs) > 1 and inputs[-1] == "0":
        inputs = inputs[:-1]

    step   = len(inputs)
    choice = inputs[0] if inputs else ""

    # ── Unregistered user flow ────────────────────────────────────────────────
    user = User.query.filter_by(phone=phone).first()
    if not user:
        if step == 0:
            return r("Welcome to Solidarity Health Pool.\nNot registered.\n1. Register\n2. Exit")
        if step == 1 and choice == "1":
            return r("Enter your full name:")
        if step == 2 and raw_inputs[0] == "1":
            return r("Choose a 4-digit PIN:")
        if step == 3 and raw_inputs[0] == "1":
            name = inputs[1]
            pin = inputs[2].strip()
            if not pin.isdigit() or len(pin) != 4:
                return r("PIN must be exactly 4 digits. Dial again.", end=True)
            new_user = User(phone=phone, name=name, pin=pin, sub_wallet_balance=0.0, trust_score=0.5)
            db.session.add(new_user)
            db.session.commit()
            default_comm = Community.query.first()
            if default_comm:
                mem = CommunityMembership(user_id=new_user.id, community_id=default_comm.id, role='member')
                db.session.add(mem)
                new_user.primary_community_id = default_comm.id
                db.session.commit()
            return r(f"Registered as {name}. Dial again to access your account.", end=True)
        return r("Invalid.", end=True)

    # ── Registered user setup ─────────────────────────────────────────────────
    primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    role = 'member'
    if primary_comm:
        mem = CommunityMembership.query.filter_by(user_id=user.id, community_id=user.primary_community_id).first()
        role = mem.role if mem else 'member'

    # Main menu (step 0, or back-navigated to root)
    if step == 0:
        if not primary_comm:
            return r("You are not in a community yet.\n4. Community (create/join)\n7. Help/FAQ\n0. Exit")
        return _ussd_main_menu(user, role, primary_comm, r)

    # ── 0. Exit from main menu ────────────────────────────────────────────────
    if choice == "0" and step == 1:
        return r("Goodbye. Stay well!", end=True)

    # ── 1. Balance ────────────────────────────────────────────────────────────
    if choice == "1":
        try:
            ceil_val = compute_draw_ceiling(user.id)
        except Exception:
            ceil_val = 0.0
        if primary_comm:
            ph = _pool_health(primary_comm.pool_balance)
            bal = (f"Wallet: UGX {user.sub_wallet_balance:,.0f}\n"
                   f"Draw ceiling: UGX {ceil_val:,.0f}\n"
                   f"Pool: UGX {primary_comm.pool_balance:,.0f} ({ph['label']})")
        else:
            bal = f"Wallet: UGX {user.sub_wallet_balance:,.0f}\nDraw ceiling: UGX {ceil_val:,.0f}"
        return r(bal, end=True)

    # ── 3. Trust score ────────────────────────────────────────────────────────
    if choice == "3":
        score = get_combined_score(user.id)
        return r(f"Trust score: {score:.4f}\n\nHigher = more pool access.\nImprove by contributing & witnessing.", end=True)

    # ── 4. Community ──────────────────────────────────────────────────────────
    if choice == "4":
        if step == 1:
            return r("Community\n1. Create community\n2. Join community\n0. Back")
        sub = inputs[1] if step > 1 else ''
        if sub == "1":
            if step == 2:
                ussd_sessions[phone] = {"state": "create_name"}
                return r("Enter a name for your community:\n0. Back")
            if step >= 3:
                comm_name = inputs[2]
                invite = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                new_comm = Community(name=comm_name, invite_code=invite,
                                     pool_balance=0.0, admin_user_id=user.id)
                db.session.add(new_comm)
                db.session.commit()
                new_mem = CommunityMembership(user_id=user.id, community_id=new_comm.id, role='admin')
                db.session.add(new_mem)
                user.primary_community_id = new_comm.id
                db.session.commit()
                ussd_sessions.pop(phone, None)
                return r(f"Community '{comm_name}' created.\nInvite code: {invite}", end=True)
        elif sub == "2":
            if step == 2:
                ussd_sessions[phone] = {"state": "join_invite"}
                return r("Enter invite code:\n0. Back")
            if step >= 3:
                invite_code = inputs[2].strip().upper()
                comm = Community.query.filter_by(invite_code=invite_code).first()
                if not comm:
                    return r("Invalid invite code. Try again.", end=True)
                existing = CommunityMembership.query.filter_by(user_id=user.id, community_id=comm.id).first()
                if existing:
                    return r("You are already a member of that community.", end=True)
                new_mem = CommunityMembership(user_id=user.id, community_id=comm.id, role='member')
                db.session.add(new_mem)
                if not user.primary_community_id:
                    user.primary_community_id = comm.id
                db.session.commit()
                return r(f"Joined {comm.name}.", end=True)
        return r("Invalid choice.\n0. Back")

    # ── 2. Request care ───────────────────────────────────────────────────────
    if choice == "2":
        if not primary_comm:
            return r("Join a community first (option 4).", end=True)
        user_communities = get_user_communities(user.id)
        if not user_communities:
            return r("Join a community first (option 4).", end=True)

        # PIN gate: step 1 asks for PIN; step 2 verifies it; rest of flow shifts by 1
        if step == 1:
            return r("Enter your PIN to continue:")
        if step == 2:
            entered_pin = inputs[1].strip()
            if entered_pin != (user.pin or '1234'):
                return r("Incorrect PIN. Dial again.", end=True)
            # PIN verified — show care request entry
            try:
                _ceil = compute_draw_ceiling(user.id)
            except Exception:
                _ceil = 0.0
            if len(user_communities) == 1:
                ussd_sessions[phone] = {
                    "selected_comm_id": user_communities[0].id,
                    "state": "awaiting_amount", "ceiling": _ceil,
                }
                return r(f"Request care\nYour ceiling: UGX {_ceil:,.0f}\nEnter amount (UGX):\n0. Back")
            else:
                comm_list = "\n".join([f"{i+1}. {c.name}" for i, c in enumerate(user_communities)])
                ussd_sessions[phone] = {
                    "state": "choose_comm",
                    "communities": [(c.id, c.name) for c in user_communities],
                    "ceiling": _ceil,
                }
                return r(f"Your ceiling: UGX {_ceil:,.0f}\nSelect community:\n{comm_list}\n0. Back")

        # step 3+ → original flow but reading inputs shifted by 1 (inputs[n] instead of inputs[n-1])
        # Re-index: effective step within the care flow = step - 1
        eff_step = step - 1

        # step 1 → select community (or skip if only one) / enter amount
        if eff_step == 1:
            try:
                _ceil = compute_draw_ceiling(user.id)
            except Exception:
                _ceil = 0.0
            if len(user_communities) == 1:
                ussd_sessions[phone] = {
                    "selected_comm_id": user_communities[0].id,
                    "state": "awaiting_amount", "ceiling": _ceil,
                }
                return r(f"Request care\nYour ceiling: UGX {_ceil:,.0f}\nEnter amount (UGX):\n0. Back")
            else:
                comm_list = "\n".join([f"{i+1}. {c.name}" for i, c in enumerate(user_communities)])
                ussd_sessions[phone] = {
                    "state": "choose_comm",
                    "communities": [(c.id, c.name) for c in user_communities],
                    "ceiling": _ceil,
                }
                return r(f"Your ceiling: UGX {_ceil:,.0f}\nSelect community:\n{comm_list}\n0. Back")

        sess = ussd_sessions.get(phone, {})
        state = sess.get("state", "")

        # eff_step 2 → community selection (multi-community path)
        if eff_step == 2 and state == "choose_comm":
            try:
                idx = int(inputs[2]) - 1
            except ValueError:
                return r("Invalid choice.\n0. Back")
            comms = sess["communities"]
            if 0 <= idx < len(comms):
                sess["selected_comm_id"] = comms[idx][0]
                sess["state"] = "awaiting_amount"
                ussd_sessions[phone] = sess
                return r("Enter amount (UGX):\n0. Back")
            return r("Invalid choice.\n0. Back")

        # Amount input (eff_step 2 for single-comm, eff_step 3 for multi-comm)
        amt_step = 2 if sess.get("state") == "awaiting_amount" or "selected_comm_id" in sess else 3
        if eff_step == amt_step and "selected_comm_id" in sess:
            try:
                amount = float(inputs[eff_step])
            except ValueError:
                return r("Invalid amount. Enter a number (UGX):\n0. Back")
            sess["amount"] = amount
            sess["state"] = "awaiting_provider"
            ussd_sessions[phone] = sess
            return r("Enter provider code (e.g., MULAGO001):\n0. Back")

        # Provider code input
        prov_step = amt_step + 1
        if eff_step == prov_step and "amount" in sess:
            provider_code = inputs[eff_step].strip().upper()
            provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
            if not provider:
                sample = Provider.query.filter_by(verified=True).first()
                hint = sample.provider_code if sample else 'MULAGO001'
                return r(f"Invalid code '{provider_code}'.\nTry {hint} or ask your clinic.\n0. Back")
            sess["provider_id"] = provider.id
            ussd_sessions[phone] = sess
            return r("Emergency?\n1. Yes\n2. No\n0. Back")

        # Confirm + submit
        conf_step = prov_step + 1
        if eff_step == conf_step and "provider_id" in sess:
            emerg = (inputs[eff_step] == "1")
            amount = sess["amount"]
            provider_id = sess["provider_id"]
            selected_comm = Community.query.get(sess["selected_comm_id"])
            ceiling = sess.get("ceiling") or compute_draw_ceiling(user.id)

            # Pool health guard
            if is_large_withdrawal_blocked(selected_comm, amount):
                ussd_sessions.pop(phone, None)
                return r("Large withdrawals paused — pool is low.\nTry a smaller amount.", end=True)

            from_sub = min(user.sub_wallet_balance, amount)
            remaining = amount - from_sub
            user.sub_wallet_balance -= from_sub
            from_pool = social_credit = 0.0
            if remaining > 0:
                allowed = min(remaining, ceiling - from_sub, selected_comm.pool_balance)
                from_pool = allowed
                selected_comm.pool_balance -= from_pool
                social_credit = remaining - from_pool
                if social_credit > 0:
                    user.total_social_credit += social_credit
                    update_recovery_parameters(user.id, social_credit)
                db.session.commit()
            care_req = CareRequest(
                user_id=user.id, community_id=selected_comm.id, provider_id=provider_id,
                amount_needed=amount, amount_from_sub=from_sub, amount_from_pool=from_pool,
                social_credit=social_credit, is_emergency=emerg, status='pending_witness',
            )
            db.session.add(care_req)
            db.session.commit()
            witnesses = select_witnesses(user.id, provider_id, community_id=selected_comm.id)
            care_req.witness_ids = ','.join(str(w.id) for w in witnesses)
            db.session.commit()
            enforce_pool_health(selected_comm)
            ussd_sessions.pop(phone, None)
            ceiling_remaining = max(0.0, ceiling - from_pool)
            need_admin = (amount > 180000) or emerg
            msg = (f"Request submitted.\n"
                   f"UGX {amount:,.0f} requested.\n"
                   f"Ceiling remaining: UGX {ceiling_remaining:,.0f}\n"
                   f"{len(witnesses)} witnesses notified.")
            if emerg:
                msg += " Auto-approved in 2h if no admin action."
            elif need_admin:
                msg += " Awaiting admin approval."
            return r(msg, end=True)

        return r("Session expired. Dial again.", end=True)

    # ── 5. Witness tasks ──────────────────────────────────────────────────────
    if choice == "5":
        if step == 1:
            pending_witness = []
            all_reqs = CareRequest.query.filter_by(status='pending_witness').all()
            for req in all_reqs:
                if req.witness_ids and str(user.id) in req.witness_ids.split(','):
                    votes_cast = [v.split(':')[0] for v in (req.witness_votes or '').split(',') if v]
                    if str(user.id) not in votes_cast:
                        pending_witness.append(req)
            if not pending_witness:
                return r("No pending witness tasks.", end=True)
            req = pending_witness[0]
            req_user = User.query.get(req.user_id)
            prov = Provider.query.get(req.provider_id)
            ussd_sessions[phone] = {"witness_req_id": req.id}
            return r(
                f"Witness task\nRequest #{req.id}\n"
                f"By: {req_user.name if req_user else '?'}\n"
                f"Provider: {prov.name if prov else '?'}\n"
                f"Amount: UGX {req.amount_needed:,.0f}\n"
                "1. Accept\n2. Reject\n0. Back"
            )
        if step == 2:
            req_id = ussd_sessions.get(phone, {}).get("witness_req_id")
            if not req_id:
                return r("Session error. Dial again.", end=True)
            care_req = CareRequest.query.get(req_id)
            if not care_req or care_req.status != 'pending_witness':
                return r("Request already processed.", end=True)
            vote_input = inputs[1]
            response = "accept" if vote_input == "1" else "reject"
            votes = [v for v in (care_req.witness_votes or '').split(',') if v]
            if f"{user.id}:{response}" not in votes:
                votes.append(f"{user.id}:{response}")
                care_req.witness_votes = ','.join(votes)
                db.session.commit()
            yes_count = sum(1 for v in votes if v.endswith('accept'))
            total = len(care_req.witness_ids.split(','))
            if yes_count >= 2:
                need_admin = (care_req.amount_needed > 180000) or care_req.is_emergency
                if need_admin:
                    care_req.status = 'pending_admin'
                else:
                    care_req.status = 'admin_approved'
                    care_req.admin_approved = True
                    ok, ref = pay_provider(
                        care_request_id=care_req.id, amount=care_req.amount_from_pool,
                        provider_id=care_req.provider_id, user_id=care_req.user_id,
                        community_id=care_req.community_id,
                    )
                    if ok:
                        care_req.payment_transaction_id = ref
                db.session.commit()
            elif len(votes) >= total:
                care_req.status = 'rejected'
                db.session.commit()
            ussd_sessions.pop(phone, None)
            return r("Vote recorded. Thank you.", end=True)

    # ── 6. Admin panel ────────────────────────────────────────────────────────
    if choice == "6":
        if role not in ['admin', 'coadmin'] or not primary_comm:
            return r("Not authorised.", end=True)
        if step == 1:
            return r("Admin panel\n1. Approve requests\n2. Invite code\n3. Members\n0. Back")
        sub = inputs[1] if step > 1 else ''
        if sub == "1":
            if step == 2:
                pending_reqs = CareRequest.query.filter_by(
                    community_id=primary_comm.id, status='pending_admin', admin_approved=False
                ).all()
                if not pending_reqs:
                    return r("No pending approvals.\n0. Back")
                ussd_sessions[phone] = {'admin_pending': [rq.id for rq in pending_reqs], 'admin_idx': 0}
                req = pending_reqs[0]
                req_user = User.query.get(req.user_id)
                prov = Provider.query.get(req.provider_id)
                return r(
                    f"Request by {req_user.name if req_user else '?'}:\n"
                    f"UGX {req.amount_needed:,.0f} at {prov.name if prov else '?'}\n"
                    "1. Approve\n2. Reject\n0. Next"
                )
            if step == 3:
                data = ussd_sessions.get(phone, {})
                pending_ids = data.get('admin_pending', [])
                idx = data.get('admin_idx', 0)
                if idx >= len(pending_ids):
                    return r("No more requests.", end=True)
                req_id = pending_ids[idx]
                care_req = CareRequest.query.get(req_id)
                action = inputs[2]
                if action == "1":
                    care_req.admin_approved = True
                    care_req.admin_id = user.id
                    care_req.status = 'admin_approved'
                    ok, ref = pay_provider(
                        care_request_id=care_req.id, amount=care_req.amount_from_pool,
                        provider_id=care_req.provider_id, user_id=care_req.user_id,
                        community_id=care_req.community_id,
                    )
                    if ok:
                        care_req.payment_transaction_id = ref
                    db.session.commit()
                    msg = f"Request #{req_id} approved and payment initiated."
                elif action == "2":
                    care_req.status = 'rejected'
                    db.session.commit()
                    msg = f"Request #{req_id} rejected."
                else:
                    data['admin_idx'] = idx + 1
                    ussd_sessions[phone] = data
                    next_idx = idx + 1
                    if next_idx < len(pending_ids):
                        next_req = CareRequest.query.get(pending_ids[next_idx])
                        ru = User.query.get(next_req.user_id)
                        pv = Provider.query.get(next_req.provider_id)
                        return r(
                            f"Request by {ru.name if ru else '?'}:\n"
                            f"UGX {next_req.amount_needed:,.0f} at {pv.name if pv else '?'}\n"
                            "1. Approve\n2. Reject\n0. Next"
                        )
                    return r("All requests processed.", end=True)
                return r(msg, end=True)
        elif sub == "2":
            return r(f"Invite code: {primary_comm.invite_code}\n\nShare with new members.", end=True)
        elif sub == "3":
            members = CommunityMembership.query.filter_by(community_id=primary_comm.id).all()
            names = [User.query.get(m.user_id).name for m in members[:5] if User.query.get(m.user_id)]
            msg = f"Members ({len(members)} total):\n" + "\n".join(names)
            if len(members) > 5:
                msg += f"\n+{len(members) - 5} more"
            return r(msg, end=True)
        return r("Invalid.\n0. Back")

    # ── 7. Help / FAQ ─────────────────────────────────────────────────────────
    if choice == "7":
        if step == 1:
            return r(
                "Help & FAQ\n"
                "1. What is SolidarityPool?\n"
                "2. How do contributions work?\n"
                "3. How do I request care funds?\n"
                "4. What is a trust score?\n"
                "5. What is a draw ceiling?\n"
                "0. Back"
            )
        topic = inputs[1] if step > 1 else ''
        answers = {
            '1': ("SolidarityPool is a community mutual-aid fund. "
                  "Members contribute via mobile money fees and access care funds for medical needs."),
            '2': ("When you make a mobile money transaction, a small solidarity contribution "
                  "is calculated from your operator fee. 70% goes to your health wallet, "
                  "20% to the community pool, 10% is a platform fee."),
            '3': ("Choose option 2 from the main menu. Enter the amount and your clinic's "
                  "provider code (ask your clinic). Three members will verify your request."),
            '4': ("Your trust score (0-1) measures your reliability: repaying social credit, "
                  "accurate witness votes, network connections, and regular contributions."),
            '5': ("Your draw ceiling is the maximum you can request from the pool. "
                  "It grows with your trust score and the pool's health. Check it in Balance (option 1)."),
        }
        if topic in answers:
            return r(answers[topic], end=True)
        return r("Invalid topic. Dial again.", end=True)

    # ── 8. Repay debt ─────────────────────────────────────────────────────────
    if choice == "8":
        if user.total_social_credit <= 0:
            return r("You have no outstanding debt to repay.", end=True)
        # step 1 → ask for PIN
        if step == 1:
            return r(f"Repay debt\nOwed: UGX {user.total_social_credit:,.0f}\nEnter your PIN:")
        # step 2 → verify PIN
        if step == 2:
            entered_pin = inputs[1].strip()
            if entered_pin != (user.pin or '1234'):
                return r("Incorrect PIN. Dial again.", end=True)
            return r(f"Enter amount to repay (UGX)\nMax wallet: UGX {user.sub_wallet_balance:,.0f}\n0. Back")
        # step 3 → process repayment
        if step == 3:
            try:
                repay_amt = float(inputs[2])
                if repay_amt <= 0:
                    raise ValueError
            except (ValueError, IndexError):
                return r("Invalid amount. Dial again.", end=True)
            if repay_amt > user.sub_wallet_balance:
                return r(f"Insufficient wallet balance (UGX {user.sub_wallet_balance:,.0f}).", end=True)
            actual = min(repay_amt, user.total_social_credit)
            user.sub_wallet_balance -= actual
            user.total_social_credit = max(0.0, user.total_social_credit - actual)
            improvement = min(0.05, actual / 100_000 * 0.1)
            old_score = user.trust_score
            user.trust_score = min(1.0, user.trust_score + improvement)
            event = TrustEvent(user_id=user.id, old_score=old_score, new_score=user.trust_score,
                               delta=round(improvement, 6), reason='debt_repayment')
            db.session.add(event)
            tx = Transaction(user_id=user.id, amount=-actual, type='debt_repayment',
                             description=f'USSD debt repayment of UGX {actual:,.0f}')
            db.session.add(tx)
            db.session.commit()
            return r(
                f"Repaid UGX {actual:,.0f}.\n"
                f"Remaining debt: UGX {user.total_social_credit:,.0f}\n"
                f"Trust score: {user.trust_score:.4f}",
                end=True,
            )

    # ── 9. Contribution history ───────────────────────────────────────────────
    if choice == "9":
        txns = (MobileMoneyTransaction.query
                .filter_by(user_id=user.id)
                .order_by(MobileMoneyTransaction.timestamp.desc())
                .limit(5).all())
        if not txns:
            # Also check Transaction table for solidarity_wallet entries
            wallet_txns = (Transaction.query
                           .filter_by(user_id=user.id, type='solidarity_wallet')
                           .order_by(Transaction.timestamp.desc())
                           .limit(5).all())
            if not wallet_txns:
                return r(
                    "Contribution history\nNo contributions yet.\n"
                    "Contributions are logged when mobile money fees are processed.",
                    end=True
                )
            lines = [f"  UGX {t.amount:,.0f} ({t.timestamp.strftime('%d/%m')})" for t in wallet_txns]
            total_contrib = sum(t.amount for t in wallet_txns)
        else:
            lines = []
            for t in txns:
                lines.append(
                    f"  {t.timestamp.strftime('%d/%m')} "
                    f"{t.type}: fee UGX {t.normal_fee:,.0f} "
                    f"→ contrib UGX {t.solidarity_amount:,.0f}"
                )
            total_contrib = sum(t.solidarity_amount for t in txns)
        msg = f"Last contributions:\n" + "\n".join(lines) + f"\n\nShown: UGX {total_contrib:,.0f}"
        return r(msg, end=True)

    return r("Invalid choice.", end=True)


# ------------------ Admin: Platform Monitor ------------------

@app.route('/admin/monitor')
@admin_required
def admin_monitor():
    from sqlalchemy import func
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    # Total solidarity contributions (solidarity_wallet transactions)
    total_solidarity = db.session.query(
        func.coalesce(func.sum(Transaction.amount), 0.0)
    ).filter(Transaction.type.in_(['solidarity_wallet', 'solidarity_pool', 'solidarity_fee'])).scalar() or 0.0

    # Platform revenue
    total_platform_revenue = db.session.query(
        func.coalesce(func.sum(PlatformRevenue.amount), 0.0)
    ).scalar() or 0.0
    revenue_count = PlatformRevenue.query.count()
    recent_revenue = PlatformRevenue.query.order_by(PlatformRevenue.timestamp.desc()).limit(20).all()

    # Active users (any transaction in last 30 days)
    active_users = db.session.query(func.count(func.distinct(Transaction.user_id))).filter(
        Transaction.timestamp >= thirty_days_ago
    ).scalar() or 0

    total_users = User.query.count()
    fraud_count = FraudAlert.query.filter_by(resolved=False).count()
    pending_verifications = VerifiedProvider.query.filter_by(verification_status='pending').count()

    # Communities with member counts
    communities_raw = Community.query.order_by(Community.pool_balance.desc()).all()
    communities = []
    for comm in communities_raw:
        comm.member_count = CommunityMembership.query.filter_by(community_id=comm.id).count()
        communities.append(comm)

    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    return render_template('admin_monitor.html',
                           total_solidarity=total_solidarity,
                           total_platform_revenue=total_platform_revenue,
                           revenue_count=revenue_count,
                           recent_revenue=recent_revenue,
                           active_users=active_users,
                           total_users=total_users,
                           fraud_count=fraud_count,
                           pending_verifications=pending_verifications,
                           communities=communities,
                           now=now_str)


# ------------------ Admin: Trust Override ------------------

@app.route('/admin/trust')
@admin_required
def admin_trust_page():
    user = User.query.get(session['user_id'])
    return render_template('admin_trust.html', user=user)


@app.route('/admin/trust/override', methods=['POST'])
@admin_required
def admin_trust_override_by_phone():
    admin = User.query.get(session['user_id'])
    phone = request.form.get('phone', '').strip()
    target = User.query.filter_by(phone=phone).first()
    if not target:
        return f"No user found with phone {phone}. Please check and try again.", 404
    try:
        new_score = float(request.form.get('trust_score', ''))
        new_score = max(0.0, min(1.0, new_score))
    except ValueError:
        return "Invalid trust score. Enter a number between 0 and 1.", 400
    reason = request.form.get('reason', 'admin_override').strip() or 'admin_override'
    event = TrustEvent(
        user_id=target.id,
        old_score=target.trust_score,
        new_score=new_score,
        delta=round(new_score - target.trust_score, 6),
        reason=reason,
        factors='admin_override',
    )
    db.session.add(event)
    target.trust_score = new_score
    db.session.commit()
    from loguru import logger
    logger.info("Admin trust override: admin_id={} target_id={} phone={} new_score={}",
                admin.id, target.id, phone, new_score)
    return redirect(url_for('admin_care'))


# ------------------ Admin: CSV Exports ------------------

@app.route('/admin/export/payments.csv')
@admin_required
def export_payments_csv():
    payments = PaymentRecord.query.order_by(PaymentRecord.created_at.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Date', 'Reference', 'Member', 'Phone', 'Provider', 'Amount (UGX)', 'Status', 'Confirmed At'])
    for p in payments:
        member = User.query.get(p.user_id)
        provider = Provider.query.get(p.provider_id)
        w.writerow([
            p.created_at.strftime('%Y-%m-%d %H:%M'),
            p.reference_code,
            member.name if member else 'N/A',
            member.phone if member else 'N/A',
            provider.name if provider else 'N/A',
            f'{p.amount:.2f}',
            p.status,
            p.provider_confirmed_at.strftime('%Y-%m-%d') if p.provider_confirmed_at else '',
        ])
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="solidarity_payments.csv"'},
    )


@app.route('/admin/export/trust.csv')
@admin_required
def export_trust_csv():
    events = TrustEvent.query.order_by(TrustEvent.timestamp.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Date', 'Member', 'Phone', 'Old Score', 'New Score', 'Delta', 'Reason',
                'F-Repayment', 'F-Witness', 'F-Network', 'F-Activity'])
    for e in events:
        member = User.query.get(e.user_id)
        w.writerow([
            e.timestamp.strftime('%Y-%m-%d %H:%M'),
            member.name if member else 'N/A',
            member.phone if member else 'N/A',
            f'{e.old_score:.4f}' if e.old_score is not None else '',
            f'{e.new_score:.4f}' if e.new_score is not None else '',
            f'{e.delta:.4f}' if e.delta is not None else '',
            e.reason or '',
            f'{e.f_repayment:.4f}' if e.f_repayment is not None else '',
            f'{e.f_witness:.4f}' if e.f_witness is not None else '',
            f'{e.f_network:.4f}' if e.f_network is not None else '',
            f'{e.f_activity:.4f}' if e.f_activity is not None else '',
        ])
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="solidarity_trust_history.csv"'},
    )


# ── Admin: View user profile ──────────────────────────────────────────────────

@app.route('/admin/view-user')
@admin_required
def admin_view_user():
    from trust_graph import compute_draw_ceiling
    search_phone = request.args.get('phone', '').strip()
    target = None
    not_found = False
    care_requests = []
    trust_events = []
    ceiling = 0.0
    primary_comm = None
    if search_phone:
        target = User.query.filter_by(phone=search_phone).first()
        if not target:
            not_found = True
        else:
            care_requests = (CareRequest.query.filter_by(user_id=target.id)
                             .order_by(CareRequest.created_at.desc()).limit(10).all())
            trust_events = (TrustEvent.query.filter_by(user_id=target.id)
                            .order_by(TrustEvent.timestamp.desc()).limit(10).all())
            try:
                ceiling = compute_draw_ceiling(target.id)
            except Exception:
                ceiling = 0.0
            primary_comm = (Community.query.get(target.primary_community_id)
                            if target.primary_community_id else None)
            _log_admin_action(session['user_id'], 'view_user', target_user_id=target.id,
                              details=f'Viewed profile of {target.name} ({target.phone})')
    return render_template('admin_user_profile.html',
                           target=target, search_phone=search_phone, not_found=not_found,
                           care_requests=care_requests, trust_events=trust_events,
                           ceiling=ceiling, primary_comm=primary_comm,
                           admin_phones=ADMIN_PHONES)


# ── Admin: Audit log ──────────────────────────────────────────────────────────

@app.route('/admin/audit-log')
@admin_required
def admin_audit_log():
    logs = (AdminAuditLog.query.order_by(AdminAuditLog.timestamp.desc()).limit(200).all())
    for log in logs:
        log.admin = User.query.get(log.admin_id)
        log.target_user = User.query.get(log.target_user_id) if log.target_user_id else None
    return render_template('admin_audit_log.html', logs=logs)


# ------------------ Leaderboard ------------------

@app.route('/leaderboard')
def leaderboard():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    from sqlalchemy import func
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.session.query(Transaction.user_id, func.sum(Transaction.amount).label('total'))
        .filter(Transaction.type == 'pool_contribution', Transaction.timestamp >= month_start)
        .group_by(Transaction.user_id)
        .order_by(func.sum(Transaction.amount).desc())
        .limit(20)
        .all()
    )
    leaders = []
    user_rank = None
    user_total = 0.0
    for rank, row in enumerate(rows, start=1):
        member = User.query.get(row.user_id)
        if member:
            leaders.append({'user': member, 'total': row.total})
            if row.user_id == user.id:
                user_rank = rank
                user_total = row.total
    leaders = leaders[:10]
    month_label = datetime.utcnow().strftime('%B %Y')
    return render_template('leaderboard.html', user=user, leaders=leaders,
                           user_rank=user_rank, user_total=user_total, month=month_label)


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
