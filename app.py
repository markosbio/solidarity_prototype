from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from models import db, User, Transaction, Community, CommunityMembership, Provider, CareRequest, SystemState
from trust_graph import compute_draw_ceiling
from witness import select_witnesses
from recovery import update_recovery_parameters
from payments import pay_provider
from trust_engine import get_combined_score  # make sure this exists in trust_engine.py
import random
import string
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'solidarity-final-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///solidarity.db'
db.init_app(app)

# Create tables if not exist
with app.app_context():
    db.create_all()
    # Create a default global community if none exists
    if Community.query.count() == 0:
        default_comm = Community(name="Global Health Pool", invite_code="GLOBAL001", pool_balance=5000.0, admin_user_id=1)
        db.session.add(default_comm)
        db.session.commit()
    # Create a test provider if none
    if Provider.query.count() == 0:
        mulago = Provider(name="Mulago Hospital", provider_code="MULAGO001", payment_type="mpesa", payment_details="254700000", verified=True)
        db.session.add(mulago)
        db.session.commit()

# ---------------- Web Routes (existing, kept) ----------------
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
        # Auto-join default community
        default_comm = Community.query.first()
        if default_comm:
            membership = CommunityMembership(user_id=user.id, community_id=default_comm.id, role='member')
            db.session.add(membership)
            user.primary_community_id = default_comm.id
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
            round_up = 0.01
        user.sub_wallet_balance += round_up
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
        provider_id = int(request.form['provider_id'])
        is_emergency = 'is_emergency' in request.form
        community = Community.query.get(user.primary_community_id)
        if not community:
            return "You need to join a community first."
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
        # Create care request
        care_req = CareRequest(
            user_id=user.id,
            community_id=community.id,
            provider_id=provider_id,
            amount_needed=needed_amount,
            amount_from_sub=from_sub,
            amount_from_pool=from_pool,
            social_credit=social_credit,
            is_emergency=is_emergency,
            status='pending_witness'
        )
        db.session.add(care_req)
        db.session.commit()
        # Select witnesses (from same community)
        witnesses = select_witnesses(user.id, provider_id, community_id=community.id)
        witness_ids = ','.join(str(w.id) for w in witnesses)
        care_req.witness_ids = witness_ids
        db.session.commit()
        return render_template('request_result.html', needed=needed_amount, from_sub=from_sub, from_pool=from_pool, social_credit=social_credit, request_id=care_req.id)
    providers = Provider.query.filter_by(verified=True).all()
    return render_template('request_care.html', user=user, providers=providers)

@app.route('/witness_dashboard')
def witness_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    pending = []
    requests = CareRequest.query.filter_by(status='pending_witness').all()
    for req in requests:
        if req.witness_ids and str(user.id) in req.witness_ids.split(','):
            pending.append(req)
    return render_template('witness_dashboard.html', user=user, pending=pending)

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
    # Record vote
    votes = care_req.witness_votes.split(',') if care_req.witness_votes else []
    if f"{user.id}:{response}" not in votes:
        votes.append(f"{user.id}:{response}")
        care_req.witness_votes = ','.join(votes)
        db.session.commit()
    # Count approvals
    yes_count = sum(1 for v in votes if v.endswith('accept'))
    total_witnesses = len(care_req.witness_ids.split(','))
    if yes_count >= 2:  # threshold
        # Witness approved. Check if admin needed
        need_admin = (care_req.amount_needed > 50) or care_req.is_emergency
        if need_admin:
            care_req.status = 'pending_admin'
        else:
            care_req.status = 'admin_approved'  # auto-approved
            care_req.admin_approved = True
            # Pay provider directly
            pay_provider(care_req.provider_id, care_req.amount_from_pool)
        db.session.commit()
    elif len(votes) >= total_witnesses:
        # All voted but insufficient yes -> reject
        care_req.status = 'rejected'
        db.session.commit()
    return redirect(url_for('witness_dashboard'))

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('register'))

