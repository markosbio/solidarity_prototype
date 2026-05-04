import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from loguru import logger

from models import (
    db, User, Transaction, WitnessRequest, SystemState, MpesaTransaction,
    TrustEvent, Provider, CareRequest, Community, CommunityMembership,
)
from trust_graph import compute_draw_ceiling, TrustGraphError
from trust_engine import (
    recompute_trust_score, TrustEngineError, get_combined_score,
    simulate_ceiling_preview,
)
from witness import select_witnesses, record_witness_outcome, WitnessSelectionError
from recovery import update_recovery_parameters, RecoveryError
from providers import providers_bp, pay_provider, _is_admin
from communities import communities_bp
from ussd import ussd_bp

load_dotenv()

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'solidarity-demo-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///solidarity.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# ── Logging ────────────────────────────────────────────────────────────────────

logger.add(
    'logs/solidarity.log',
    rotation='10 MB', retention='14 days', level='INFO',
    format='{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}',
)

# ── Flask-Login ────────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'


@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))


from flask_login import UserMixin
User.__bases__ = (UserMixin, *User.__bases__)

# ── Blueprints ─────────────────────────────────────────────────────────────────

app.register_blueprint(ussd_bp)
app.register_blueprint(providers_bp)
app.register_blueprint(communities_bp)

# ── Database init + column migrations ─────────────────────────────────────────

ADMIN_THRESHOLD = 50.0   # care requests above this require admin approval


def _migrate():
    """Add new columns to existing tables (idempotent, PostgreSQL-safe)."""
    stmts = [
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS primary_community_id INTEGER',
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS is_global_admin BOOLEAN DEFAULT FALSE',
    ]
    for sql in stmts:
        try:
            db.session.execute(db.text(sql))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.debug("Migration (may already exist): {}", e)


with app.app_context():
    os.makedirs('logs', exist_ok=True)
    db.create_all()
    _migrate()

    state = SystemState.query.first()
    if not state:
        state = SystemState(communal_pool_balance=5000.0)
        db.session.add(state)
        db.session.commit()
        logger.info("Seeded global communal pool with KES 5000")

    # Seed one verified demo provider if none exist
    if not Provider.query.first():
        demo = Provider(
            name='Mulago National Hospital',
            provider_code='MULAGO001',
            payment_type='mpesa',
            payment_details='256700000000',
            verified=True,
        )
        db.session.add(demo)
        db.session.commit()
        logger.info("Seeded demo provider: MULAGO001")

# ── Context helpers ────────────────────────────────────────────────────────────

def _get_pool(user: User):
    """Return (pool_obj, attr_name) for the user's active pool."""
    if user.primary_community_id:
        comm = Community.query.get(user.primary_community_id)
        if comm:
            return comm, 'pool_balance'
    return SystemState.query.first(), 'communal_pool_balance'


def _pool_balance(user: User) -> float:
    obj, attr = _get_pool(user)
    return getattr(obj, attr, 0.0)


def _deduct_pool(user: User, amount: float):
    obj, attr = _get_pool(user)
    setattr(obj, attr, getattr(obj, attr, 0.0) - amount)


# ── Standard web routes ────────────────────────────────────────────────────────

