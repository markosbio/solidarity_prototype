from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from models import db, User, Transaction, Community, CommunityMembership, Provider, CareRequest, SystemState, PaymentRecord, MpesaTopup
from trust_graph import compute_draw_ceiling
from witness import select_witnesses
from recovery import update_recovery_parameters
from payments import pay_provider
from mpesa import stk_push, parse_stk_callback, MpesaError
from trust_engine import get_combined_score
from communities import communities_bp
from providers_bp import providers_bp
from ussd import ussd_bp
import random
import string
import os
import io
import csv
from datetime import datetime, timedelta
from notifications import notify_ceiling_increase, notify_pool_low
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', os.environ.get('SECRET_KEY', 'solidarity-dev-key-change-in-production'))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///solidarity.db')
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
        return render_template('dashboard.html', user=user, primary_comm=primary_comm,
                               is_admin=is_admin, ceiling=ceiling, pool_health=ph)
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
        # User registration (also auto-login for existing)
        phone = request.form['phone']
        name = request.form['name']
        referred_by = request.form.get('referred_by')
        existing = User.query.filter_by(phone=phone).first()
        if existing:
            session['user_id'] = existing.id
            return redirect(url_for('home'))
        user = User(phone=phone, name=name, sub_wallet_balance=0.0, trust_score=0.5)
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
    if request.method == 'POST':
        purchase_amount = float(request.form['purchase_amount'])
        round_up = round(round(purchase_amount) - purchase_amount, 4)
        if round_up <= 0:
            round_up = 0.01
        try:
            old_ceiling = compute_draw_ceiling(user.id)
        except Exception:
            old_ceiling = 0.0
        to_wallet, to_pool, to_fee = _roundup_split(round_up)
        user.sub_wallet_balance += to_wallet
        primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
        if primary_comm and to_pool > 0:
            primary_comm.pool_balance += to_pool
            ph = _pool_health(primary_comm.pool_balance)
            notify_pool_low(primary_comm, ph['pct'])
        db.session.add(Transaction(user_id=user.id, amount=to_wallet, type='roundup',
                                   description=f'Round-up wallet share from UGX {purchase_amount:.0f}'))
        if to_pool > 0 and primary_comm:
            db.session.add(Transaction(user_id=user.id, amount=to_pool, type='pool_contribution',
                                       description=f'Round-up pool share from UGX {purchase_amount:.0f}'))
        if to_fee > 0:
            db.session.add(Transaction(user_id=user.id, amount=to_fee, type='platform_fee',
                                       description=f'Round-up platform fee from UGX {purchase_amount:.0f}'))
        db.session.commit()
        try:
            new_ceiling = compute_draw_ceiling(user.id)
            notify_ceiling_increase(user, new_ceiling, old_ceiling)
        except Exception:
            pass
        return redirect(url_for('home'))
    return render_template('simulate_roundup.html', user=user, mpesa_enabled=mpesa_enabled,
                           wallet_pct=wallet_pct, pool_pct=pool_pct, fee_pct=fee_pct)


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
        care_req = CareRequest(
            user_id=user.id, community_id=community.id, provider_id=provider_id,
            amount_needed=needed_amount, amount_from_sub=from_sub, amount_from_pool=from_pool,
            social_credit=social_credit, is_emergency=is_emergency, status='pending_witness'
        )
        db.session.add(care_req)
        db.session.commit()
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
def admin_care():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    _check_emergency_auto_approvals()
    pending = CareRequest.query.filter_by(status='pending_admin', admin_approved=False).all()
    for cr in pending:
        cr.requester = User.query.get(cr.user_id)
    return render_template('admin_care.html', user=user, pending=pending)

@app.route('/admin/care/<int:request_id>', methods=['POST'])
def admin_care_action(request_id):
    if 'user_id' not in session:
        return redirect(url_for('register'))
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
def admin_approve(request_id, action):
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get(request_id)
    if not care_req:
        return "Request not found"
    community = Community.query.get(care_req.community_id)
    membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=community.id).first()
    if not membership or membership.role not in ['admin', 'coadmin']:
        return "Not authorized"
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
    return redirect(url_for('login'))

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