# ---------------- USSD (Africa's Talking) ----------------
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

    # Registration
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
            # Join default community (first one)
            default_comm = Community.query.first()
            if default_comm:
                membership = CommunityMembership(user_id=new_user.id, community_id=default_comm.id, role='member')
                db.session.add(membership)
                new_user.primary_community_id = default_comm.id
                db.session.commit()
            return r(f"Registered {name}. Use same number to access services.", end=True)
        return r("Invalid.", end=True)

    # Registered user
    primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=user.primary_community_id).first() if user.primary_community_id else None
    role = membership.role if membership else 'member'

    if step == 0:
        menu = f"Hi {user.name}\n1. Balance\n2. Request care\n3. Trust score\n4. Community\n5. Witness tasks\n"
        if role in ['admin', 'coadmin']:
            menu += "6. Admin panel\n"
        menu += "7. Exit"
        return r(menu)

    choice = inputs[0]

    # Balance
    if choice == "1":
        if primary_comm:
            bal = f"Wallet: ${user.sub_wallet_balance:.2f}\nPool: ${primary_comm.pool_balance:.2f}"
        else:
            bal = "Join a community first (option 4)."
        return r(bal)

    # Trust score
    if choice == "3":
        score = get_combined_score(user.id)
        return r(f"Trust score: {score:.2f}")

    # Community actions
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

    # Request care
    if choice == "2":
        if not primary_comm:
            return r("Join a community first (option 4).", end=True)
        if step == 1:
            return r("Enter amount (USD):")
        elif step == 2:
            try:
                amount = float(inputs[1])
                ussd_sessions[phone] = {"amount": amount, "state": "awaiting_provider"}
                return r("Enter provider code (e.g., MULAGO001):")
            except:
                return r("Invalid amount.", end=True)
        elif step == 3:
            provider_code = inputs[2].strip().upper()
            provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
            if not provider:
                return r("Provider not found or unverified.", end=True)
            amount = ussd_sessions.get(phone, {}).get("amount", 0)
            if amount <= 0:
                return r("Session expired.", end=True)
            ussd_sessions[phone]["provider_id"] = provider.id
            return r("Emergency? (1=Yes, 2=No)")
        elif step == 4:
            emerg = (inputs[3] == "1")
            amount = ussd_sessions[phone]["amount"]
            provider_id = ussd_sessions[phone]["provider_id"]
            provider = Provider.query.get(provider_id)
            ceiling = compute_draw_ceiling(user.id)
            from_sub = min(user.sub_wallet_balance, amount)
            remaining = amount - from_sub
            user.sub_wallet_balance -= from_sub
            from_pool = 0.0
            social_credit = 0.0
            if remaining > 0:
                allowed = min(remaining, ceiling - from_sub, primary_comm.pool_balance)
                from_pool = allowed
                primary_comm.pool_balance -= from_pool
                social_credit = remaining - from_pool
                if social_credit > 0:
                    user.total_social_credit += social_credit
                    update_recovery_parameters(user.id, social_credit)
                db.session.commit()
            # Create care request
            care_req = CareRequest(
                user_id=user.id,
                community_id=primary_comm.id,
                provider_id=provider_id,
                amount_needed=amount,
                amount_from_sub=from_sub,
                amount_from_pool=from_pool,
                social_credit=social_credit,
                is_emergency=emerg,
                status='pending_witness'
            )
            db.session.add(care_req)
            db.session.commit()
            # Select witnesses from same community
            witnesses = select_witnesses(user.id, provider_id, community_id=primary_comm.id)
            witness_ids = ','.join(str(w.id) for w in witnesses)
            care_req.witness_ids = witness_ids
            db.session.commit()
            ussd_sessions.pop(phone, None)
            need_admin = (amount > 50) or emerg
            msg = f"Request submitted. {len(witnesses)} witnesses will verify."
            if need_admin:
                msg += " Admin approval also required."
            return r(msg, end=True)

    # Witness tasks (simplified)
    if choice == "5":
        # Find pending witness requests for this user
        pending = []
        requests = CareRequest.query.filter_by(status='pending_witness').all()
        for req in requests:
            if req.witness_ids and str(user.id) in req.witness_ids.split(','):
                pending.append(req)
        if not pending:
            return r("No pending witness requests.")
        # Show first
        req = pending[0]
        ussd_sessions[phone] = {"witness_req_id": req.id}
        return r(f"Request #{req.id}: ${req.amount_needed}\n1. Accept\n2. Reject")
    # After witness choice (step 2)
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
            need_admin = (care_req.amount_needed > 50) or care_req.is_emergency
            if need_admin:
                care_req.status = 'pending_admin'
            else:
                care_req.status = 'admin_approved'
                care_req.admin_approved = True
                pay_provider(care_req.provider_id, care_req.amount_from_pool)
            db.session.commit()
        elif len(votes) >= total:
            care_req.status = 'rejected'
            db.session.commit()
        ussd_sessions.pop(phone, None)
        return r("Vote recorded. Thank you.", end=True)

    # Admin panel
    if choice == "6" and role in ['admin', 'coadmin']:
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
                prov = Provider.query.get(req.provider_id)
                return r(f"Req #{req.id}: ${req.amount_needed} at {prov.name}\n1. Approve\n2. Reject\n0. Next")
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
            # Approve/reject logic
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
                pay_provider(care_req.provider_id, care_req.amount_from_pool)
                db.session.commit()
                msg = f"Request #{req_id} approved."
            elif action == "2":
                care_req.status = 'rejected'
                db.session.commit()
                msg = f"Request #{req_id} rejected."
            else:
                # Next
                data['admin_idx'] = idx + 1
                ussd_sessions[phone] = data
                next_idx = idx + 1
                if next_idx < len(pending_ids):
                    next_req = CareRequest.query.get(pending_ids[next_idx])
                    prov = Provider.query.get(next_req.provider_id)
                    return r(f"Req #{next_req.id}: ${next_req.amount_needed} at {prov.name}\n1. Approve\n2. Reject\n0. Next")
                else:
                    return r("All requests processed.", end=True)
            return r(msg + "\nContinue? 1. Yes 2. No")
        return r("Invalid.", end=True)

    if choice == "7":
        return r("Goodbye.", end=True)

    return r("Invalid choice.", end=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