@app.route('/')
@login_required
def home():
    return render_template('dashboard.html', user=current_user,
                           is_admin=_is_admin(current_user))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        name = request.form.get('name', '').strip()
        referred_by = request.form.get('referred_by', '').strip()

        if not phone or not name:
            return render_template('register.html', error='Phone and name are required.')
        if User.query.filter_by(phone=phone).first():
            return render_template('register.html', error='Phone number already registered.')

        user = User(phone=phone, name=name, sub_wallet_balance=0.0,
                    trust_score=0.5, region_prefix=phone[:3])
        if referred_by:
            referrer = User.query.filter_by(phone=referred_by).first()
            if referrer:
                user.referred_by = referrer.id

        db.session.add(user)
        db.session.commit()
        login_user(user)
        logger.info("Registered: phone={} name={}", phone, name)
        return redirect(url_for('home'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        user = User.query.filter_by(phone=phone).first()
        if user:
            login_user(user)
            logger.info("Login: phone={}", phone)
            return redirect(url_for('home'))
        logger.warning("Failed login: phone={}", phone)
        return render_template('login.html', error='User not found. Please register first.')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/simulate_roundup', methods=['GET', 'POST'])
@login_required
def simulate_roundup():
    if request.method == 'POST':
        try:
            purchase_amount = float(request.form['purchase_amount'])
        except (KeyError, ValueError):
            return render_template('simulate_roundup.html', user=current_user,
                                   error='Please enter a valid purchase amount.')

        round_up = round(purchase_amount) - purchase_amount
        if round_up <= 0:
            round_up = 0.01

        current_user.sub_wallet_balance += round_up
        db.session.add(Transaction(
            user_id=current_user.id, amount=round_up, type='roundup',
            description=f'Round-up from {purchase_amount:.2f}',
        ))
        db.session.commit()
        logger.info("Round-up: user_id={} amount={:.4f}", current_user.id, round_up)

        try:
            recompute_trust_score(current_user.id, reason='roundup')
        except TrustEngineError as exc:
            logger.error("TrustEngineError after roundup: {}", exc)

        return redirect(url_for('home'))
    return render_template('simulate_roundup.html', user=current_user)


# ── Phase 1: Care request with provider registry ───────────────────────────────

@app.route('/request_care', methods=['GET', 'POST'])
@login_required
def request_care():
    providers = Provider.query.filter_by(verified=True).order_by(Provider.name).all()

    if request.method == 'POST':
        try:
            needed_amount = float(request.form['needed_amount'])
            provider_id = int(request.form['provider_id'])
            is_emergency = request.form.get('is_emergency') == '1'
        except (KeyError, ValueError):
            return render_template('request_care.html', user=current_user,
                                   providers=providers, error='Invalid form data.')

        provider = Provider.query.get(provider_id)
        if not provider or not provider.verified:
            return render_template('request_care.html', user=current_user,
                                   providers=providers, error='Selected provider is not verified.')

        try:
            ceiling = compute_draw_ceiling(current_user.id)
        except TrustGraphError as exc:
            logger.error("TrustGraphError for user_id={}: {}", current_user.id, exc)
            return render_template('request_care.html', user=current_user,
                                   providers=providers, error='Could not compute draw ceiling.')

        from_sub = min(current_user.sub_wallet_balance, needed_amount)
        remaining = needed_amount - from_sub
        current_user.sub_wallet_balance -= from_sub

        from_pool = 0.0
        social_credit = 0.0

        if remaining > 0:
            pool_balance = _pool_balance(current_user)
            allowed = min(remaining, ceiling - from_sub, pool_balance)
            from_pool = max(allowed, 0.0)
            _deduct_pool(current_user, from_pool)
            social_credit = remaining - from_pool
            if social_credit > 0:
                current_user.total_social_credit += social_credit
                try:
                    update_recovery_parameters(current_user.id, social_credit)
                except RecoveryError as exc:
                    logger.error("RecoveryError: {}", exc)
            db.session.commit()

        try:
            witnesses = select_witnesses(
                current_user.id, provider.provider_code,
                community_id=current_user.primary_community_id,
            )
        except WitnessSelectionError as exc:
            logger.error("WitnessSelectionError: {}", exc)
            witnesses = []

        care_req = CareRequest(
            user_id=current_user.id,
            provider_id=provider_id,
            community_id=current_user.primary_community_id,
            amount_needed=needed_amount,
            amount_from_sub=from_sub,
            amount_from_pool=from_pool,
            social_credit=social_credit,
            status='pending',
            is_emergency=is_emergency,
            witness_ids=','.join(str(w.id) for w in witnesses),
        )
        db.session.add(care_req)
        db.session.commit()

        logger.info(
            "CareRequest #{} created: user_id={} provider={} needed={} emergency={}",
            care_req.id, current_user.id, provider.provider_code, needed_amount, is_emergency,
        )

        try:
            recompute_trust_score(current_user.id, reason='care_request')
        except TrustEngineError:
            pass

        return render_template(
            'request_result.html',
            needed=needed_amount, from_sub=from_sub, from_pool=from_pool,
            social_credit=social_credit, request_id=care_req.id,
            provider=provider, is_emergency=is_emergency,
        )

    return render_template('request_care.html', user=current_user, providers=providers)


# ── Phase 1+3: Witness dashboard (privacy-masked) + CareRequest verification ──

@app.route('/witness_dashboard')
@login_required
def witness_dashboard():
    # CareRequests where current user is a witness
    pending_care = [
        cr for cr in CareRequest.query.filter_by(status='pending').all()
        if cr.witness_ids and str(current_user.id) in cr.witness_ids.split(',')
    ]
    # Legacy WitnessRequests (backward compat)
    pending_legacy = [
        r for r in WitnessRequest.query.filter_by(status='pending').all()
        if r.witness_ids and str(current_user.id) in r.witness_ids.split(',')
    ]
    is_admin = _is_admin(current_user)
    return render_template(
        'witness_dashboard.html', user=current_user,
        pending_care=pending_care, pending_legacy=pending_legacy,
        is_admin=is_admin,
    )


@app.route('/care/verify/<int:request_id>/<response>')
@login_required
def verify_care(request_id: int, response: str):
    if response not in ('accept', 'reject'):
        abort(400, description='Invalid response.')

    care_req = CareRequest.query.get_or_404(request_id)

    if care_req.status != 'pending':
        abort(400, description='Request is no longer pending.')

    witness_ids = [w for w in (care_req.witness_ids or '').split(',') if w.strip()]
    if str(current_user.id) not in witness_ids:
        abort(403, description='You are not a witness for this request.')

    # Prevent double-voting
    votes = care_req.witness_votes or ''
    if f"{current_user.id}:" in votes:
        return redirect(url_for('witness_dashboard'))

    care_req.witness_votes = votes + f"{current_user.id}:{response},"
    db.session.commit()

    votes_list = [v for v in care_req.witness_votes.split(',') if v.strip()]
    yes_count = sum(1 for v in votes_list if ':accept' in v)
    total_votes = len(votes_list)
    total_witnesses = len(witness_ids)

    requires_admin = (care_req.amount_needed > ADMIN_THRESHOLD) and not care_req.is_emergency

    if yes_count >= 2:
        care_req.witness_approved = True
        if not requires_admin:
            # Auto-approve: emergency OR small amount
            care_req.status = 'admin_approved'
            db.session.commit()
            if care_req.amount_from_pool > 0:
                pay_provider(care_req.provider_id, care_req.amount_from_pool, care_req.id)
            logger.info("CareRequest #{} auto-approved and paid (emergency={}, amount={})",
                        request_id, care_req.is_emergency, care_req.amount_needed)
        else:
            care_req.status = 'witness_approved'
            db.session.commit()
            logger.info("CareRequest #{} witness-approved, awaiting admin", request_id)

        record_witness_outcome(request_id, 'paid', model='care')
        try:
            recompute_trust_score(care_req.user_id, reason='witness_verified')
        except TrustEngineError:
            pass

    elif total_votes >= total_witnesses:
        care_req.status = 'flagged'
        db.session.commit()
        record_witness_outcome(request_id, 'flagged', model='care')
        logger.info("CareRequest #{} flagged", request_id)
        try:
            recompute_trust_score(care_req.user_id, reason='witness_flagged')
        except TrustEngineError:
            pass

    return redirect(url_for('witness_dashboard'))


# Legacy WitnessRequest verification (backward compat)
@app.route('/verify_witness/<int:request_id>/<response>')
@login_required
def verify_witness(request_id: int, response: str):
    if response not in ('accept', 'reject'):
        abort(400, description='Invalid response value.')

    req = WitnessRequest.query.get(request_id)
    if not req or req.status != 'pending':
        abort(400, description='Request already resolved or invalid.')
    if not req.witness_ids or str(current_user.id) not in req.witness_ids.split(','):
        abort(403, description='You are not a witness for this request.')

    existing_votes = req.votes or ''
    if f"{current_user.id}:" in existing_votes:
        return redirect(url_for('witness_dashboard'))

    req.votes = existing_votes + f"{current_user.id}:{response},"
    db.session.commit()

    votes_list = [v for v in req.votes.split(',') if v.strip()]
    yes_count = sum(1 for v in votes_list if ':accept' in v)
    total_votes = len(votes_list)
    total_witnesses = len([w for w in req.witness_ids.split(',') if w.strip()])

    if yes_count >= 2:
        req.status = 'verified'
        db.session.commit()
        record_witness_outcome(request_id, 'verified')
        try:
            recompute_trust_score(req.user_id, reason='witness_verified')
        except TrustEngineError:
            pass
    elif total_votes >= total_witnesses:
        req.status = 'flagged'
        db.session.commit()
        record_witness_outcome(request_id, 'flagged')
        try:
            recompute_trust_score(req.user_id, reason='witness_flagged')
        except TrustEngineError:
            pass

    return redirect(url_for('witness_dashboard'))


# ── Phase 3: Admin care approval ───────────────────────────────────────────────

@app.route('/admin/care')
@login_required
def admin_care():
    if not _is_admin(current_user):
        abort(403, description='Admin access required.')
    pending = CareRequest.query.filter_by(status='witness_approved').order_by(
        CareRequest.created_at
    ).all()
    return render_template('admin_care.html', user=current_user, pending=pending)


@app.route('/admin/care/<int:request_id>/action', methods=['POST'])
@login_required
def admin_care_action(request_id: int):
    if not _is_admin(current_user):
        abort(403)
    care_req = CareRequest.query.get_or_404(request_id)
    action = request.form.get('action', 'approve')

    if action == 'approve':
        care_req.admin_approved = True
        care_req.admin_id = current_user.id
        care_req.status = 'admin_approved'
        db.session.commit()
        if care_req.amount_from_pool > 0:
            ok = pay_provider(care_req.provider_id, care_req.amount_from_pool, care_req.id)
            if not ok:
                logger.error("pay_provider failed for CareRequest #{}", request_id)
        logger.info("CareRequest #{} admin-approved by user_id={}", request_id, current_user.id)
    elif action == 'deny':
        care_req.status = 'admin_denied'
        db.session.commit()
        logger.info("CareRequest #{} admin-denied by user_id={}", request_id, current_user.id)

    return redirect(url_for('admin_care'))


# ── Trust history ──────────────────────────────────────────────────────────────

@app.route('/trust_history')
@login_required
def trust_history():
    events = (TrustEvent.query
              .filter_by(user_id=current_user.id)
              .order_by(TrustEvent.timestamp.desc())
              .limit(100).all())
    return render_template('trust_history.html', user=current_user, events=events)


# ── Ceiling preview API ────────────────────────────────────────────────────────

@app.route('/api/ceiling_preview')
@login_required
def ceiling_preview():
    try:
        data = simulate_ceiling_preview(current_user.id)
    except Exception as exc:
        logger.error("ceiling_preview failed for user_id={}: {}", current_user.id, exc)
        return jsonify({'error': 'Could not compute preview.'}), 500
    return jsonify(data)


# ── M-Pesa routes ──────────────────────────────────────────────────────────────

@app.route('/mpesa/stk_push', methods=['POST'])
@login_required
def mpesa_stk_push():
    from mpesa import stk_push, MpesaError
    try:
        amount = float(request.form.get('amount', 0))
        purpose = request.form.get('purpose', 'roundup')
        if amount <= 0:
            return jsonify({'error': 'Amount must be positive'}), 400
    except ValueError:
        return jsonify({'error': 'Invalid amount'}), 400

    try:
        result = stk_push(phone=current_user.phone, amount=amount,
                          account_reference='SolidarityPool',
                          description=f'Solidarity {purpose}')
    except MpesaError as exc:
        logger.error("STK Push failed for user_id={}: {}", current_user.id, exc)
        return jsonify({'error': str(exc)}), 502

    mpesa_tx = MpesaTransaction(
        user_id=current_user.id,
        checkout_request_id=result.get('CheckoutRequestID'),
        merchant_request_id=result.get('MerchantRequestID'),
        phone=current_user.phone, amount=amount, purpose=purpose, status='pending',
    )
    db.session.add(mpesa_tx)
    db.session.commit()
    return jsonify({'message': 'STK Push sent. Check your phone.',
                    'checkout_request_id': result.get('CheckoutRequestID')})


@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    from mpesa import parse_stk_callback, MpesaError
    try:
        data = parse_stk_callback(request.get_json(force=True) or {})
    except MpesaError as exc:
        logger.error("M-Pesa callback parse error: {}", exc)
        return jsonify({'ResultCode': 1, 'ResultDesc': 'Parse error'}), 400

    tx = MpesaTransaction.query.filter_by(
        checkout_request_id=data['checkout_request_id']
    ).first()

    if tx:
        if data['result_code'] == 0:
            tx.status = 'success'
            tx.mpesa_receipt = data.get('mpesa_receipt')
            if tx.purpose == 'roundup':
                user = User.query.get(tx.user_id)
                if user:
                    user.sub_wallet_balance += tx.amount
                    db.session.add(Transaction(
                        user_id=user.id, amount=tx.amount, type='mpesa_roundup',
                        description=f'M-Pesa payment {tx.mpesa_receipt}',
                    ))
            logger.info("M-Pesa confirmed: receipt={} amount={}", tx.mpesa_receipt, tx.amount)
            try:
                recompute_trust_score(tx.user_id, reason='mpesa_payment')
            except TrustEngineError:
                pass
        else:
            tx.status = 'failed'
            logger.warning("M-Pesa failed: {}", data['result_desc'])
        db.session.commit()
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})


