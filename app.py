import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from loguru import logger

from models import db, User, Transaction, WitnessRequest, SystemState, MpesaTransaction, TrustEvent
from trust_graph import compute_draw_ceiling, TrustGraphError
from trust_engine import recompute_trust_score, TrustEngineError
from witness import select_witnesses, record_witness_outcome, WitnessSelectionError
from recovery import update_recovery_parameters, RecoveryError
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
    rotation='10 MB',
    retention='14 days',
    level='INFO',
    format='{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}',
)

# ── Flask-Login ────────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'


@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))


# Flask-Login requires these on the User model; add them without touching models.py
from flask_login import UserMixin
User.__bases__ = (UserMixin, *User.__bases__)

# ── Blueprints ─────────────────────────────────────────────────────────────────

app.register_blueprint(ussd_bp)

# ── Database init ──────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    os.makedirs('logs', exist_ok=True)
    state = SystemState.query.first()
    if not state:
        state = SystemState(communal_pool_balance=5000.0)
        db.session.add(state)
        db.session.commit()
        logger.info("Seeded communal pool with KES 5000")

# ── Web Routes ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def home():
    return render_template('dashboard.html', user=current_user)


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

        user = User(
            phone=phone,
            name=name,
            sub_wallet_balance=0.0,
            trust_score=0.5,
            region_prefix=phone[:3],
        )
        if referred_by:
            referrer = User.query.filter_by(phone=referred_by).first()
            if referrer:
                user.referred_by = referrer.id

        db.session.add(user)
        db.session.commit()
        login_user(user)
        logger.info("New user registered: phone={} name={}", phone, name)
        return redirect(url_for('home'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        user = User.query.filter_by(phone=phone).first()
        if user:
            login_user(user)
            logger.info("User logged in: phone={}", phone)
            return redirect(url_for('home'))
        logger.warning("Failed login attempt for phone={}", phone)
        return render_template('login.html', error='User not found. Please register first.')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logger.info("User logged out: id={}", current_user.id)
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
        tx = Transaction(
            user_id=current_user.id,
            amount=round_up,
            type='roundup',
            description=f'Round-up from {purchase_amount:.2f}',
        )
        db.session.add(tx)
        db.session.commit()
        logger.info("Round-up: user_id={} amount={:.4f}", current_user.id, round_up)

        try:
            recompute_trust_score(current_user.id, reason='roundup')
        except TrustEngineError as exc:
            logger.error("TrustEngineError after roundup for user_id={}: {}", current_user.id, exc)

        return redirect(url_for('home'))

    return render_template('simulate_roundup.html', user=current_user)


@app.route('/request_care', methods=['GET', 'POST'])
@login_required
def request_care():
    if request.method == 'POST':
        try:
            needed_amount = float(request.form['needed_amount'])
            provider_id = request.form.get('provider_id', '').strip() or 'unknown'
        except (KeyError, ValueError):
            return render_template('request_care.html', user=current_user,
                                   error='Please enter a valid amount.')

        try:
            ceiling = compute_draw_ceiling(current_user.id)
        except TrustGraphError as exc:
            logger.error("TrustGraphError for user_id={}: {}", current_user.id, exc)
            return render_template('request_care.html', user=current_user,
                                   error='Could not compute your draw ceiling. Please try again.')

        from_sub = min(current_user.sub_wallet_balance, needed_amount)
        remaining = needed_amount - from_sub
        current_user.sub_wallet_balance -= from_sub

        from_pool = 0.0
        social_credit = 0.0

        if remaining > 0:
            state = SystemState.query.first()
            if state:
                allowed_from_pool = min(remaining, ceiling - from_sub, state.communal_pool_balance)
                from_pool = max(allowed_from_pool, 0.0)
                state.communal_pool_balance -= from_pool
                social_credit = remaining - from_pool

                if social_credit > 0:
                    current_user.total_social_credit += social_credit
                    try:
                        update_recovery_parameters(current_user.id, social_credit)
                    except RecoveryError as exc:
                        logger.error("RecoveryError for user_id={}: {}", current_user.id, exc)

                db.session.commit()

        try:
            witnesses = select_witnesses(current_user.id, provider_id)
        except WitnessSelectionError as exc:
            logger.error("WitnessSelectionError for user_id={}: {}", current_user.id, exc)
            witnesses = []

        req = WitnessRequest(
            user_id=current_user.id,
            needed_amount=needed_amount,
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
            "Care request submitted: user_id={} needed={} from_sub={} from_pool={} social_credit={}",
            current_user.id, needed_amount, from_sub, from_pool, social_credit
        )

        try:
            recompute_trust_score(current_user.id, reason='care_request')
        except TrustEngineError as exc:
            logger.error("TrustEngineError after care_request for user_id={}: {}", current_user.id, exc)

        return render_template(
            'request_result.html',
            needed=needed_amount,
            from_sub=from_sub,
            from_pool=from_pool,
            social_credit=social_credit,
            request_id=req.id,
        )

    return render_template('request_care.html', user=current_user)


@app.route('/witness_dashboard')
@login_required
def witness_dashboard():
    pending = []
    for req in WitnessRequest.query.filter_by(status='pending').all():
        if req.witness_ids and str(current_user.id) in req.witness_ids.split(','):
            pending.append(req)
    return render_template('witness_dashboard.html', user=current_user, pending=pending)


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

    # Prevent double-voting
    existing_votes = req.votes or ''
    if f"{current_user.id}:" in existing_votes:
        logger.warning("Duplicate vote attempt by user_id={} for request_id={}", current_user.id, request_id)
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
        logger.info("Request {} verified by consensus", request_id)
        try:
            recompute_trust_score(req.user_id, reason='witness_verified')
        except TrustEngineError as exc:
            logger.error("TrustEngineError after witness_verified for user_id={}: {}", req.user_id, exc)
    elif total_votes >= total_witnesses:
        req.status = 'flagged'
        db.session.commit()
        record_witness_outcome(request_id, 'flagged')
        logger.info("Request {} flagged — insufficient yes votes", request_id)
        try:
            recompute_trust_score(req.user_id, reason='witness_flagged')
        except TrustEngineError as exc:
            logger.error("TrustEngineError after witness_flagged for user_id={}: {}", req.user_id, exc)

    return redirect(url_for('witness_dashboard'))


# ── Trust History Route ────────────────────────────────────────────────────────

@app.route('/trust_history')
@login_required
def trust_history():
    events = (
        TrustEvent.query
        .filter_by(user_id=current_user.id)
        .order_by(TrustEvent.timestamp.desc())
        .limit(100)
        .all()
    )
    return render_template('trust_history.html', user=current_user, events=events)


# ── M-Pesa Routes ──────────────────────────────────────────────────────────────

@app.route('/mpesa/stk_push', methods=['POST'])
@login_required
def mpesa_stk_push():
    """Initiate an M-Pesa STK Push for the logged-in user."""
    from mpesa import stk_push, MpesaError

    try:
        amount = float(request.form.get('amount', 0))
        purpose = request.form.get('purpose', 'roundup')
        if amount <= 0:
            return jsonify({'error': 'Amount must be positive'}), 400
    except ValueError:
        return jsonify({'error': 'Invalid amount'}), 400

    try:
        result = stk_push(
            phone=current_user.phone,
            amount=amount,
            account_reference='SolidarityPool',
            description=f'Solidarity {purpose}',
        )
    except MpesaError as exc:
        logger.error("STK Push failed for user_id={}: {}", current_user.id, exc)
        return jsonify({'error': str(exc)}), 502

    mpesa_tx = MpesaTransaction(
        user_id=current_user.id,
        checkout_request_id=result.get('CheckoutRequestID'),
        merchant_request_id=result.get('MerchantRequestID'),
        phone=current_user.phone,
        amount=amount,
        purpose=purpose,
        status='pending',
    )
    db.session.add(mpesa_tx)
    db.session.commit()

    return jsonify({
        'message': 'STK Push sent. Check your phone.',
        'checkout_request_id': result.get('CheckoutRequestID'),
    })


@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Safaricom sends payment confirmation here."""
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
            # Credit sub-wallet for successful round-up payments
            if tx.purpose == 'roundup':
                user = User.query.get(tx.user_id)
                if user:
                    user.sub_wallet_balance += tx.amount
                    db.session.add(Transaction(
                        user_id=user.id,
                        amount=tx.amount,
                        type='mpesa_roundup',
                        description=f'M-Pesa payment {tx.mpesa_receipt}',
                    ))
            logger.info("M-Pesa payment confirmed: receipt={} amount={}", tx.mpesa_receipt, tx.amount)
            try:
                recompute_trust_score(tx.user_id, reason='mpesa_payment')
            except TrustEngineError as exc:
                logger.error("TrustEngineError after mpesa_payment for user_id={}: {}", tx.user_id, exc)
        else:
            tx.status = 'failed'
            logger.warning("M-Pesa payment failed: checkout_id={} desc={}", data['checkout_request_id'], data['result_desc'])

        db.session.commit()

    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})


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
    return render_template('error.html', code=500, message='An internal error occurred. Please try again.'), 500

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