@app.route('/ussd', methods=['GET', 'POST'])
def ussd():
    phone = request.values.get("phoneNumber", "")
    text = request.values.get("text", "")
    user = User.query.filter_by(phone=phone).first()
    inputs = text.split('*') if text else []
    step = len(inputs)

    def r(msg, end=False):
        return f"{'END' if end else 'CON'} {msg}"

    if not user:
        if step == 0:
            return r("Welcome to Solidarity Health Pool.\nNot registered.\n1. Register\n2. Exit")
        elif step == 1 and inputs[0] == "1":
            return r("Enter your full name:")
        elif step == 2:
            name = inputs[1]
            new_user = User(phone=phone, name=name, sub_wallet_balance=0.0, trust_score=0.5)
            db.session.add(new_user)
            db.session.commit()
            default_comm = Community.query.first()
            if default_comm:
                membership = CommunityMembership(user_id=new_user.id, community_id=default_comm.id, role='member')
                db.session.add(membership)
                new_user.primary_community_id = default_comm.id
                db.session.commit()
            return r(f"Registered {name}. Use same number to access services.", end=True)
        return r("Invalid.", end=True)

    primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    membership = None
    role = 'member'
    if primary_comm:
        membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=user.primary_community_id).first()
        role = membership.role if membership else 'member'

    if not primary_comm and step == 0:
        return r("You are not in any community.\n4. Community (create/join)\n7. Help/FAQ\n0. Exit")

    if step == 0:
        mpesa_configured = bool(os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET'))
        menu = f"Hi {user.name}\n1. Balance\n2. Request care\n3. Trust score\n4. Community\n5. Witness tasks\n7. Help/FAQ\n"
        if mpesa_configured:
            menu += "8. Top up via M-Pesa\n"
        if role in ['admin', 'coadmin'] and primary_comm:
            menu += "6. Admin panel\n"
        menu += "0. Exit"
        return r(menu)

    choice = inputs[0]

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

    if choice == "3":
        score = get_combined_score(user.id)
        return r(f"Trust score: {score:.2f}")

    if choice == "4":
        if step == 1:
            return r("1. Create community\n2. Join community\n0. Back")
        elif step == 2:
            sub = inputs[1]
            if sub == "1":
                ussd_sessions[phone] = {"state": "create_name"}
                return r("Enter community name:")
            elif sub == "2":
                ussd_sessions[phone] = {"state": "join_invite"}
                return r("Enter invite code:")
            else:
                return r("Invalid.", end=True)
        elif step == 3:
            state = ussd_sessions.get(phone, {}).get("state")
            if state == "create_name":
                comm_name = inputs[2]
                invite = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                new_comm = Community(name=comm_name, invite_code=invite, pool_balance=0.0, admin_user_id=user.id)
                db.session.add(new_comm)
                db.session.commit()
                membership = CommunityMembership(user_id=user.id, community_id=new_comm.id, role='admin')
                db.session.add(membership)
                user.primary_community_id = new_comm.id
                db.session.commit()
                ussd_sessions.pop(phone, None)
                return r(f"Community '{comm_name}' created. Invite code: {invite}", end=True)
            elif state == "join_invite":
                invite_code = inputs[2].strip().upper()
                comm = Community.query.filter_by(invite_code=invite_code).first()
                if not comm:
                    return r("Invalid invite code.", end=True)
                existing = CommunityMembership.query.filter_by(user_id=user.id, community_id=comm.id).first()
                if existing:
                    return r("Already a member.", end=True)
                membership = CommunityMembership(user_id=user.id, community_id=comm.id, role='member')
                db.session.add(membership)
                user.primary_community_id = comm.id
                db.session.commit()
                return r(f"Joined {comm.name}.", end=True)
        return r("Session expired.", end=True)

    if choice == "2":
        user_communities = get_user_communities(user.id)
        if not user_communities:
            return r("Join a community first (option 4).", end=True)
        if len(user_communities) == 1:
            selected_comm = user_communities[0]
            if step == 1:
                try:
                    _ceil = compute_draw_ceiling(user.id)
                except Exception:
                    _ceil = 0.0
                ussd_sessions[phone] = {"selected_comm_id": selected_comm.id, "state": "awaiting_amount", "ceiling": _ceil}
                return r(f"Your ceiling: UGX {_ceil:,.0f}\nEnter amount (UGX):")
            elif step == 2:
                try:
                    amount = float(inputs[1])
                except:
                    return r("Invalid amount.", end=True)
                ussd_sessions[phone]["amount"] = amount
                ussd_sessions[phone]["state"] = "awaiting_provider"
                return r("Enter provider code (e.g., MULAGO001):")
            elif step == 3:
                provider_code = inputs[2].strip().upper()
                provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
                if not provider:
                    sample = Provider.query.filter_by(verified=True).first()
                    hint = sample.provider_code if sample else 'MULAGO001'
                    return r(f"Invalid code '{provider_code}'.\nTry {hint} or ask your clinic.", end=True)
                amount = ussd_sessions[phone]["amount"]
                ussd_sessions[phone]["provider_id"] = provider.id
                return r("Emergency? (1=Yes, 2=No)")
            elif step == 4:
                emerg = (inputs[3] == "1")
                amount = ussd_sessions[phone]["amount"]
                provider_id = ussd_sessions[phone]["provider_id"]
                selected_comm_id = ussd_sessions[phone]["selected_comm_id"]
                selected_comm = Community.query.get(selected_comm_id)
                ceiling = ussd_sessions[phone].get("ceiling") or compute_draw_ceiling(user.id)
                from_sub = min(user.sub_wallet_balance, amount)
                remaining = amount - from_sub
                user.sub_wallet_balance -= from_sub
                from_pool = 0.0
                social_credit = 0.0
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
                    social_credit=social_credit, is_emergency=emerg, status='pending_witness'
                )
                db.session.add(care_req)
                db.session.commit()
                witnesses = select_witnesses(user.id, provider_id, community_id=selected_comm.id)
                witness_ids = ','.join(str(w.id) for w in witnesses)
                care_req.witness_ids = witness_ids
                db.session.commit()
                ussd_sessions.pop(phone, None)
                ceiling_remaining = max(0.0, ceiling - from_pool)
                need_admin = (amount > 50) or emerg
                msg = f"Request submitted.\nCeiling remaining: ${ceiling_remaining:.0f}\n{len(witnesses)} witnesses notified."
                if emerg:
                    msg += " Auto-approved in 2h if no admin."
                elif need_admin:
                    msg += " Admin approval required."
                return r(msg, end=True)
            else:
                return r("Invalid step.", end=True)
        else:
            if step == 1:
                try:
                    _ceil = compute_draw_ceiling(user.id)
                except Exception:
                    _ceil = 0.0
                comm_list = "\n".join([f"{i+1}. {c.name}" for i, c in enumerate(user_communities)])
                ussd_sessions[phone] = {"state": "choose_comm", "communities": [(c.id, c.name) for c in user_communities], "ceiling": _ceil}
                return r(f"Your ceiling: UGX {_ceil:,.0f}\nSelect community:\n{comm_list}\n0. Back")
            elif step == 2 and ussd_sessions.get(phone, {}).get("state") == "choose_comm":
                idx = int(inputs[1]) - 1
                comms = ussd_sessions[phone]["communities"]
                if 0 <= idx < len(comms):
                    selected_comm_id = comms[idx][0]
                    ussd_sessions[phone]["selected_comm_id"] = selected_comm_id
                    ussd_sessions[phone]["state"] = "awaiting_amount"
                    return r("Enter amount (USD):")
                else:
                    return r("Invalid choice.", end=True)
            elif step == 3 and ussd_sessions.get(phone, {}).get("state") == "awaiting_amount":
                try:
                    amount = float(inputs[2])
                except:
                    return r("Invalid amount.", end=True)
                ussd_sessions[phone]["amount"] = amount
                ussd_sessions[phone]["state"] = "awaiting_provider"
                return r("Enter provider code (e.g., MULAGO001):")
            elif step == 4:
                provider_code = inputs[3].strip().upper()
                provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
                if not provider:
                    sample = Provider.query.filter_by(verified=True).first()
                    hint = sample.provider_code if sample else 'MULAGO001'
                    return r(f"Invalid code '{provider_code}'.\nTry {hint} or ask your clinic.", end=True)
                amount = ussd_sessions[phone]["amount"]
                ussd_sessions[phone]["provider_id"] = provider.id
                return r("Emergency? (1=Yes, 2=No)")
            elif step == 5:
                emerg = (inputs[4] == "1")
                amount = ussd_sessions[phone]["amount"]
                provider_id = ussd_sessions[phone]["provider_id"]
                selected_comm_id = ussd_sessions[phone]["selected_comm_id"]
                selected_comm = Community.query.get(selected_comm_id)
                ceiling = ussd_sessions[phone].get("ceiling") or compute_draw_ceiling(user.id)
                from_sub = min(user.sub_wallet_balance, amount)
                remaining = amount - from_sub
                user.sub_wallet_balance -= from_sub
                from_pool = 0.0
                social_credit = 0.0
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
                    social_credit=social_credit, is_emergency=emerg, status='pending_witness'
                )
                db.session.add(care_req)
                db.session.commit()
                witnesses = select_witnesses(user.id, provider_id, community_id=selected_comm.id)
                witness_ids = ','.join(str(w.id) for w in witnesses)
                care_req.witness_ids = witness_ids
                db.session.commit()
                ussd_sessions.pop(phone, None)
                ceiling_remaining = max(0.0, ceiling - from_pool)
                need_admin = (amount > 50) or emerg
                msg = f"Request submitted.\nCeiling remaining: ${ceiling_remaining:.0f}\n{len(witnesses)} witnesses notified."
                if emerg:
                    msg += " Auto-approved in 2h if no admin."
                elif need_admin:
                    msg += " Admin approval required."
                return r(msg, end=True)
            else:
                return r("Invalid step.", end=True)

    if choice == "5":
        pending = []
        requests = CareRequest.query.filter_by(status='pending_witness').all()
        for req in requests:
            if req.witness_ids and str(user.id) in req.witness_ids.split(','):
                pending.append(req)
        if not pending:
            return r("No pending witness requests.")
        req = pending[0]
        ussd_sessions[phone] = {"witness_req_id": req.id}
        return r(f"Request #{req.id}: ${req.amount_needed}\n1. Accept\n2. Reject")
    if step == 2 and choice == "5":
        req_id = ussd_sessions.get(phone, {}).get("witness_req_id")
        if not req_id:
            return r("Session error.", end=True)
        care_req = CareRequest.query.get(req_id)
        if not care_req or care_req.status != 'pending_witness':
            return r("Request already processed.", end=True)
        vote = inputs[1]
        response = "accept" if vote == "1" else "reject"
        votes = care_req.witness_votes.split(',') if care_req.witness_votes else []
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
                success, ref = pay_provider(
                    care_request_id=care_req.id, amount=care_req.amount_from_pool,
                    provider_id=care_req.provider_id, user_id=care_req.user_id,
                    community_id=care_req.community_id
                )
                if success:
                    care_req.payment_transaction_id = ref
            db.session.commit()
        elif len(votes) >= total:
            care_req.status = 'rejected'
            db.session.commit()
        ussd_sessions.pop(phone, None)
        return r("Vote recorded. Thank you.", end=True)

    if choice == "6" and role in ['admin', 'coadmin'] and primary_comm:
        if step == 1:
            return r("Admin:\n1. Approve requests\n2. Invite code\n3. Members\n0. Back")
        elif step == 2:
            sub = inputs[1]
            if sub == "1":
                pending_reqs = CareRequest.query.filter_by(community_id=primary_comm.id, status='pending_admin', admin_approved=False).all()
                if not pending_reqs:
                    return r("No pending approvals.")
                ussd_sessions[phone] = {'admin_pending': [r.id for r in pending_reqs], 'admin_idx': 0}
                req = pending_reqs[0]
                requester = User.query.get(req.user_id)
                prov = Provider.query.get(req.provider_id)
                return r(f"Request by {requester.name}: ${req.amount_needed} at {prov.name}\n1. Approve\n2. Reject\n0. Next")
            elif sub == "2":
                return r(f"Invite code: {primary_comm.invite_code}")
            elif sub == "3":
                members = CommunityMembership.query.filter_by(community_id=primary_comm.id).all()
                names = [User.query.get(m.user_id).name for m in members[:5]]
                msg = "Members:\n" + "\n".join(names)
                if len(members) > 5:
                    msg += f"\n+{len(members)-5} more"
                return r(msg)
            else:
                return r("Invalid.", end=True)
        elif step == 3 and inputs[1] == "1":
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
                success, ref = pay_provider(
                    care_request_id=care_req.id, amount=care_req.amount_from_pool,
                    provider_id=care_req.provider_id, user_id=care_req.user_id,
                    community_id=care_req.community_id
                )
                if success:
                    care_req.payment_transaction_id = ref
                db.session.commit()
                msg = f"Request #{req_id} approved."
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
                    requester = User.query.get(next_req.user_id)
                    prov = Provider.query.get(next_req.provider_id)
                    return r(f"Request by {requester.name}: UGX {next_req.amount_needed:,.0f} at {prov.name}\n1. Approve\n2. Reject\n0. Next")
                else:
                    return r("All requests processed.", end=True)
            return r(msg + "\nContinue? 1. Yes 2. No")
        return r("Invalid.", end=True)

    if choice == "8":
        if step == 1:
            return r("Enter top-up amount (UGX):")
        try:
            topup_amount = float(inputs[1])
            if topup_amount < 1:
                raise ValueError("Minimum UGX 1")
        except (ValueError, IndexError):
            return r("Invalid amount. Please enter a whole number.", end=True)
        if not (os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET')):
            return r("M-Pesa is not configured. Contact support.", end=True)
        try:
            result = stk_push(
                phone=phone,
                amount=topup_amount,
                account_reference='SolidarityPool',
                description=f'USSD top-up for {user.name}',
            )
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
            return r(
                f"M-Pesa prompt sent to {phone}.\n"
                f"Amount: UGX {int(topup_amount)}\n"
                "Approve on your phone to top up your wallet.",
                end=True,
            )
        except MpesaError as exc:
            logger.error("USSD STK push failed: {}", exc)
            return r("M-Pesa prompt failed. Try again later.", end=True)

    if choice == "0":
        return r("Goodbye. Stay well!", end=True)

    if choice == "7":
        if step == 1:
            return r(
                "SolidarityPool Help\n"
                "1. What is SolidarityPool?\n"
                "2. How do round-ups work?\n"
                "3. How do I request care funds?\n"
                "4. What is a trust score?\n"
                "5. What is a draw ceiling?\n"
                "0. Back"
            )
        topic = inputs[1] if step > 1 else ''
        if topic == '1':
            return r(
                "SolidarityPool is a community mutual-aid fund. "
                "Members save via micro round-ups and can access care funds for medical emergencies.",
                end=True
            )
        elif topic == '2':
            return r(
                "When you buy something (e.g. UGX 12,500), we round up to UGX 13,000 "
                "and save the UGX 500. "
                "70% goes to your wallet, 20% to the community pool, 10% is a platform fee.",
                end=True
            )
        elif topic == '3':
            return r(
                "Choose option 2 from the main menu. Enter the amount, then your clinic's "
                "provider code (e.g. MULAGO001 — ask your clinic). "
                "Three community members will verify your request.",
                end=True
            )
        elif topic == '4':
            return r(
                "Your trust score (0-1) measures your reliability: "
                "repaying social credit, accurate witness votes, "
                "network connections, and regular round-up contributions.",
                end=True
            )
        elif topic == '5':
            return r(
                "Your draw ceiling is the maximum you can request from the pool. "
                "It grows as your trust score improves and the pool stays healthy. "
                "Check it with option 1 (Balance).",
                end=True
            )
        elif topic == '0':
            return r("Returning to main menu. Dial again to continue.")
        return r("Invalid help topic. Dial again.", end=True)

    return r("Invalid choice.", end=True)


# ------------------ Admin: Trust Override ------------------

@app.route('/admin/trust')
def admin_trust_page():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    return render_template('admin_trust.html', user=user)


@app.route('/admin/trust/override', methods=['POST'])
def admin_trust_override_by_phone():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    admin = User.query.get(session['user_id'])
    memberships = CommunityMembership.query.filter_by(user_id=admin.id).all()
    is_comm_admin = admin.is_global_admin or any(m.role in ['admin', 'coadmin'] for m in memberships)
    if not is_comm_admin:
        return "Not authorized — only community admins can override trust scores.", 403
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
def export_payments_csv():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    admin = User.query.get(session['user_id'])
    admin_comm_ids = [
        m.community_id for m in CommunityMembership.query.filter_by(user_id=admin.id).all()
        if m.role in ['admin', 'coadmin']
    ]
    if not admin_comm_ids and not admin.is_global_admin:
        return "Not authorized — community admin access required.", 403
    if admin.is_global_admin:
        payments = PaymentRecord.query.order_by(PaymentRecord.created_at.desc()).all()
    else:
        payments = PaymentRecord.query.filter(
            PaymentRecord.community_id.in_(admin_comm_ids)
        ).order_by(PaymentRecord.created_at.desc()).all()
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
def export_trust_csv():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    admin = User.query.get(session['user_id'])
    admin_comm_ids = [
        m.community_id for m in CommunityMembership.query.filter_by(user_id=admin.id).all()
        if m.role in ['admin', 'coadmin']
    ]
    if not admin_comm_ids and not admin.is_global_admin:
        return "Not authorized — community admin access required.", 403
    if admin.is_global_admin:
        events = TrustEvent.query.order_by(TrustEvent.timestamp.desc()).all()
    else:
        member_ids = [
            m.user_id for m in CommunityMembership.query.filter(
                CommunityMembership.community_id.in_(admin_comm_ids)
            ).all()
        ]
        events = TrustEvent.query.filter(
            TrustEvent.user_id.in_(member_ids)
        ).order_by(TrustEvent.timestamp.desc()).all()
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