# ── Simple USSD route (Phase 1 updated: provider code validation) ──────────────

ussd_sessions: dict = {}


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
            return r("Welcome to SolidarityPool\n1. Register\n2. Exit")
        elif step == 1 and inputs[0] == "1":
            return r("Enter your full name:")
        elif step == 2:
            name = inputs[1].strip()
            if not name:
                return r("Name cannot be blank.", end=True)
            new_user = User(phone=phone, name=name, sub_wallet_balance=0.0,
                            trust_score=0.5, region_prefix=phone[:3])
            db.session.add(new_user)
            db.session.commit()
            logger.info("USSD registration: phone={}", phone)
            return r(f"Registered as {name}. Dial again to log in.", end=True)
        else:
            return r("Goodbye.", end=True)

    if step == 0:
        return r(f"Hi {user.name}\n1. Balance\n2. Request care\n3. Trust score\n4. Exit")

    choice = inputs[0]

    if choice == "1":
        state = SystemState.query.first()
        pool = state.communal_pool_balance if state else 0
        return r(f"Wallet: ${user.sub_wallet_balance:.2f}\nPool: ${pool:.2f}")

    if choice == "3":
        score = get_combined_score(user.id)
        ceiling = compute_draw_ceiling(user.id)
        return r(f"Trust: {score:.2f}\nDraw ceiling: ${ceiling:.2f}")

    if choice == "2":
        if step == 1:
            return r("Enter amount needed ($):")
        elif step == 2:
            try:
                amount = float(inputs[1])
                if amount <= 0:
                    return r("Amount must be positive.", end=True)
                ussd_sessions[phone] = {"amount": amount}
                # Show verified providers
                pvds = Provider.query.filter_by(verified=True).limit(3).all()
                menu = "Enter provider code:\n" + "\n".join(
                    f"{p.provider_code}" for p in pvds
                )
                return r(menu)
            except (ValueError, IndexError):
                return r("Invalid amount.", end=True)
        elif step == 3:
            provider_code = inputs[2].strip().upper()
            amount = ussd_sessions.get(phone, {}).get("amount", 0)
            if amount <= 0:
                return r("Session expired. Please try again.", end=True)

            provider = Provider.query.filter_by(
                provider_code=provider_code, verified=True
            ).first()
            if not provider:
                ussd_sessions.pop(phone, None)
                return r(f"Provider code '{provider_code}' not found or not verified.", end=True)

            # Process the care request
            try:
                ceiling = compute_draw_ceiling(user.id)
            except TrustGraphError:
                ussd_sessions.pop(phone, None)
                return r("Could not compute draw ceiling.", end=True)

            from_sub = min(user.sub_wallet_balance, amount)
            remaining = amount - from_sub
            user.sub_wallet_balance -= from_sub

            from_pool = 0.0
            social_credit = 0.0
            state = SystemState.query.first()
            if remaining > 0 and state:
                allowed = min(remaining, ceiling - from_sub, state.communal_pool_balance)
                from_pool = max(allowed, 0.0)
                state.communal_pool_balance -= from_pool
                social_credit = remaining - from_pool
                if social_credit > 0:
                    user.total_social_credit += social_credit
                    try:
                        update_recovery_parameters(user.id, social_credit)
                    except RecoveryError:
                        pass
                db.session.commit()

            try:
                witnesses = select_witnesses(user.id, provider_code,
                                            community_id=user.primary_community_id)
            except WitnessSelectionError:
                witnesses = []

            # Large requests via USSD are treated as non-emergency
            care_req = CareRequest(
                user_id=user.id, provider_id=provider.id,
                community_id=user.primary_community_id,
                amount_needed=amount, amount_from_sub=from_sub,
                amount_from_pool=from_pool, social_credit=social_credit,
                status='pending', is_emergency=False,
                witness_ids=','.join(str(w.id) for w in witnesses),
            )
            db.session.add(care_req)
            db.session.commit()

            try:
                recompute_trust_score(user.id, reason='ussd_care_request')
            except TrustEngineError:
                pass

            ussd_sessions.pop(phone, None)
            return r(
                f"Request #{care_req.id} sent to {provider.name}.\n"
                f"Wallet: ${from_sub:.2f} | Pool: ${from_pool:.2f}\n"
                f"Credit: ${social_credit:.2f}",
                end=True,
            )

    if choice == "4":
        return r("Goodbye.", end=True)

    return r("Invalid option.", end=True)


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(400)
def bad_request(exc):
    return render_template('error.html', code=400, message=exc.description), 400


@app.errorhandler(403)
def forbidden(exc):
    return render_template('error.html', code=403, message=exc.description), 403


@app.errorhandler(404)
def not_found(exc):
    return render_template('error.html', code=404, message='Page not found.'), 404


@app.errorhandler(500)
def server_error(exc):
    logger.error("Internal server error: {}", exc)
    return render_template('error.html', code=500,
                           message='An internal error occurred. Please try again.'), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
