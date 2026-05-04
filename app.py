from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from models import db, User, Transaction, WitnessRequest
from trust_graph import compute_draw_ceiling
from witness import select_witnesses, verify_consensus
from recovery import update_recovery_parameters
import random
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'solidarity-demo-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///solidarity.db'
db.init_app(app)

# Create tables on first run
with app.app_context():
    db.create_all()
    # Seed a communal pool balance (simulated)
    from models import SystemState
    state = SystemState.query.first()
    if not state:
        state = SystemState(communal_pool_balance=5000.0)
        db.session.add(state)
        db.session.commit()

# ------------------ Routes ------------------
@app.route('/')
def home():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        return render_template('dashboard.html', user=user)
    return redirect(url_for('register'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        phone = request.form['phone']
        name = request.form['name']
        referred_by = request.form.get('referred_by')
        user = User(phone=phone, name=name, sub_wallet_balance=0.0, trust_score=0.5)
        if referred_by:
            referrer = User.query.filter_by(phone=referred_by).first()
            if referrer:
                user.referred_by = referrer.id
        db.session.add(user)
        db.session.commit()
        session['user_id'] = user.id
        return redirect(url_for('home'))
    return render_template('register.html')

@app.route('/simulate_roundup', methods=['GET', 'POST'])
def simulate_roundup():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        purchase_amount = float(request.form['purchase_amount'])
        round_up = round(purchase_amount) - purchase_amount
        if round_up <= 0:
            round_up = 0.01  # minimum if exact dollar
        user.sub_wallet_balance += round_up
        # Record transaction
        tx = Transaction(user_id=user.id, amount=round_up, type='roundup', description=f'Round-up from {purchase_amount}')
        db.session.add(tx)
        db.session.commit()
        return redirect(url_for('home'))
    return render_template('simulate_roundup.html', user=user)

@app.route('/request_care', methods=['GET', 'POST'])
def request_care():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        needed_amount = float(request.form['needed_amount'])
        provider_id = request.form['provider_id']  # simple string
        
        # Step 1: compute draw ceiling
        ceiling = compute_draw_ceiling(user.id)
        
        # Step 2: cover from sub-wallet first
        from_sub = min(user.sub_wallet_balance, needed_amount)
        remaining = needed_amount - from_sub
        user.sub_wallet_balance -= from_sub
        
        from_pool = 0.0
        social_credit = 0.0
        if remaining > 0:
            state = SystemState.query.first()
            allowed_from_pool = min(remaining, ceiling - from_sub, state.communal_pool_balance)
            from_pool = allowed_from_pool
            state.communal_pool_balance -= from_pool
            social_credit = remaining - from_pool
            if social_credit > 0:
                user.total_social_credit += social_credit
                # Trigger adaptive recovery (update future round-up intensifier)
                update_recovery_parameters(user.id, social_credit)
            db.session.commit()
        
        # Step 3: Witness mesh verification (async simulation)
        witnesses = select_witnesses(user.id, provider_id)
        request_obj = WitnessRequest(
            user_id=user.id,
            needed_amount=needed_amount,
            provider_id=provider_id,
            from_sub=from_sub,
            from_pool=from_pool,
            social_credit=social_credit,
            status='pending'
        )
        db.session.add(request_obj)
        db.session.commit()
        
        # For demo, we'll store witnesses in session to simulate later
        request_obj.witness_ids = ','.join(str(w.id) for w in witnesses)
        db.session.commit()
        
        return render_template('request_result.html', 
                               needed=needed_amount,
                               from_sub=from_sub,
                               from_pool=from_pool,
                               social_credit=social_credit,
                               request_id=request_obj.id)
    return render_template('request_care.html', user=user)

@app.route('/witness_dashboard')
def witness_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    # Find pending witness requests where this user is a witness
    pending = []
    all_requests = WitnessRequest.query.filter_by(status='pending').all()
    for req in all_requests:
        if req.witness_ids and str(user.id) in req.witness_ids.split(','):
            pending.append(req)
    return render_template('witness_dashboard.html', user=user, pending=pending)

@app.route('/verify_witness/<int:request_id>/<response>')
def verify_witness(request_id, response):
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    req = WitnessRequest.query.get(request_id)
    if not req or req.status != 'pending':
        return "Request already resolved or invalid", 400
    # Check if user is indeed a witness
    if str(user.id) not in req.witness_ids.split(','):
        return "Not authorized", 403
    # Record vote (in real system you'd track per-witness; here simple)
    if not hasattr(req, 'votes'):
        req.votes = ''
    req.votes += f"{user.id}:{response},"
    db.session.commit()
    # Check consensus
    witness_ids = [int(x) for x in req.witness_ids.split(',')]
    total = len(witness_ids)
    # Count affirmative responses (we consider "accept" as yes)
    votes_list = req.votes.split(',')
    yes_count = sum(1 for vote in votes_list if vote.endswith('accept'))
    if yes_count >= 2:  # simple threshold: at least 2 out of 3
        req.status = 'verified'
        db.session.commit()
        # Optionally release funds (already done before verification in our flow)
    elif len(votes_list) >= total:  # all voted and no consensus -> flag
        req.status = 'flagged'
        db.session.commit()
    return redirect(url_for('witness_dashboard'))

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('register'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
