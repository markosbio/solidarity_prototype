from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, flash
from functools import wraps
from models import (db, User, Transaction, Community, CommunityMembership, Provider,
                    CareRequest, SystemState, PaymentRecord, MpesaTopup,
                    MobileMoneyTransaction, VerifiedProvider, FraudAlert, PlatformRevenue,
                    TrustEvent, AdminAuditLog, GlobalAdmin, UserLoginHistory, ProviderCodeHistory,
                    PinResetOTP, AdminSetting, PlatformWithdrawal, ProviderWithdrawal,
                    SupportTicket, SupportMessage)
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
from notifications import (notify_ceiling_increase, notify_pool_low, notify_fraud_flagged,
                           notify_admin_care_pending, notify_admin_new_support_ticket,
                           notify_admin_dispute_filed, notify_admin_fraud_alert,
                           notify_admin_new_provider_app)
from dotenv import load_dotenv
from loguru import logger
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', os.environ.get('SECRET_KEY', 'solidarity-dev-key-change-in-production'))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///solidarity.db')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# ── Rate limiting ───────────────────────────────────────────────────────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# ── Admin access control ───────────────────────────────────────────────────────
# Phones always granted admin — auto-seeded into GlobalAdmin on first access
ADMIN_PHONES = ['0769547988']

def _ensure_global_admin(user):
    """If user's phone is in ADMIN_PHONES, create GlobalAdmin record if missing."""
    try:
        if user.phone in ADMIN_PHONES:
            existing = GlobalAdmin.query.filter_by(user_id=user.id).first()
            if not existing:
                ga = GlobalAdmin(user_id=user.id, created_by=user.id, role='super_admin')
                db.session.add(ga)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            return True
        return GlobalAdmin.query.filter_by(user_id=user.id).first() is not None
    except Exception:
        db.session.rollback()
        return user.phone in ADMIN_PHONES


def _get_current_admin_role():
    """Return role string for the logged-in admin, or None."""
    uid = session.get('user_id')
    if not uid:
        return None
    ga = GlobalAdmin.query.filter_by(user_id=uid).first()
    if ga:
        return ga.role or 'super_admin'
    user = db.session.get(User, uid)
    if user and user.phone in ADMIN_PHONES:
        return 'super_admin'
    return None


def _is_super_admin():
    return _get_current_admin_role() == 'super_admin'


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = db.session.get(User, session['user_id'])
        if not user or not _ensure_global_admin(user):
            return render_template('admin_access_denied.html',
                                   logged_in_phone=user.phone if user else None), 403
        session['admin_authed'] = True
        return f(*args, **kwargs)
    return decorated


def super_admin_required(f):
    """Decorator: require super_admin role (on top of admin_required)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_super_admin():
            flash('This action requires Super Admin role.', 'error')
            return redirect(url_for('admin_global_admins'))
        return f(*args, **kwargs)
    return decorated


def roles_required(*allowed_roles):
    """Decorator: restrict route to admins with one of the given roles.
    Must be applied AFTER @admin_required.
    Role hierarchy: super_admin always passes; then the explicit allowed list.
    Usage:
        @app.route('/admin/care')
        @admin_required
        @roles_required('super_admin', 'operator')
        def admin_care(): ...
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            role = _get_current_admin_role()
            if role not in allowed_roles:
                return render_template(
                    'admin_access_denied.html',
                    logged_in_phone=None,
                    reason=f"Your role ({role}) cannot access this area. Required: {' or '.join(allowed_roles)}."
                ), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

def _admin_redirect(url):
    """Return a 200 JS-redirect page instead of a 302.
    Prevents Replit's mTLS proxy from flashing an intermediate 'loading' page
    on POST → redirect chains in the admin panel."""
    safe = url.replace('"', '%22')
    return (
        f'<!DOCTYPE html><html><head>'
        f'<meta http-equiv="refresh" content="0;url={safe}">'
        f'<script>window.location.replace("{safe}")</script>'
        f'</head><body></body></html>'
    )


def _log_admin_action(admin_id, action, target_user_id=None, details='',
                      old_value=None, new_value=None):
    log = AdminAuditLog(
        admin_id=admin_id,
        target_user_id=target_user_id,
        action=action,
        details=details[:500],
        ip=request.remote_addr,
        old_value=old_value[:500] if old_value else None,
        new_value=new_value[:500] if new_value else None,
    )
    db.session.add(log)
    db.session.commit()

def _get_admin_phones():
    """Return a list of all admin phone numbers (GlobalAdmin table + ADMIN_PHONES fallback)."""
    try:
        phones = []
        for ga in GlobalAdmin.query.all():
            u = db.session.get(User, ga.user_id)
            if u and u.phone:
                phones.append(u.phone)
        if not phones:
            phones = list(ADMIN_PHONES)
        return phones
    except Exception:
        return list(ADMIN_PHONES)


def _total_payable(care_req) -> float:
    """Total amount the provider should receive for this care request.
    = wallet contribution + pool contribution + reserve contribution
    (social_credit is the member's debt, not paid to the provider from pool)
    """
    from_sub     = float(care_req.amount_from_sub or 0.0)
    from_pool    = float(care_req.amount_from_pool or 0.0)
    from_reserve = float(getattr(care_req, 'amount_from_reserve', 0.0) or 0.0)
    return round(from_sub + from_pool + from_reserve, 2)


def _reverse_care_request_financials(care_req) -> None:
    """Reverse all financial side-effects of a pending care request on rejection.

    Restores: user sub_wallet, community pool, global reserve, and social credit.
    Call before setting status='rejected' and before db.session.commit().
    """
    try:
        req_user = User.query.get(care_req.user_id)
        if req_user:
            sub = float(care_req.amount_from_sub or 0.0)
            if sub > 0:
                req_user.sub_wallet_balance = round(req_user.sub_wallet_balance + sub, 2)
            sc = float(care_req.social_credit or 0.0)
            if sc > 0:
                req_user.total_social_credit = round(
                    max(0.0, (req_user.total_social_credit or 0.0) - sc), 2)
        pool_amt = float(care_req.amount_from_pool or 0.0)
        if pool_amt > 0 and care_req.community_id:
            comm = Community.query.get(care_req.community_id)
            if comm and not comm.is_global_reserve:
                comm.pool_balance = round(comm.pool_balance + pool_amt, 2)
        reserve_amt = float(care_req.amount_from_reserve or 0.0)
        if reserve_amt > 0:
            reserve = Community.query.filter_by(is_global_reserve=True).first()
            if reserve:
                reserve.pool_balance = round(reserve.pool_balance + reserve_amt, 2)
    except Exception as exc:
        from loguru import logger as _log
        _log.error("Failed to reverse care request #{} financials: {}", care_req.id, exc)


def _notify_community_admins_care_pending(care_req, member_user) -> None:
    """SMS all community admins when a new care request needs their review."""
    if not care_req.community_id:
        return
    comm = Community.query.get(care_req.community_id)
    if not comm:
        return
    admin_ms = CommunityMembership.query.filter(
        CommunityMembership.community_id == comm.id,
        CommunityMembership.role.in_(['admin', 'coadmin'])
    ).all()
    phones = []
    for m in admin_ms:
        u = User.query.get(m.user_id)
        if u and u.phone:
            phones.append(u.phone)
    if phones:
        from notifications import notify_community_admin_new_request
        notify_community_admin_new_request(
            phones, member_user.name,
            float(care_req.amount_needed or 0),
            care_req.id, comm.name)


def _community_pending_count(user_id: int) -> int:
    """Count pending_community_admin care requests in communities where user is admin/coadmin."""
    try:
        admin_comm_ids = [
            m.community_id for m in
            CommunityMembership.query.filter(
                CommunityMembership.user_id == user_id,
                CommunityMembership.role.in_(['admin', 'coadmin'])
            ).all()
        ]
        if not admin_comm_ids:
            return 0
        return CareRequest.query.filter(
            CareRequest.community_id.in_(admin_comm_ids),
            CareRequest.status == 'pending_community_admin'
        ).count()
    except Exception:
        return 0


def _community_pending_by_id(user_id: int) -> dict:
    """Return {community_id: pending_count} for communities the user admins."""
    try:
        admin_memberships = CommunityMembership.query.filter(
            CommunityMembership.user_id == user_id,
            CommunityMembership.role.in_(['admin', 'coadmin'])
        ).all()
        result = {}
        for m in admin_memberships:
            count = CareRequest.query.filter_by(
                community_id=m.community_id,
                status='pending_community_admin'
            ).count()
            if count:
                result[m.community_id] = count
        return result
    except Exception:
        return {}


def _admin_pending_counts():
    """Compute badge counts for admin navigation & dashboard. Returns a dict."""
    try:
        role = _get_current_admin_role()
        if not role:
            return {}
        counts = {
            'role': role,
            'pending_care': CareRequest.query.filter_by(
                status='pending_admin', admin_approved=False).count(),
            'fraud_alerts': FraudAlert.query.filter_by(resolved=False).count(),
            'pending_providers': VerifiedProvider.query.filter_by(
                verification_status='pending').count(),
            'open_support': SupportTicket.query.filter_by(status='open').count(),
            'open_disputes': PaymentRecord.query.filter(
                PaymentRecord.dispute_status == 'open').count(),
        }
        # Role-based filtering: only show tasks relevant to each role
        if role == 'support':
            counts['pending_care'] = 0
            counts['fraud_alerts'] = 0
        elif role == 'operator':
            counts['pending_providers'] = 0
            counts['open_support'] = 0
        return counts
    except Exception:
        return {}


@app.context_processor
def inject_admin_counts():
    """Inject admin_counts into every template on admin routes."""
    from flask import request as _req
    if _req.path.startswith('/admin'):
        return {'admin_counts': _admin_pending_counts()}
    return {'admin_counts': {}}


db.init_app(app)
app.register_blueprint(communities_bp)
app.register_blueprint(providers_bp)
app.register_blueprint(ussd_bp)

# ── Security configuration ─────────────────────────────────────────────────────

WEAK_PINS = {
    '1234', '2345', '3456', '4567', '5678', '6789',
    '9876', '8765', '7654', '6543', '5432', '4321', '3210',
    '0000', '1111', '2222', '3333', '4444', '5555', '6666', '7777', '8888', '9999',
    '1212', '2121', '1313', '3131', '1122', '2211', '1100', '0011',
    '1010', '0101', '2020', '0202', '1230', '0123', '1357', '2468',
}

def _is_weak_pin(pin: str) -> bool:
    """Return True if the PIN is too predictable."""
    if pin in WEAK_PINS:
        return True
    if len(set(pin)) == 1:
        return True
    digits = [int(d) for d in pin]
    diffs = [digits[i + 1] - digits[i] for i in range(len(digits) - 1)]
    if all(d == 1 for d in diffs) or all(d == -1 for d in diffs):
        return True
    return False

SESSION_IDLE_TIMEOUT = int(os.environ.get('SESSION_IDLE_TIMEOUT', 900))
LOCKOUT_DURATION_MINUTES = int(os.environ.get('LOCKOUT_DURATION_MINUTES', 30))
_ADMIN_IP_RAW = os.environ.get('ADMIN_IP_WHITELIST', '').strip()
ADMIN_IP_WHITELIST = [ip.strip() for ip in _ADMIN_IP_RAW.split(',') if ip.strip()]

_CSRF_EXEMPT_PREFIXES = ('/mpesa/', '/ussd/', '/api/', '/static/', '/mobile-money/')


def _get_csrf_token():
    """Return (and create if missing) the per-session CSRF token."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = ''.join(
            random.choices(string.ascii_letters + string.digits, k=40)
        )
    return session['_csrf_token']


def _notify_lockout(user):
    """Alert admin phones when an account gets locked."""
    try:
        from notifications import _send_sms
        msg = (f'[SolidarityPool Alert] Account auto-locked: {user.phone} ({user.name}) '
               f'after {MAX_LOGIN_ATTEMPTS} failed PIN attempts.')
        for phone in ADMIN_PHONES:
            try:
                _send_sms(phone, msg)
            except Exception:
                pass
    except Exception:
        pass


@app.before_request
def _security_before_request():
    from loguru import logger

    # 1. Session inactivity timeout
    if 'user_id' in session:
        now_ts = datetime.utcnow().timestamp()
        last_active = session.get('_last_active')
        if last_active and (now_ts - last_active) > SESSION_IDLE_TIMEOUT:
            uid = session.get('user_id')
            session.clear()
            logger.info("Session expired by inactivity: user_id={}", uid)
            if request.path not in ('/login', '/logout'):
                flash('Your session expired due to inactivity. Please log in again.', 'info')
            return redirect(url_for('login'))
        session['_last_active'] = now_ts

    # 2. Session version check (logout-all-devices)
    if 'user_id' in session and '_session_version' in session:
        user = db.session.get(User, session['user_id'])
        if user:
            current_ver = user.session_version or 1
            if session.get('_session_version', 1) != current_ver:
                session.clear()
                return redirect(url_for('login', msg='session_revoked'))

    # 3. Admin IP whitelist
    if ADMIN_IP_WHITELIST and request.path.startswith('/admin'):
        remote_ip = request.remote_addr or ''
        if remote_ip not in ADMIN_IP_WHITELIST:
            logger.warning("Admin IP blocked: ip={} path={}", remote_ip, request.path)
            return render_template('error.html', code=403,
                                   message='Admin access denied: your IP address is not authorised.'), 403

    # 4. CSRF protection on POST (exempt webhooks)
    if request.method == 'POST':
        if not any(request.path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES):
            token_in_form = request.form.get('csrf_token', '')
            token_in_session = session.get('_csrf_token', '')
            if token_in_session and token_in_form != token_in_session:
                logger.warning("CSRF mismatch: path={} ip={}", request.path, request.remote_addr)
                return render_template('error.html', code=403,
                                       message='Security check failed. Please go back and try again.'), 403


@app.after_request
def _inject_csrf_meta(response):
    """Auto-inject CSRF token + floating chat bubble into every HTML page."""
    if response.content_type and response.content_type.startswith('text/html'):
        token = _get_csrf_token()
        data = response.get_data(as_text=True)
        # Skip injecting into the support page itself and admin support
        path = request.path
        skip_chat = path.startswith('/support') or path.startswith('/admin/support')

        csrf_snippet = (
            f'<meta name="csrf-token" content="{token}">'
            '<script>document.addEventListener("DOMContentLoaded",function(){'
            'var m=document.querySelector(\'meta[name="csrf-token"]\');'
            'if(!m)return;var v=m.content;'
            'document.querySelectorAll("form").forEach(function(f){'
            'if(f.method.toLowerCase()==="post"){'
            'if(!f.querySelector(\'input[name="csrf_token"]\')){'
            'var i=document.createElement("input");i.type="hidden";'
            'i.name="csrf_token";i.value=v;f.appendChild(i);}}'
            '});});</script>'
        )

        chat_bubble = '' if skip_chat else (
            '<style>'
            '#sp-chat-btn{position:fixed;bottom:22px;right:20px;z-index:9999;'
            'background:#2d7dd2;color:#fff;border:none;border-radius:50px;'
            'padding:12px 20px;font-size:0.92rem;font-weight:600;cursor:pointer;'
            'box-shadow:0 4px 14px rgba(45,125,210,0.45);display:flex;align-items:center;gap:8px;'
            'text-decoration:none;}'
            '#sp-chat-btn:hover{background:#2566b0;}'
            '#sp-chat-badge{background:#dc3545;color:#fff;border-radius:50%;'
            'font-size:0.7rem;font-weight:700;padding:1px 6px;margin-left:2px;}'
            '</style>'
            '<a id="sp-chat-btn" href="/support">💬 <span>Help</span></a>'
        )

        if '</head>' in data:
            data = data.replace('</head>', csrf_snippet + '</head>', 1)
        if '</body>' in data and chat_bubble:
            data = data.replace('</body>', chat_bubble + '</body>', 1)
        response.set_data(data)
    return response


# Create tables and seed default data
def _run_column_migrations():
    """Add new columns to existing tables without Alembic."""
    from loguru import logger
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS is_locked BOOLEAN DEFAULT FALSE",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS locked_reason VARCHAR(200)",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS locked_by INTEGER",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(50)",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS failed_login_count INTEGER DEFAULT 0",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS tos_accepted_at TIMESTAMP",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS preferred_language VARCHAR(5) DEFAULT 'en'",
        "ALTER TABLE global_admin ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'super_admin'",
        "UPDATE community SET pool_balance = 0.0 WHERE invite_code = 'GLOBAL001' AND pool_balance = 1000000.0",
        "ALTER TABLE transaction ADD COLUMN IF NOT EXISTS reversed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE transaction ADD COLUMN IF NOT EXISTS reversed_by INTEGER",
        "ALTER TABLE transaction ADD COLUMN IF NOT EXISTS reversed_reason VARCHAR(200)",
        "ALTER TABLE transaction ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMP",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS session_version INTEGER DEFAULT 1",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS reversed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS reversed_by INTEGER",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS reversed_reason VARCHAR(300)",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMP",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS on_hold BOOLEAN DEFAULT FALSE",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS on_hold_reason VARCHAR(200)",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS dispute_status VARCHAR(20)",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS dispute_note VARCHAR(500)",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS dispute_by_user_id INTEGER",
        "ALTER TABLE payment_record ADD COLUMN IF NOT EXISTS dispute_at TIMESTAMP",
        "ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS old_value VARCHAR(500)",
        "ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS new_value VARCHAR(500)",
        "ALTER TABLE verified_provider ADD COLUMN IF NOT EXISTS contact_person VARCHAR(150)",
        "ALTER TABLE user_login_history ADD COLUMN IF NOT EXISTS user_agent VARCHAR(300)",
        "ALTER TABLE support_ticket ADD COLUMN IF NOT EXISTS priority VARCHAR(10) DEFAULT 'medium'",
        # Architecture v2
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS lifetime_contribution_score FLOAT DEFAULT 0.0",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS net_support_balance FLOAT DEFAULT 0.0",
        "ALTER TABLE member ADD COLUMN IF NOT EXISTS primary_community_changed_at TIMESTAMP",
        "ALTER TABLE community ADD COLUMN IF NOT EXISTS is_global_reserve BOOLEAN DEFAULT FALSE",
        "ALTER TABLE community ADD COLUMN IF NOT EXISTS liquidity_health_score FLOAT DEFAULT 1.0",
        "ALTER TABLE community_membership ADD COLUMN IF NOT EXISTS leave_requested_at TIMESTAMP",
        "ALTER TABLE community_membership ADD COLUMN IF NOT EXISTS leave_status VARCHAR(20)",
        "ALTER TABLE community_membership ADD COLUMN IF NOT EXISTS leave_rejection_reason VARCHAR(300)",
        "ALTER TABLE community_membership ADD COLUMN IF NOT EXISTS leave_initiated_by VARCHAR(20)",
        "ALTER TABLE community_membership ADD COLUMN IF NOT EXISTS leave_reason TEXT",
        "ALTER TABLE care_request ADD COLUMN IF NOT EXISTS risk_tier INTEGER DEFAULT 0",
        "ALTER TABLE care_request ADD COLUMN IF NOT EXISTS amount_from_reserve FLOAT DEFAULT 0.0",
        "ALTER TABLE care_request ADD COLUMN IF NOT EXISTS invoice_description VARCHAR(500) DEFAULT ''",
        "UPDATE community SET is_global_reserve = TRUE WHERE invite_code = 'GLOBAL001'",
        """CREATE TABLE IF NOT EXISTS support_ticket (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES member(id),
            phone VARCHAR(20),
            subject VARCHAR(200) NOT NULL,
            status VARCHAR(20) DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            assigned_to INTEGER REFERENCES member(id)
        )""",
        """CREATE TABLE IF NOT EXISTS support_message (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER REFERENCES support_ticket(id) ON DELETE CASCADE,
            sender_type VARCHAR(10) NOT NULL,
            sender_id INTEGER REFERENCES member(id),
            body TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT NOW(),
            read_at TIMESTAMP
        )""",
        "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emergency_multiplier FLOAT DEFAULT 1.5",
        "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emergency_hard_cap FLOAT DEFAULT 200000.0",
        "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emg_mult_low FLOAT DEFAULT 1.2",
        "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emg_mult_high FLOAT DEFAULT 2.0",
        "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emg_tier_low_max FLOAT DEFAULT 0.4",
        "ALTER TABLE system_state ADD COLUMN IF NOT EXISTS emg_tier_high_min FLOAT DEFAULT 0.7",
    ]
    try:
        with db.engine.connect() as conn:
            for sql in migrations:
                try:
                    conn.execute(text(sql))
                except Exception as _col_e:
                    logger.debug("Column migration skipped ({}): {}", sql[:60], _col_e)
            conn.commit()
    except Exception as _e:
        logger.warning("Column migration outer error: {}", _e)

with app.app_context():
    try:
        db.create_all()
    except Exception as _e:
        from loguru import logger
        logger.warning("db.create_all() raised an error (tables may already exist): {}", _e)
        db.session.rollback()
    _run_column_migrations()
    try:
        if Community.query.count() == 0:
            default_comm = Community(name="Global Health Pool", invite_code="GLOBAL001", pool_balance=0.0, admin_user_id=None, is_global_reserve=True)
            db.session.add(default_comm)
            db.session.commit()
        else:
            # Ensure the flag is set even if seeded before the migration ran
            try:
                from sqlalchemy import text as _text
                with db.engine.connect() as _conn:
                    _conn.execute(_text("UPDATE community SET is_global_reserve = TRUE WHERE invite_code = 'GLOBAL001' AND (is_global_reserve IS NULL OR is_global_reserve = FALSE)"))
                    _conn.commit()
            except Exception:
                pass
    except Exception as _e:
        from loguru import logger
        logger.warning("Community seed skipped: {}", _e)
        db.session.rollback()
    try:
        if Provider.query.count() == 0:
            mulago = Provider(name="Mulago Hospital", provider_code="MULAGO001", payment_type="mpesa", payment_details="254700000", verified=True)
            db.session.add(mulago)
            db.session.commit()
    except Exception as _e:
        from loguru import logger
        logger.warning("Provider seed skipped: {}", _e)
        db.session.rollback()

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


def _get_emergency_multiplier(trust_score: float) -> tuple:
    """Return (multiplier, hard_cap, tier_label) based on the user's trust score.

    Three tiers (all thresholds and multipliers configurable by super admin):
      low   trust_score < tier_low_max   → emg_mult_low   (default 1.2×)
      mid   tier_low_max ≤ score < tier_high_min → emergency_multiplier (default 1.5×)
      high  trust_score ≥ tier_high_min  → emg_mult_high  (default 2.0×)
    Hard cap always applies regardless of tier.
    """
    try:
        state = SystemState.query.first()
        if state:
            cap        = float(getattr(state, 'emergency_hard_cap',  None) or 200_000.0)
            tier_low   = float(getattr(state, 'emg_tier_low_max',    None) or 0.4)
            tier_high  = float(getattr(state, 'emg_tier_high_min',   None) or 0.7)
            mult_low   = float(getattr(state, 'emg_mult_low',        None) or 1.2)
            mult_mid   = float(getattr(state, 'emergency_multiplier', None) or 1.5)
            mult_high  = float(getattr(state, 'emg_mult_high',       None) or 2.0)
            if trust_score < tier_low:
                return mult_low, cap, 'low'
            elif trust_score >= tier_high:
                return mult_high, cap, 'high'
            else:
                return mult_mid, cap, 'mid'
    except Exception:
        pass
    if trust_score < 0.4:
        return 1.2, 200_000.0, 'low'
    elif trust_score >= 0.7:
        return 2.0, 200_000.0, 'high'
    return 1.5, 200_000.0, 'mid'


def _get_all_emergency_settings() -> dict:
    """Return all emergency tier config as a dict for admin display."""
    try:
        state = SystemState.query.first()
        if state:
            return {
                'emg_mult_low':      float(getattr(state, 'emg_mult_low',        None) or 1.2),
                'emg_mult_mid':      float(getattr(state, 'emergency_multiplier', None) or 1.5),
                'emg_mult_high':     float(getattr(state, 'emg_mult_high',       None) or 2.0),
                'emg_tier_low_max':  float(getattr(state, 'emg_tier_low_max',    None) or 0.4),
                'emg_tier_high_min': float(getattr(state, 'emg_tier_high_min',   None) or 0.7),
                'emergency_hard_cap':float(getattr(state, 'emergency_hard_cap',  None) or 200_000.0),
            }
    except Exception:
        pass
    return {
        'emg_mult_low': 1.2, 'emg_mult_mid': 1.5, 'emg_mult_high': 2.0,
        'emg_tier_low_max': 0.4, 'emg_tier_high_min': 0.7, 'emergency_hard_cap': 200_000.0,
    }


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
        # Never show the global reserve pool on the personal dashboard
        if primary_comm and primary_comm.is_global_reserve:
            primary_comm = None
        # Auto-heal: if primary is None but user has a real community, auto-assign it
        if not primary_comm:
            real_ms = (CommunityMembership.query
                       .filter_by(user_id=user.id)
                       .join(Community, CommunityMembership.community_id == Community.id)
                       .filter(Community.is_global_reserve == False)
                       .first())
            if real_ms:
                user.primary_community_id = real_ms.community_id
                db.session.commit()
                primary_comm = Community.query.get(real_ms.community_id)
        membership = None
        if primary_comm:
            membership = CommunityMembership.query.filter_by(user_id=user.id, community_id=primary_comm.id).first()
        is_admin = _ensure_global_admin(user)
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
        community_pending = _community_pending_count(user.id)
        # Admin badge counts for system admin panel link
        admin_badge = 0
        if is_admin:
            try:
                ac = _admin_pending_counts()
                admin_badge = (
                    ac.get('pending_care', 0) +
                    ac.get('fraud_alerts', 0) +
                    ac.get('pending_providers', 0) +
                    ac.get('open_support', 0) +
                    ac.get('open_disputes', 0)
                )
            except Exception:
                admin_badge = 0
        # Recent approved care requests this user received
        recent_care = (
            CareRequest.query
            .filter(
                CareRequest.user_id == user.id,
                CareRequest.status.in_(['approved', 'admin_approved'])
            )
            .order_by(CareRequest.created_at.desc())
            .limit(5).all()
        )
        # Any pending requests for this user
        pending_care = (
            CareRequest.query
            .filter(
                CareRequest.user_id == user.id,
                CareRequest.status.in_(['pending_community_admin', 'pending_admin', 'pending_witness'])
            )
            .order_by(CareRequest.created_at.desc())
            .all()
        )
        # Provider invoices awaiting this member's approval
        pending_invoices = (
            CareRequest.query
            .filter(
                CareRequest.user_id == user.id,
                CareRequest.status == 'pending_patient_approval'
            )
            .order_by(CareRequest.created_at.desc())
            .all()
        )
        return render_template('dashboard.html', user=user, primary_comm=primary_comm,
                               membership=membership,
                               is_admin=is_admin, ceiling=ceiling, pool_health=ph,
                               ceiling_multiplier=ceiling_multiplier,
                               health_contributions=health_contributions,
                               community_pending=community_pending,
                               admin_badge=admin_badge,
                               recent_care=recent_care,
                               pending_care=pending_care,
                               pending_invoices=pending_invoices)
    return redirect(url_for('register'))

def _record_login(user, success=True):
    """Record a login attempt in login history and update User fields."""
    try:
        ip = request.remote_addr
        ua = request.headers.get('User-Agent', '')[:300]
        history = UserLoginHistory(user_id=user.id, ip=ip, success=success, user_agent=ua)
        db.session.add(history)
        if success:
            user.last_login_at = datetime.utcnow()
            user.last_login_ip = ip
            user.failed_login_count = 0
        else:
            user.failed_login_count = (user.failed_login_count or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()

MAX_LOGIN_ATTEMPTS = 5   # lock account after this many consecutive wrong PINs

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        step  = request.form.get('step', 'check')

        # ── Step 1: phone lookup ──────────────────────────────────────────────
        if step == 'check':
            user = User.query.filter_by(phone=phone).first()
            if user:
                # Check permanent lock first
                if getattr(user, 'is_locked', False):
                    reason = getattr(user, 'locked_reason', '') or 'Contact support.'
                    return render_template('login.html', phone=phone,
                                           error=f'Account locked. {reason}')
                # Check timed lockout
                locked_until = getattr(user, 'locked_until', None)
                if locked_until and locked_until > datetime.utcnow():
                    remaining = max(1, int((locked_until - datetime.utcnow()).total_seconds() / 60) + 1)
                    return render_template('login.html', phone=phone,
                                           error=f'Account temporarily locked. Try again in {remaining} minute{"s" if remaining != 1 else ""}.')
                elif locked_until and locked_until <= datetime.utcnow():
                    user.locked_until = None
                    user.failed_login_count = 0
                    db.session.commit()
                # Existing user — prompt for PIN
                return render_template('login.html', phone=phone, show_pin=True)
            # New number — show registration form
            return render_template('login.html', phone=phone, show_register=True)

        # ── Step 2: PIN verification ──────────────────────────────────────────
        elif step == 'pin':
            pin  = request.form.get('pin', '').strip()
            user = User.query.filter_by(phone=phone).first()
            if not user:
                return render_template('login.html', phone=phone,
                                       error='Phone number not found. Please try again.')
            if getattr(user, 'is_locked', False):
                reason = getattr(user, 'locked_reason', '') or 'Contact support.'
                return render_template('login.html', phone=phone,
                                       error=f'Account locked. {reason}')
            # Check timed lockout
            locked_until = getattr(user, 'locked_until', None)
            if locked_until and locked_until > datetime.utcnow():
                remaining = max(1, int((locked_until - datetime.utcnow()).total_seconds() / 60) + 1)
                return render_template('login.html', phone=phone,
                                       error=f'Account temporarily locked. Try again in {remaining} minute{"s" if remaining != 1 else ""}.')
            elif locked_until and locked_until <= datetime.utcnow():
                user.locked_until = None
                user.failed_login_count = 0
                db.session.commit()
            failed = getattr(user, 'failed_login_count', 0) or 0
            # Verify PIN
            if pin != (user.pin or ''):
                _record_login(user, success=False)
                remaining_attempts = max(0, MAX_LOGIN_ATTEMPTS - (failed + 1))
                if remaining_attempts == 0:
                    user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
                    user.failed_login_count = 0
                    db.session.commit()
                    _notify_lockout(user)
                    return render_template('login.html', phone=phone,
                                           error=f'Account locked for {LOCKOUT_DURATION_MINUTES} minutes after too many failed attempts.')
                return render_template('login.html', phone=phone, show_pin=True,
                                       error=f'Incorrect PIN. {remaining_attempts} attempt{"s" if remaining_attempts != 1 else ""} remaining.')
            # Correct PIN — log in
            _record_login(user, success=True)
            session.permanent = True
            session['user_id'] = user.id
            session['_session_version'] = user.session_version or 1
            next_url = request.args.get('next') or url_for('home')
            return redirect(next_url)

        # ── Step 3: new member registration ──────────────────────────────────
        elif step == 'register':
            # Phone is now editable on the registration form — re-read it
            phone       = request.form.get('phone', phone).strip()
            name        = request.form.get('name', '').strip()
            pin         = request.form.get('pin', '').strip()
            confirm_pin = request.form.get('confirm_pin', '').strip()
            referred_by = request.form.get('referred_by', '').strip()
            tos_accept  = request.form.get('tos_accept', '')

            if not phone or not phone.replace('+', '').isdigit():
                return render_template('login.html', phone=phone, show_register=True,
                                       error='Please enter a valid phone number.')

            existing = User.query.filter_by(phone=phone).first()
            if existing:
                # Race condition — already registered; ask for PIN
                return render_template('login.html', phone=phone, show_pin=True,
                                       error='This number is already registered. Please enter your PIN.')
            if not name:
                return render_template('login.html', phone=phone, show_register=True,
                                       error='Please enter your full name.')
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                return render_template('login.html', phone=phone, show_register=True,
                                       error='PIN must be 4–6 digits.')
            if _is_weak_pin(pin):
                return render_template('login.html', phone=phone, show_register=True,
                                       error='That PIN is too easy to guess. Please choose a less predictable PIN.')
            if pin != confirm_pin:
                return render_template('login.html', phone=phone, show_register=True,
                                       error='PINs do not match. Please try again.')
            if not tos_accept:
                return render_template('login.html', phone=phone, show_register=True,
                                       error='You must accept the Terms of Service to register.')

            user = User(phone=phone, name=name, pin=pin,
                        sub_wallet_balance=0.0, trust_score=0.5)
            if referred_by:
                referrer = User.query.filter_by(phone=referred_by).first()
                if referrer:
                    user.referred_by = referrer.id
            db.session.add(user)
            db.session.commit()
            # New users start with no community — they join voluntarily via invite code

            # No auto-login — redirect to login with success message
            logger.info("New member registered: phone={} name={}", phone, name)
            return redirect(url_for('login', phone=phone, registered='1'))

    # GET
    prefill_phone = request.args.get('phone', '')
    registered    = request.args.get('registered', '')
    return render_template(
        'login.html',
        phone=prefill_phone,
        success='Account created! Please log in with your phone number and PIN.' if registered else None,
    )


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Legacy register route — redirects into the unified login flow."""
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        name = request.form.get('name', '').strip()
        referred_by = request.form.get('referred_by', '').strip()
        pin = request.form.get('pin', '1234').strip()
        confirm_pin = request.form.get('confirm_pin', '').strip()
        existing = User.query.filter_by(phone=phone).first()
        if existing:
            # Already registered — send to login PIN step
            return redirect(url_for('login', phone=phone))
        if not pin.isdigit() or not (4 <= len(pin) <= 6):
            return render_template('register.html', error='PIN must be 4–6 digits.')
        if _is_weak_pin(pin):
            return render_template('register.html', error='That PIN is too easy to guess. Please choose a less predictable one.')
        if pin != confirm_pin:
            return render_template('register.html', error='PINs do not match. Please try again.')
        tos_accept = request.form.get('tos_accept', '')
        if not tos_accept:
            return render_template('register.html', error='You must accept the Terms of Service to register.')
        user = User(phone=phone, name=name, pin=pin, sub_wallet_balance=0.0, trust_score=0.5,
                    tos_accepted_at=datetime.utcnow())
        if referred_by:
            referrer = User.query.filter_by(phone=referred_by).first()
            if referrer:
                user.referred_by = referrer.id
        db.session.add(user)
        db.session.commit()
        # New users start with no community — they join voluntarily via invite code
        # No auto-login — redirect to login with success banner
        logger.info("New member registered via /register: phone={} name={}", phone, name)
        return redirect(url_for('login', phone=phone, registered='1'))
    prefill = request.args.get('phone', '')
    return render_template('register.html', prefill_phone=prefill)

@app.route('/register_provider', methods=['POST'])
def register_provider():
    return redirect(url_for('apply_provider'))

# ── Public provider application page ──────────────────────────────────────────

@app.route('/apply-provider', methods=['GET', 'POST'])
def apply_provider():
    if request.method == 'POST':
        provider_name    = request.form.get('provider_name', '').strip()
        contact_person   = request.form.get('contact_person', '').strip()
        phone            = request.form.get('phone', '').strip()
        provider_wallet_number = request.form.get('provider_wallet_number', '').strip()
        business_license = request.form.get('business_license', '').strip()
        location         = request.form.get('location', '').strip()
        payment_type     = request.form.get('payment_type', '').strip()
        notes            = request.form.get('notes', '').strip()

        if not all([provider_name, contact_person, phone, provider_wallet_number, payment_type, business_license, location]):
            form = request.form
            return render_template('apply_provider.html', submitted=False,
                                   error='All required fields must be filled in.', form=form)

        tos_accept = request.form.get('tos_accept', '')
        if not tos_accept:
            form = request.form
            return render_template('apply_provider.html', submitted=False,
                                   error='You must accept the Terms of Service to apply.', form=form)

        vp = VerifiedProvider(
            provider_name=provider_name,
            contact_person=contact_person,
            phone=phone,
            provider_wallet_number=provider_wallet_number,
            business_license=business_license,
            location=location,
            verification_status='pending',
            review_notes=f'Payment: {payment_type}. Notes: {notes}' if notes else f'Payment: {payment_type}',
        )
        db.session.add(vp)
        db.session.commit()
        try:
            from notifications import notify_new_provider_application
            for ga in GlobalAdmin.query.all():
                ga_user = User.query.get(ga.user_id)
                if ga_user:
                    notify_new_provider_application(ga_user.phone, provider_name, phone)
        except Exception:
            pass
        return render_template('apply_provider.html', submitted=True,
                               submitted_name=provider_name, submitted_phone=phone, form={})

    return render_template('apply_provider.html', submitted=False, error=None, form={})

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
    pending_care = []
    if membership.role in ('admin', 'coadmin'):
        pending_care = (CareRequest.query
                        .filter_by(community_id=comm_id, status='pending_community_admin')
                        .order_by(CareRequest.id.desc()).all())
        for req in pending_care:
            req.member = User.query.get(req.user_id)
            req.provider = VerifiedProvider.query.get(req.provider_id) if req.provider_id else None
    return render_template('community_dashboard.html',
                           community=community,
                           members=members,
                           user_role=membership.role,
                           current_user_id=user.id,
                           pending_care=pending_care)


@app.route('/community/<int:comm_id>/care-action/<int:request_id>/<action>', methods=['POST'])
def community_care_action(comm_id, request_id, action):
    """Community admin approve or reject a pending_community_admin care request."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    membership = CommunityMembership.query.filter_by(
        user_id=user.id, community_id=comm_id).first()
    if not membership or membership.role not in ('admin', 'coadmin'):
        flash('Not authorised.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    care_req = CareRequest.query.get(request_id)
    if not care_req or (care_req.community_id != comm_id and care_req.community_id is not None):
        flash('Request not found.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    if care_req.status != 'pending_community_admin':
        flash('This request is no longer awaiting community review.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    if action == 'approve':
        care_req.status = 'admin_approved'
        care_req.admin_approved = True
        care_req.admin_id = user.id
        # Update net_support_balance
        _cu = User.query.get(care_req.user_id)
        if _cu:
            _cu.net_support_balance = round(
                (_cu.net_support_balance or 0.0) - float(care_req.amount_needed or 0), 2)
        db.session.commit()
        try:
            ok, ref = pay_provider(
                care_request_id=care_req.id,
                amount=float(care_req.amount_needed or 0),
                provider_id=care_req.provider_id,
                user_id=care_req.user_id,
                community_id=care_req.community_id or comm_id)
            if ok:
                care_req.payment_transaction_id = ref
                db.session.commit()
        except Exception:
            pass
        try:
            from notifications import notify_member_care_approved
            _cu2 = User.query.get(care_req.user_id)
            _prov = VerifiedProvider.query.get(care_req.provider_id) if care_req.provider_id else None
            if _cu2:
                notify_member_care_approved(
                    _cu2, float(care_req.amount_needed or 0),
                    _prov.name if _prov else 'your provider')
        except Exception:
            pass
        _log_admin_action(user.id, 'community_care_approve',
                          details=f'community={comm_id} request=#{request_id}')
        flash(f'Request #{request_id} approved — payment triggered.', 'success')
    elif action == 'reject':
        _reverse_care_request_financials(care_req)
        care_req.status = 'rejected'
        care_req.admin_id = user.id
        db.session.commit()
        try:
            from notifications import notify_member_care_rejected
            _cu3 = User.query.get(care_req.user_id)
            if _cu3:
                notify_member_care_rejected(_cu3)
        except Exception:
            pass
        _log_admin_action(user.id, 'community_care_reject',
                          details=f'community={comm_id} request=#{request_id}')
        flash(f'Request #{request_id} rejected — funds returned to member.', 'success')
    else:
        flash('Invalid action.', 'error')
    return redirect(url_for('community_dashboard', comm_id=comm_id))


@app.route('/community/<int:comm_id>/promote/<int:member_id>', methods=['POST'])
def community_promote(comm_id, member_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    admin_ms = CommunityMembership.query.filter_by(
        user_id=session['user_id'], community_id=comm_id).first()
    if not admin_ms or admin_ms.role != 'admin':
        return "Access denied — community admin only.", 403
    target_ms = CommunityMembership.query.filter_by(
        user_id=member_id, community_id=comm_id).first()
    if not target_ms:
        return "Member not found in this community.", 404
    target_ms.role = 'coadmin'
    db.session.commit()
    _log_admin_action(session['user_id'], 'promote_coadmin', target_user_id=member_id,
                      details=f'Promoted user {member_id} to coadmin in community {comm_id}')
    return redirect(url_for('community_dashboard', comm_id=comm_id))


@app.route('/community/<int:comm_id>/demote/<int:member_id>', methods=['POST'])
def community_demote(comm_id, member_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    admin_ms = CommunityMembership.query.filter_by(
        user_id=session['user_id'], community_id=comm_id).first()
    if not admin_ms or admin_ms.role != 'admin':
        return "Access denied — community admin only.", 403
    target_ms = CommunityMembership.query.filter_by(
        user_id=member_id, community_id=comm_id).first()
    if not target_ms:
        return "Member not found in this community.", 404
    if target_ms.role == 'admin':
        return "Cannot demote the community owner.", 403
    target_ms.role = 'member'
    db.session.commit()
    _log_admin_action(session['user_id'], 'demote_coadmin', target_user_id=member_id,
                      details=f'Demoted user {member_id} to member in community {comm_id}')
    return redirect(url_for('community_dashboard', comm_id=comm_id))


@app.route('/simulate_roundup')
def simulate_roundup():
    return redirect(url_for('home'))


@app.route('/admin/simulate-contribution', methods=['GET', 'POST'])
@admin_required
def admin_simulate_contribution():
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
        # pool shares already distributed to all communities by process_fee_contribution
        primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
        if primary_comm:
            enforce_pool_health(primary_comm)
            ph = _pool_health(primary_comm.pool_balance)
            notify_pool_low(primary_comm, ph['pct'])
        db.session.commit()
        try:
            new_ceiling = compute_draw_ceiling(user.id)
            notify_ceiling_increase(user, new_ceiling, old_ceiling)
        except Exception:
            pass
        flash(f'Contribution of UGX {solidarity_amount:,.0f} recorded successfully.', 'success')
        return redirect(url_for('admin_simulate_contribution'))
    return render_template('simulate_roundup.html', user=user, mpesa_enabled=mpesa_enabled,
                           solidarity_pct=solidarity_pct, wallet_pct=wallet_pct)


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

    # All providers shown; verified flag affects tier logic
    providers = Provider.query.all()
    primary_community = (Community.query.get(user.primary_community_id)
                         if user.primary_community_id else None)
    global_reserve = Community.query.filter_by(is_global_reserve=True).first()
    try:
        ceiling = round(compute_draw_ceiling(user.id), 2)
    except Exception:
        ceiling = 0.0

    emg_mult, emg_cap, emg_tier = _get_emergency_multiplier(user.trust_score)
    emergency_ceiling = round(min(ceiling * emg_mult, emg_cap), 2)

    if request.method == 'POST':
        needed_amount = float(request.form['needed_amount'])
        provider_id = int(request.form['provider_id'])
        wallet_only = 'wallet_only' in request.form
        is_emergency = 'is_emergency' in request.form
        provider = Provider.query.get(provider_id)

        # Re-compute with POST-time trust score (same user, consistent)
        emg_mult, emg_cap, emg_tier = _get_emergency_multiplier(user.trust_score)
        emergency_ceiling = round(min(ceiling * emg_mult, emg_cap), 2)

        # ── Ceiling validation (non-wallet requests) ───────────────────────────
        if not wallet_only:
            if is_emergency:
                if needed_amount > emergency_ceiling:
                    return render_template(
                        'request_care.html', user=user, providers=providers,
                        primary_community=primary_community, ceiling=ceiling,
                        emergency_ceiling=emergency_ceiling, emg_tier=emg_tier,
                        emg_mult=emg_mult,
                        error=f'Amount exceeds your emergency ceiling of UGX {emergency_ceiling:,.0f}. '
                              'Please reduce the amount or contact support.')
            else:
                if needed_amount > ceiling:
                    return render_template(
                        'request_care.html', user=user, providers=providers,
                        primary_community=primary_community, ceiling=ceiling,
                        emergency_ceiling=emergency_ceiling, emg_tier=emg_tier,
                        emg_mult=emg_mult,
                        error='Your requested amount exceeds your current drawing ceiling. '
                              'Please reduce the amount or submit an Exceptional Assistance Request.')

        # ── Wallet-only path — instant, no approval ────────────────────────────
        if wallet_only:
            if user.sub_wallet_balance < needed_amount:
                return render_template(
                    'request_care.html', user=user, providers=providers,
                    primary_community=primary_community, ceiling=ceiling,
                    error=(f'Insufficient wallet balance '
                           f'(UGX {user.sub_wallet_balance:,.0f} available). '
                           f'Enter a smaller amount or uncheck wallet-only to draw from the pool.'))
            user.sub_wallet_balance -= needed_amount
            care_req = CareRequest(
                user_id=user.id,
                community_id=primary_community.id if primary_community else None,
                provider_id=provider_id,
                amount_needed=needed_amount, amount_from_sub=needed_amount,
                amount_from_pool=0.0, social_credit=0.0,
                is_emergency=False, status='approved', risk_tier=1,
            )
            db.session.add(care_req)
            user.net_support_balance = round((user.net_support_balance or 0.0) - needed_amount, 2)
            db.session.commit()
            try:
                pay_provider(care_req.id)
            except Exception:
                pass
            return render_template(
                'request_result.html',
                needed=needed_amount, from_sub=needed_amount, from_pool=0.0,
                from_reserve=0.0, social_credit=0.0, request_id=care_req.id,
                ceiling_remaining=None, wallet_only=True, risk_tier=1)

        # ── Pool path: auto-fund wallet → primary community → global reserve ────

        # Step 1: Wallet
        from_sub = round(min(user.sub_wallet_balance, needed_amount), 2)
        remaining = round(needed_amount - from_sub, 2)
        user.sub_wallet_balance -= from_sub

        # Step 2: Primary community pool
        from_pool = 0.0
        if remaining > 0 and primary_community:
            if is_large_withdrawal_blocked(primary_community, remaining):
                return render_template(
                    'request_care.html', user=user, providers=providers,
                    primary_community=primary_community, ceiling=ceiling,
                    error='Large withdrawals are temporarily paused to protect the pool. Try a smaller amount.')
            available = min(remaining, max(0.0, ceiling - from_sub), primary_community.pool_balance)
            from_pool = round(available, 2)
            primary_community.pool_balance -= from_pool
            remaining = round(remaining - from_pool, 2)

        # Step 3: Global reserve
        from_reserve = 0.0
        if remaining > 0 and global_reserve:
            available_reserve = min(remaining, global_reserve.pool_balance)
            from_reserve = round(available_reserve, 2)
            global_reserve.pool_balance -= from_reserve
            remaining = round(remaining - from_reserve, 2)

        # Any leftover → social credit
        social_credit = round(remaining, 2)
        if social_credit > 0:
            user.total_social_credit = round((user.total_social_credit or 0.0) + social_credit, 2)
            update_recovery_parameters(user.id, social_credit)

        care_req = CareRequest(
            user_id=user.id,
            community_id=primary_community.id if primary_community else (
                global_reserve.id if global_reserve else None),
            provider_id=provider_id,
            amount_needed=needed_amount, amount_from_sub=from_sub,
            amount_from_pool=from_pool, social_credit=social_credit,
            is_emergency=is_emergency, status='pending_witness',
        )
        if hasattr(care_req, 'amount_from_reserve'):
            care_req.amount_from_reserve = from_reserve
        db.session.add(care_req)
        db.session.commit()

        # ── Fraud scoring ──────────────────────────────────────────────────────
        fraud_score = 0.0
        try:
            fraud_score, fraud_reasons = calculate_fraud_risk(user.id, care_req.id)
            care_req.fraud_score = fraud_score
            if is_fraud_flagged(fraud_score):
                care_req.fraud_flagged = True
                care_req.fraud_reasons = '; '.join(fraud_reasons)
                log_fraud_alert(user.id, care_req.id, fraud_score, fraud_reasons)
                try:
                    notify_admin_fraud_alert(_get_admin_phones(), user.name, fraud_score, care_req.id)
                except Exception:
                    pass
        except Exception:
            pass

        # ── Risk-tier assignment (replaces witness gate) ───────────────────────
        # Tier 1: small + trusted + verified provider + clean fraud score → auto-approve
        # Tier 2: medium or moderate risk → community admin review
        # Tier 3: large / high fraud / unverified on large amount → global admin
        is_verified_provider = bool(provider and provider.verified)
        if (needed_amount <= 50_000
                and user.trust_score >= 0.6
                and is_verified_provider
                and fraud_score < 0.3
                and not care_req.fraud_flagged):
            tier = 1
        elif care_req.fraud_flagged:
            tier = 3  # fraud → system admin only
        else:
            tier = 2  # all others (including large/emergency) → community admin

        care_req.risk_tier = tier

        if tier == 1:
            care_req.status = 'approved'
            care_req.admin_approved = True
            user.net_support_balance = round((user.net_support_balance or 0.0) - needed_amount, 2)
            db.session.commit()
            try:
                ok, ref = pay_provider(
                    care_request_id=care_req.id, amount=needed_amount,
                    provider_id=provider_id, user_id=user.id,
                    community_id=care_req.community_id,
                )
                if ok:
                    care_req.payment_transaction_id = ref
                    db.session.commit()
            except Exception:
                pass

        elif tier == 2:
            care_req.status = 'pending_community_admin'
            db.session.commit()
            try:
                _notify_community_admins_care_pending(care_req, user)
            except Exception:
                pass

        else:
            care_req.status = 'pending_admin'
            db.session.commit()
            try:
                notify_admin_care_pending(
                    _get_admin_phones(), user.name,
                    float(care_req.amount_needed or 0), care_req.id)
            except Exception:
                pass

        # Update liquidity health for primary community
        if primary_community:
            try:
                enforce_pool_health(primary_community)
                total_contributed = (
                    db.session.query(db.func.sum(Transaction.amount))
                    .filter_by(type='solidarity_pool')
                    .scalar() or 1.0
                )
                total_disbursed = (
                    db.session.query(db.func.sum(CareRequest.amount_from_pool))
                    .filter_by(community_id=primary_community.id)
                    .filter(CareRequest.status.in_(['approved', 'admin_approved']))
                    .scalar() or 0.0
                )
                primary_community.liquidity_health_score = round(
                    float(primary_community.pool_balance or 0) / max(float(total_disbursed or 1), 1.0), 4
                )
                db.session.commit()
            except Exception:
                pass

        try:
            ceiling_remaining = max(0.0, round(ceiling - from_pool - from_reserve, 2))
        except Exception:
            ceiling_remaining = 0.0

        return render_template(
            'request_result.html',
            needed=needed_amount, from_sub=from_sub, from_pool=from_pool,
            from_reserve=from_reserve, social_credit=social_credit,
            request_id=care_req.id, ceiling_remaining=ceiling_remaining,
            wallet_only=False, risk_tier=tier, status=care_req.status)

    return render_template('request_care.html', user=user, providers=providers,
                           primary_community=primary_community, ceiling=ceiling,
                           emergency_ceiling=emergency_ceiling,
                           emg_tier=emg_tier, emg_mult=emg_mult)

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
        # Only route to system admin if fraud-flagged; all others go to community admin
        need_system_admin = bool(care_req.fraud_flagged)
        if need_system_admin:
            care_req.status = 'pending_admin'
            try:
                _care_user = db.session.get(User, care_req.user_id)
                notify_admin_care_pending(
                    _get_admin_phones(),
                    _care_user.name if _care_user else 'Unknown',
                    float(care_req.amount_needed or 0),
                    care_req.id,
                )
            except Exception:
                pass
        else:
            care_req.status = 'pending_community_admin'
            try:
                _notify_community_admins_care_pending(care_req, db.session.get(User, care_req.user_id))
            except Exception:
                pass
        db.session.commit()
    elif len(votes) >= total_witnesses:
        _reverse_care_request_financials(care_req)
        care_req.status = 'rejected'
        db.session.commit()
    return redirect(url_for('witness_dashboard'))

@app.route('/trust_history')
def trust_history():
    if 'user_id' not in session:
        return redirect(url_for('register'))
    if not session.get('pin_verified'):
        return redirect(url_for('verify_pin', next=url_for('trust_history')))
    from models import TrustEvent
    user = User.query.get(session['user_id'])
    events = TrustEvent.query.filter_by(user_id=user.id).order_by(TrustEvent.timestamp.desc()).limit(50).all()
    return render_template('trust_history.html', user=user, events=events)

@app.route('/admin/care')
@admin_required
@roles_required('super_admin', 'operator')
def admin_care():
    user = User.query.get(session['user_id'])
    _check_emergency_auto_approvals()
    pending = CareRequest.query.filter(
        CareRequest.status.in_(['pending_admin', 'pending_community_admin']),
        CareRequest.admin_approved == False
    ).all()
    for cr in pending:
        cr.requester = User.query.get(cr.user_id)
    solidarity_pct = _get_solidarity_percent()
    fraud_count = FraudAlert.query.filter_by(resolved=False).count()
    # Platform health stats (simple queries to avoid legacy API issues)
    try:
        total_members = User.query.count()
    except Exception:
        total_members = 0
    try:
        total_pool = sum(float(c.pool_balance or 0) for c in Community.query.all())
    except Exception:
        total_pool = 0.0
    try:
        total_disbursed = sum(
            float(cr.amount_requested or 0)
            for cr in CareRequest.query.filter_by(status='approved').all()
        )
    except Exception:
        total_disbursed = 0.0
    pending_count = len(pending)
    return render_template('admin_care.html', user=user, pending=pending,
                           solidarity_pct=solidarity_pct, fraud_count=fraud_count,
                           total_members=total_members, total_pool=total_pool,
                           total_disbursed=total_disbursed, pending_count=pending_count)


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
@roles_required('super_admin', 'operator')
def admin_verified_providers():
    applications = VerifiedProvider.query.order_by(VerifiedProvider.created_at.desc()).all()
    # Build lookup maps for attaching Provider records
    providers_by_name  = {p.name.strip().lower(): p for p in Provider.query.all()}
    providers_by_phone = {p.contact_phone: p for p in Provider.query.all() if p.contact_phone}
    for app_ in applications:
        if app_.reviewed_by:
            app_.resolver = User.query.get(app_.reviewed_by)
        else:
            app_.resolver = None
        # Try to link to a Provider record (for code management)
        app_.provider_ref = (
            providers_by_name.get(app_.provider_name.strip().lower()) or
            providers_by_phone.get(app_.phone)
        )
    return render_template('admin_verified_providers.html', applications=applications)


@app.route('/admin/verified-providers/apply', methods=['POST'])
@admin_required
def apply_verified_provider():
    vp = VerifiedProvider(
        provider_name=request.form.get('provider_name', '').strip(),
        contact_person=request.form.get('contact_person', '').strip() or None,
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
    from loguru import logger
    from notifications import notify_provider_approved, notify_provider_rejected
    vp = VerifiedProvider.query.get_or_404(app_id)
    action = request.form.get('action')
    notes = request.form.get('notes', '').strip()

    if action == 'verify':
        vp.verification_status = 'verified'
        vp.review_notes = notes
        vp.reviewed_at = datetime.utcnow()
        vp.reviewed_by = session['user_id']

        # Always create a NEW Provider record per individual so codes are unique.
        # Use the facility name as the prefix for sequential numbering so that
        # multiple people from the same clinic get e.g. MUTUNGOC001, MUTUNGOC002, …
        import re
        prefix = re.sub(r'[^A-Z0-9]', '', vp.provider_name.upper())[:8] or 'CLINIC'
        new_code = _generate_sequential_code(prefix)
        individual_name = getattr(vp, 'contact_person', None) or vp.provider_name
        provider = Provider(
            name=vp.provider_name,
            provider_code=new_code,
            payment_type='mobile_money',
            payment_details=vp.provider_wallet_number or '',
            verified=True,
            contact_name=individual_name,
            contact_phone=vp.phone,
        )
        db.session.add(provider)

        db.session.commit()
        logger.info("Provider application approved: vp_id={} individual={} code={}",
                    vp.id, individual_name, provider.provider_code)
        notify_provider_approved(vp.phone, vp.provider_name, provider.provider_code)

    elif action == 'reject':
        vp.verification_status = 'rejected'
        vp.review_notes = notes
        vp.reviewed_at = datetime.utcnow()
        vp.reviewed_by = session['user_id']
        db.session.commit()
        logger.info("Provider application rejected: vp_id={} reason={}", vp.id, notes)
        notify_provider_rejected(vp.phone, vp.provider_name, notes)

    return redirect(url_for('admin_verified_providers'))


@app.route('/admin/fraud-alerts')
@admin_required
@roles_required('super_admin', 'operator')
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
    _log_admin_action(session['user_id'], 'fraud_alert_resolved',
                      target_user_id=alert.user_id,
                      details=f'Fraud alert #{alert_id} resolved',
                      old_value='open', new_value='resolved')
    return redirect(url_for('admin_fraud_alerts'))

@app.route('/admin/leave-requests')
@admin_required
@roles_required('super_admin', 'operator')
def admin_leave_requests():
    user = User.query.get(session['user_id'])
    from models import CommunityMembership
    # System admins only see: members WITH outstanding credit, OR admin-initiated removals where member has debt
    all_pending = (CommunityMembership.query
                   .filter(CommunityMembership.leave_requested_at.isnot(None),
                           CommunityMembership.leave_status == 'pending')
                   .order_by(CommunityMembership.leave_requested_at.asc())
                   .all())
    pending = [m for m in all_pending
               if m.user and m.user.total_social_credit > 0]
    return render_template('admin_leave_requests.html', user=user, pending=pending)


@app.route('/community/<int:comm_id>/leave-action/<int:membership_id>', methods=['POST'])
def community_leave_action(comm_id, membership_id):
    """Community admin approves or denies a debt-free member leave request."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    admin_ms = CommunityMembership.query.filter_by(
        user_id=session['user_id'], community_id=comm_id
    ).first()
    if not admin_ms or admin_ms.role not in ('admin', 'coadmin'):
        flash('Access denied — community admin only.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    mem = CommunityMembership.query.get_or_404(membership_id)
    if mem.community_id != comm_id:
        abort(404)
    member = User.query.get(mem.user_id)
    community = Community.query.get(comm_id)
    action = request.form.get('action')
    reason = request.form.get('reason', '').strip()
    if member and member.total_social_credit > 0:
        flash('This member has outstanding credit — their request is handled by system admins.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    if action == 'approve':
        if member and member.primary_community_id == comm_id:
            other = (CommunityMembership.query
                     .filter(CommunityMembership.user_id == member.id,
                             CommunityMembership.community_id != comm_id,
                             CommunityMembership.id != mem.id)
                     .join(Community, CommunityMembership.community_id == Community.id)
                     .filter(Community.is_global_reserve == False)
                     .first())
            member.primary_community_id = other.community_id if other else None
            member.primary_community_changed_at = datetime.utcnow()
        comm_name = community.name if community else '?'
        member_name = member.name if member else 'unknown'
        db.session.delete(mem)
        db.session.flush()
        try:
            compute_draw_ceiling(member.id)
        except Exception:
            pass
        _log_admin_action(session['user_id'], 'community_leave_approved',
                          target_user_id=member.id if member else None,
                          details=f'Community admin approved leave from {comm_name}')
        db.session.commit()
        flash(f'{member_name} has been removed from {comm_name}.', 'success')
    elif action == 'deny':
        mem.leave_status = 'rejected'
        mem.leave_requested_at = None
        mem.leave_rejection_reason = reason or 'Request denied by community admin.'
        _log_admin_action(session['user_id'], 'community_leave_denied',
                          target_user_id=mem.user_id,
                          details=f'Community admin denied leave from {community.name if community else "?"}')
        db.session.commit()
        flash('Leave request denied.', 'success')
    return redirect(url_for('community_dashboard', comm_id=comm_id))


@app.route('/community/<int:comm_id>/remove-member/<int:member_id>', methods=['POST'])
def community_remove_member(comm_id, member_id):
    """Community admin requests removal of a member with a reason."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    admin_ms = CommunityMembership.query.filter_by(
        user_id=session['user_id'], community_id=comm_id
    ).first()
    if not admin_ms or admin_ms.role not in ('admin', 'coadmin'):
        flash('Access denied — community admin only.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    mem = CommunityMembership.query.filter_by(user_id=member_id, community_id=comm_id).first()
    if not mem:
        flash('Member not found in this community.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    member = User.query.get(member_id)
    community = Community.query.get(comm_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('A reason is required to request removal.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    if mem.leave_status == 'pending':
        flash('A leave/removal request is already pending for this member.', 'error')
        return redirect(url_for('community_dashboard', comm_id=comm_id))
    has_debt = member and member.total_social_credit > 0
    mem.leave_requested_at = datetime.utcnow()
    mem.leave_status = 'pending'
    mem.leave_initiated_by = 'admin'
    mem.leave_reason = reason
    mem.leave_rejection_reason = None
    if not has_debt:
        # No debt — community admin removes directly
        if member and member.primary_community_id == comm_id:
            other = (CommunityMembership.query
                     .filter(CommunityMembership.user_id == member.id,
                             CommunityMembership.community_id != comm_id,
                             CommunityMembership.id != mem.id)
                     .join(Community, CommunityMembership.community_id == Community.id)
                     .filter(Community.is_global_reserve == False)
                     .first())
            member.primary_community_id = other.community_id if other else None
            member.primary_community_changed_at = datetime.utcnow()
        db.session.delete(mem)
        db.session.flush()
        try:
            compute_draw_ceiling(member_id)
        except Exception:
            pass
        _log_admin_action(session['user_id'], 'community_member_removed',
                          target_user_id=member_id,
                          details=f'Removed from community #{comm_id} ({community.name if community else "?"}). Reason: {reason}')
        db.session.commit()
        flash(f'{member.name if member else "Member"} has been removed from the community. Reason: {reason}', 'success')
    else:
        # Has debt — escalate to system admin for review
        db.session.commit()
        _log_admin_action(session['user_id'], 'community_removal_requested',
                          target_user_id=member_id,
                          details=f'Community admin requested removal from #{comm_id} ({community.name if community else "?"}). Reason: {reason}. Escalated to system admin (member has debt).')
        flash(f'Removal request submitted for {member.name if member else "member"} — escalated to system review because this member has outstanding credit.', 'success')
    return redirect(url_for('community_dashboard', comm_id=comm_id))


@app.route('/admin/leave-requests/<int:membership_id>', methods=['POST'])
@admin_required
@roles_required('super_admin', 'operator')
def admin_leave_action(membership_id):
    from models import CommunityMembership
    admin_user = User.query.get(session['user_id'])
    mem = CommunityMembership.query.get_or_404(membership_id)
    action = request.form.get('action')
    member = User.query.get(mem.user_id)
    community = Community.query.get(mem.community_id)
    reason = request.form.get('reason', '').strip()
    if action == 'approve':
        # Reassign primary community — pick any other community they belong to
        if member and member.primary_community_id == mem.community_id:
            other = (CommunityMembership.query
                     .filter(CommunityMembership.user_id == member.id,
                             CommunityMembership.community_id != mem.community_id,
                             CommunityMembership.id != mem.id)
                     .join(Community, CommunityMembership.community_id == Community.id)
                     .filter(Community.is_global_reserve == False)
                     .first())
            member.primary_community_id = other.community_id if other else None
            member.primary_community_changed_at = datetime.utcnow()
        comm_name = community.name if community else '?'
        comm_id   = mem.community_id
        user_id   = mem.user_id
        db.session.delete(mem)
        db.session.flush()
        # Recalculate draw ceiling after leave
        try:
            compute_draw_ceiling(user_id)
        except Exception:
            pass
        _log_admin_action(admin_user.id, 'leave_approved',
                          target_user_id=user_id,
                          details=f'Leave from community #{comm_id} ({comm_name})' + (f' — {reason}' if reason else ''))
        flash(f'Leave approved — {member.name if member else "user"} removed from {comm_name}.')
    elif action == 'deny':
        mem.leave_requested_at = None
        mem.leave_status = 'rejected'
        mem.leave_rejection_reason = reason or 'Request denied by admin.'
        _log_admin_action(admin_user.id, 'leave_denied',
                          target_user_id=mem.user_id,
                          details=f'Leave denied for community #{mem.community_id}' + (f' — reason: {reason}' if reason else ''))
        flash('Leave request denied.')
    db.session.commit()
    return redirect(url_for('admin_leave_requests'))


@app.route('/admin/care/<int:request_id>', methods=['POST'])
@admin_required
@roles_required('super_admin', 'operator')
def admin_care_action(request_id):
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get(request_id)
    if not care_req:
        return "Request not found", 404
    action = request.form.get('action')
    if action == 'approve':
        old_status = care_req.status
        care_req.admin_approved = True
        care_req.admin_id = user.id
        care_req.status = 'admin_approved'
        payable = _total_payable(care_req)
        success, ref = pay_provider(
            care_request_id=care_req.id, amount=payable if payable > 0 else float(care_req.amount_needed or 0),
            provider_id=care_req.provider_id, user_id=care_req.user_id,
            community_id=care_req.community_id
        )
        if success:
            care_req.payment_transaction_id = ref
        # Update net_support_balance for the requester
        _req_user = User.query.get(care_req.user_id)
        if _req_user:
            _req_user.net_support_balance = round(
                (_req_user.net_support_balance or 0.0) - float(care_req.amount_needed or 0), 2)
        db.session.commit()
        _log_admin_action(user.id, 'care_request_approved',
                          target_user_id=care_req.user_id,
                          details=f'Care request #{care_req.id} UGX {care_req.amount_needed:,.0f}',
                          old_value=old_status, new_value='admin_approved')
    elif action == 'deny':
        old_status = care_req.status
        reason = request.form.get('reason', '')
        _reverse_care_request_financials(care_req)
        care_req.status = 'rejected'
        db.session.commit()
        try:
            from notifications import notify_member_care_rejected
            _deny_user = User.query.get(care_req.user_id)
            if _deny_user:
                notify_member_care_rejected(_deny_user, reason)
        except Exception:
            pass
        _log_admin_action(user.id, 'care_request_denied',
                          target_user_id=care_req.user_id,
                          details=f'Care request #{care_req.id}. Reason: {reason[:200]}',
                          old_value=old_status, new_value='rejected')
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
        need_admin = bool(care_req.fraud_flagged)
        if need_admin:
            care_req.status = 'pending_admin'
            try:
                _care_user2 = db.session.get(User, care_req.user_id)
                notify_admin_care_pending(
                    _get_admin_phones(),
                    _care_user2.name if _care_user2 else 'Unknown',
                    float(care_req.amount_needed or 0),
                    care_req.id,
                )
            except Exception:
                pass
        else:
            care_req.status = 'admin_approved'
            care_req.admin_approved = True
            payable = _total_payable(care_req)
            success, ref = pay_provider(
                care_request_id=care_req.id,
                amount=payable if payable > 0 else float(care_req.amount_needed or 0),
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
        payable = _total_payable(care_req)
        success, ref = pay_provider(
            care_request_id=care_req.id,
            amount=payable if payable > 0 else float(care_req.amount_needed or 0),
            provider_id=care_req.provider_id, user_id=care_req.user_id,
            community_id=care_req.community_id
        )
        if success:
            care_req.payment_transaction_id = ref
        db.session.commit()
        msg = f"Request #{request_id} approved."
    elif action == 'reject':
        _reverse_care_request_financials(care_req)
        care_req.status = 'rejected'
        try:
            from notifications import notify_member_care_rejected
            _deny_user = User.query.get(care_req.user_id)
            if _deny_user:
                notify_member_care_rejected(_deny_user)
        except Exception:
            pass
        db.session.commit()
        msg = f"Request #{request_id} rejected — funds returned to member."
    else:
        return "Invalid action"
    return f"<p>{msg}</p><p><a href='/'>Home</a> | <a href='/community/{community.id}'>Back to Community</a></p>"

@app.route('/api/inbox')
def api_inbox():
    """Lightweight endpoint polled by the dashboard for live invoice/task counts."""
    if 'user_id' not in session:
        from flask import jsonify
        return jsonify({'error': 'unauthenticated'}), 401
    from flask import jsonify
    uid = session['user_id']
    pending_invoices = CareRequest.query.filter_by(
        user_id=uid, status='pending_patient_approval').count()
    pending_witness = CareRequest.query.filter(
        CareRequest.status == 'pending_witness',
        CareRequest.witness_ids.like(f'%{uid}%')
    ).count()
    return jsonify({
        'pending_invoices': pending_invoices,
        'pending_witness':  pending_witness,
        'total':            pending_invoices + pending_witness,
    })


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/logout-all', methods=['POST'])
def logout_all_devices():
    """Invalidate all active sessions for this user by bumping session_version."""
    uid = session.get('user_id')
    if not uid:
        return redirect(url_for('login'))
    user = db.session.get(User, uid)
    if user:
        user.session_version = (user.session_version or 1) + 1
        db.session.commit()
    session.clear()
    flash('You have been logged out from all devices.', 'info')
    return redirect(url_for('login'))

@app.route('/admin')
def admin_redirect():
    return redirect(url_for('admin_care'))

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
        if not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
            return render_template('change_pin.html', error='New PIN must be 4–6 digits.', success=None)
        if _is_weak_pin(new_pin):
            return render_template('change_pin.html', error='That PIN is too easy to guess. Choose a less predictable PIN.', success=None)
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

@app.route('/balance')
def balance():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if not session.get('pin_verified'):
        return redirect(url_for('verify_pin', next=url_for('balance')))
    user = User.query.get(session['user_id'])
    primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    try:
        ceiling = round(compute_draw_ceiling(user.id), 2)
    except Exception:
        ceiling = 0.0
    ph = _pool_health(primary_comm.pool_balance) if primary_comm else None
    return render_template('balance.html', user=user, primary_comm=primary_comm,
                           ceiling=ceiling, pool_health=ph)


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
    # Summary stats
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    total_earned     = sum(p.amount for p in payments if getattr(p, 'reversed', False) is not True)
    month_payments   = [p for p in payments if p.created_at and p.created_at >= month_start]
    month_earned     = sum(p.amount for p in month_payments if getattr(p, 'reversed', False) is not True)
    pending_count    = sum(1 for p in payments if p.status == 'sent')
    patients_served  = len({p.user_id for p in payments if p.status not in ('reversed',)})
    # Invoices sent to patients awaiting their approval
    awaiting_invoices = (CareRequest.query
                         .filter_by(provider_id=provider.id, status='pending_patient_approval')
                         .order_by(CareRequest.created_at.desc())
                         .all())
    return render_template('provider_dashboard.html', provider=provider, payments=payments,
                           total_earned=total_earned, month_earned=month_earned,
                           pending_count=pending_count, patients_served=patients_served,
                           awaiting_invoices=awaiting_invoices)

@app.route('/provider/confirm/<ref>')
def confirm_payment(ref):
    payment = PaymentRecord.query.filter_by(reference_code=ref).first()
    if payment and payment.status == 'sent':
        payment.status = 'received'
        payment.provider_confirmed_at = datetime.utcnow()
        db.session.commit()
        try:
            from notifications import notify_payment_received
            if payment.user_id:
                member = User.query.get(payment.user_id)
                provider = Provider.query.get(payment.provider_id)
                if member and provider:
                    notify_payment_received(member, provider.name, payment.amount)
        except Exception as exc:
            pass
    return redirect(url_for('provider_dashboard'))

@app.route('/provider/start/<ref>')
def start_treatment(ref):
    payment = PaymentRecord.query.filter_by(reference_code=ref).first()
    if payment and payment.status == 'received':
        payment.status = 'treatment_started'
        payment.treatment_started_at = datetime.utcnow()
        db.session.commit()
        try:
            from notifications import notify_treatment_started
            if payment.user_id:
                member = User.query.get(payment.user_id)
                provider = Provider.query.get(payment.provider_id)
                if member and provider:
                    notify_treatment_started(member, provider.name)
        except Exception as exc:
            pass
    return redirect(url_for('provider_dashboard'))

@app.route('/provider/invoice', methods=['POST'])
def provider_invoice():
    if 'provider_id' not in session:
        return redirect(url_for('provider_login'))
    provider_code = request.form.get('provider_code', '').strip()
    provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
    if not provider:
        flash('Provider code not recognised or provider is not verified.', 'error')
        return redirect(url_for('provider_dashboard'))
    patient_phone = request.form.get('patient_phone', '').strip()
    user = User.query.filter_by(phone=patient_phone).first()
    if not user:
        flash(f'No member registered with phone {patient_phone}. Ask the patient to register first.', 'error')
        return redirect(url_for('provider_dashboard'))
    try:
        amount = float(request.form.get('amount', 0) or 0)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash('Please enter a valid invoice amount.', 'error')
        return redirect(url_for('provider_dashboard'))
    description = request.form.get('description', 'Medical Treatment').strip() or 'Medical Treatment'

    # Resolve community — wallet-only patients don't need one
    community = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    if community and community.is_global_reserve:
        community = None
    if not community:
        ms = (CommunityMembership.query
              .filter_by(user_id=user.id)
              .join(Community, CommunityMembership.community_id == Community.id)
              .filter(Community.is_global_reserve == False)
              .first())
        community = Community.query.get(ms.community_id) if ms else None
    # If still no community, wallet-only path is fine as long as wallet covers it
    if not community and user.sub_wallet_balance < amount:
        flash(
            f'{user.name} has no community and their wallet balance (UGX {user.sub_wallet_balance:,.0f}) '
            f'is less than the invoice amount (UGX {amount:,.0f}). '
            f'Ask the patient to top up their wallet or join a community pool.',
            'error'
        )
        return redirect(url_for('provider_dashboard'))

    care_req = CareRequest(
        user_id=user.id,
        community_id=community.id if community else None,
        provider_id=provider.id,
        amount_needed=amount,
        amount_from_sub=0,
        amount_from_pool=0,
        social_credit=0,
        is_emergency=False,
        status='pending_patient_approval',
        witness_ids='',
    )
    if hasattr(care_req, 'invoice_description'):
        care_req.invoice_description = description
    db.session.add(care_req)
    db.session.commit()

    try:
        from notifications import send_sms
        send_sms(
            user.phone,
            f"Hi {user.name}, {provider.name} has sent you a care invoice for UGX {amount:,.0f} "
            f"({description}). Log in to SolidarityPool to review and approve or reject it."
        )
    except Exception:
        pass

    flash(
        f'Invoice of UGX {amount:,.0f} sent to {user.name} ({patient_phone}). '
        f'They will see it on their dashboard and must approve before funds are processed.',
        'success'
    )
    return redirect(url_for('provider_dashboard'))


@app.route('/invoice/<int:request_id>')
def invoice_review(request_id):
    """Patient reviews a provider-submitted invoice."""
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get_or_404(request_id)
    if care_req.user_id != user.id:
        flash('This invoice is not addressed to you.', 'error')
        return redirect(url_for('home'))
    if care_req.status != 'pending_patient_approval':
        flash('This invoice has already been processed.', 'info')
        return redirect(url_for('home'))
    provider = Provider.query.get(care_req.provider_id) if care_req.provider_id else None
    community = Community.query.get(care_req.community_id) if care_req.community_id else None
    description = getattr(care_req, 'invoice_description', '') or 'Medical Treatment'
    return render_template('invoice_review.html', user=user, care_req=care_req,
                           provider=provider, community=community, description=description)


@app.route('/invoice/<int:request_id>/approve', methods=['POST'])
def invoice_approve(request_id):
    """Patient approves the provider invoice — runs the full financial processing."""
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get_or_404(request_id)
    if care_req.user_id != user.id or care_req.status != 'pending_patient_approval':
        flash('Invalid or already-processed invoice.', 'error')
        return redirect(url_for('home'))

    provider_id = care_req.provider_id
    provider = Provider.query.get(provider_id) if provider_id else None
    needed_amount = float(care_req.amount_needed or 0)

    primary_community = Community.query.get(care_req.community_id) if care_req.community_id else None
    global_reserve = Community.query.filter_by(is_global_reserve=True).first()

    # ── Financial allocation ──────────────────────────────────────────────────
    from_sub = round(min(user.sub_wallet_balance, needed_amount), 2)
    remaining = round(needed_amount - from_sub, 2)
    user.sub_wallet_balance -= from_sub

    from_pool = 0.0
    if remaining > 0 and primary_community and not primary_community.is_global_reserve:
        available = round(min(remaining, primary_community.pool_balance), 2)
        from_pool = available
        primary_community.pool_balance -= from_pool
        remaining = round(remaining - from_pool, 2)

    from_reserve = 0.0
    if remaining > 0 and global_reserve:
        available_reserve = round(min(remaining, global_reserve.pool_balance), 2)
        from_reserve = available_reserve
        global_reserve.pool_balance -= from_reserve
        remaining = round(remaining - from_reserve, 2)

    social_credit = round(remaining, 2)
    if social_credit > 0:
        user.total_social_credit = round((user.total_social_credit or 0.0) + social_credit, 2)
        try:
            update_recovery_parameters(user.id, social_credit)
        except Exception:
            pass

    care_req.amount_from_sub = from_sub
    care_req.amount_from_pool = from_pool
    care_req.social_credit = social_credit
    if hasattr(care_req, 'amount_from_reserve'):
        care_req.amount_from_reserve = from_reserve

    # ── Fraud scoring ─────────────────────────────────────────────────────────
    fraud_score = 0.0
    try:
        fraud_score, fraud_reasons = calculate_fraud_risk(user.id, care_req.id)
        care_req.fraud_score = fraud_score
        if is_fraud_flagged(fraud_score):
            care_req.fraud_flagged = True
            care_req.fraud_reasons = '; '.join(fraud_reasons)
            log_fraud_alert(user.id, care_req.id, fraud_score, fraud_reasons)
            try:
                notify_admin_fraud_alert(_get_admin_phones(), user.name, fraud_score, care_req.id)
            except Exception:
                pass
    except Exception:
        pass

    # ── Tier assignment ───────────────────────────────────────────────────────
    is_verified_provider = bool(provider and provider.verified)
    if (needed_amount <= 50_000
            and user.trust_score >= 0.6
            and is_verified_provider
            and fraud_score < 0.3
            and not care_req.fraud_flagged):
        tier = 1
    elif care_req.fraud_flagged:
        tier = 3
    else:
        tier = 2

    care_req.risk_tier = tier

    if tier == 1:
        care_req.status = 'approved'
        care_req.admin_approved = True
        user.net_support_balance = round((user.net_support_balance or 0.0) - needed_amount, 2)
        db.session.commit()
        try:
            payable = _total_payable(care_req)
            ok, ref = pay_provider(
                care_request_id=care_req.id,
                amount=payable if payable > 0 else needed_amount,
                provider_id=provider_id, user_id=user.id,
                community_id=care_req.community_id,
            )
            if ok:
                care_req.payment_transaction_id = ref
                db.session.commit()
        except Exception:
            pass
        flash(f'Invoice approved. UGX {needed_amount:,.0f} has been processed for {provider.name if provider else "the provider"}.', 'success')
    elif tier == 2:
        care_req.status = 'pending_community_admin'
        db.session.commit()
        try:
            _notify_community_admins_care_pending(care_req, user)
        except Exception:
            pass
        flash('Invoice approved and sent to your community admin for review.', 'success')
    else:
        care_req.status = 'pending_admin'
        db.session.commit()
        try:
            notify_admin_care_pending(
                _get_admin_phones(), user.name,
                float(care_req.amount_needed or 0), care_req.id)
        except Exception:
            pass
        flash('Invoice approved and escalated to system review.', 'success')

    db.session.commit()
    return redirect(url_for('home'))


@app.route('/invoice/<int:request_id>/reject', methods=['POST'])
def invoice_reject(request_id):
    """Patient rejects the provider invoice — cancels the care request."""
    if 'user_id' not in session:
        return redirect(url_for('register'))
    user = User.query.get(session['user_id'])
    care_req = CareRequest.query.get_or_404(request_id)
    if care_req.user_id != user.id or care_req.status != 'pending_patient_approval':
        flash('Invalid or already-processed invoice.', 'error')
        return redirect(url_for('home'))

    care_req.status = 'rejected'
    db.session.commit()

    provider = Provider.query.get(care_req.provider_id) if care_req.provider_id else None
    try:
        if provider and provider.contact_phone:
            from notifications import send_sms
            send_sms(
                provider.contact_phone,
                f"Invoice #{care_req.id} for UGX {care_req.amount_needed:,.0f} was rejected by the patient."
            )
    except Exception:
        pass

    flash('Invoice rejected. The provider has been notified.', 'info')
    return redirect(url_for('home'))

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
            "9. Contribution history\n"
            "10. Change PIN\n")
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
            return r("Choose a 4–6 digit PIN:")
        if step == 3 and raw_inputs[0] == "1":
            name = inputs[1]
            pin = inputs[2].strip()
            if not pin.isdigit() or not (4 <= len(pin) <= 6):
                return r("PIN must be 4–6 digits. Dial again.", end=True)
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

    # ── 1. Balance (PIN-gated) ─────────────────────────────────────────────────
    if choice == "1":
        if step == 1:
            return r("Enter your PIN to check balance:")
        if step == 2:
            entered_pin = inputs[1].strip()
            if entered_pin != (user.pin or '1234'):
                return r("Incorrect PIN. Dial again.", end=True)
            try:
                ceil_val = compute_draw_ceiling(user.id)
            except Exception:
                ceil_val = 0.0
            if primary_comm:
                ph = _pool_health(primary_comm.pool_balance)
                bal = (f"Wallet: UGX {user.sub_wallet_balance:,.0f}\n"
                       f"Draw ceiling: UGX {ceil_val:,.0f}\n"
                       f"Contributed (lifetime): UGX {(user.lifetime_contribution_score or 0):,.0f}\n"
                       f"Social credit owed: UGX {user.total_social_credit:,.0f}\n"
                       f"Pool ({primary_comm.name}): {ph['label']}")
            else:
                bal = (f"Wallet: UGX {user.sub_wallet_balance:,.0f}\n"
                       f"Draw ceiling: UGX {ceil_val:,.0f}\n"
                       f"Contributed (lifetime): UGX {(user.lifetime_contribution_score or 0):,.0f}\n"
                       f"Social credit owed: UGX {user.total_social_credit:,.0f}")
            return r(bal, end=True)
        return r("Session error. Dial again.", end=True)

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
                if comm.is_global_reserve:
                    return r("That community cannot be joined.", end=True)
                existing = CommunityMembership.query.filter_by(user_id=user.id, community_id=comm.id).first()
                if existing:
                    return r("You are already a member of that community.", end=True)
                new_mem = CommunityMembership(user_id=user.id, community_id=comm.id, role='member')
                db.session.add(new_mem)
                if not user.primary_community_id:
                    user.primary_community_id = comm.id
                    user.primary_community_changed_at = datetime.utcnow()
                db.session.commit()
                return r(f"Joined {comm.name}.", end=True)
        return r("Invalid choice.\n0. Back")

    # ── 2. Request care ───────────────────────────────────────────────────────
    if choice == "2":
        user_communities = get_user_communities(user.id)

        # Step 1 → ask for PIN
        if step == 1:
            return r("Enter your PIN to continue:")

        # Step 2 → verify PIN, show mode menu
        if step == 2:
            entered_pin = inputs[1].strip()
            if entered_pin != (user.pin or '1234'):
                return r("Incorrect PIN. Dial again.", end=True)
            wallet_str = f"UGX {user.sub_wallet_balance:,.0f}"
            return r(
                f"Care request\n"
                f"Your wallet: {wallet_str}\n"
                "1. Pay from my wallet only\n"
                "2. Request from community pool\n"
                "0. Back"
            )

        sess = ussd_sessions.get(phone, {})

        # Step 3 → handle mode choice (first time past step 2)
        if "care_mode" not in sess:
            mode_choice = inputs[2].strip() if len(inputs) > 2 else ""
            if mode_choice == "1":
                sess = {"care_mode": "wallet", "state": "awaiting_amount"}
                ussd_sessions[phone] = sess
                return r(
                    f"Wallet-only payment\n"
                    f"Balance: UGX {user.sub_wallet_balance:,.0f}\n"
                    "No witnesses needed.\n"
                    "Enter amount (UGX):\n0. Back"
                )
            elif mode_choice == "2":
                _primary_comm = (Community.query.get(user.primary_community_id)
                                 if user.primary_community_id else None)
                if not _primary_comm:
                    ussd_sessions.pop(phone, None)
                    return r("You need a primary community.\nDial again → 4. Community to join one.", end=True)
                try:
                    _ceil = compute_draw_ceiling(user.id)
                except Exception:
                    _ceil = 0.0
                sess = {
                    "care_mode": "pool",
                    "state": "awaiting_amount",
                    "ceiling": _ceil,
                }
                ussd_sessions[phone] = sess
                return r(
                    f"Request care\n"
                    f"Ceiling: UGX {_ceil:,.0f}\n"
                    f"Wallet: UGX {user.sub_wallet_balance:,.0f}\n"
                    f"Funds from: wallet → pool → reserve\n"
                    "Enter amount (UGX):\n0. Back"
                )
            return r("Invalid choice.\n1. Wallet only\n2. Community pool\n0. Back")

        care_mode = sess.get("care_mode", "")

        # ── Wallet-only path ──────────────────────────────────────────────────
        if care_mode == "wallet":
            state = sess.get("state", "")

            if state == "awaiting_amount" and "amount" not in sess:
                try:
                    amount = float(inputs[step - 1])
                    if amount <= 0:
                        raise ValueError
                except (ValueError, IndexError):
                    return r("Invalid amount. Enter UGX:\n0. Back")
                if amount > user.sub_wallet_balance:
                    ussd_sessions.pop(phone, None)
                    return r(
                        f"Insufficient wallet.\n"
                        f"Balance: UGX {user.sub_wallet_balance:,.0f}\n"
                        f"Needed: UGX {amount:,.0f}\n"
                        "Dial again, choose pool option.",
                        end=True
                    )
                sess["amount"] = amount
                sess["state"] = "awaiting_provider"
                ussd_sessions[phone] = sess
                return r("Enter provider code (e.g., MULAGO001):\n0. Back")

            if sess.get("state") == "awaiting_provider" and "provider_id" not in sess:
                provider_code = inputs[step - 1].strip().upper()
                provider = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
                if not provider:
                    sample = Provider.query.filter_by(verified=True).first()
                    hint = sample.provider_code if sample else 'MULAGO001'
                    return r(f"Invalid code '{provider_code}'.\nTry {hint} or ask clinic.\n0. Back")
                sess["provider_id"] = provider.id
                sess["state"] = "awaiting_confirm"
                ussd_sessions[phone] = sess
                amount = sess["amount"]
                return r(
                    f"Confirm payment\n"
                    f"UGX {amount:,.0f} to {provider.name}\n"
                    f"From your wallet. No witnesses.\n"
                    "1. Confirm\n2. Cancel"
                )

            if sess.get("state") == "awaiting_confirm" and "provider_id" in sess:
                confirm_input = inputs[step - 1].strip()
                if confirm_input != "1":
                    ussd_sessions.pop(phone, None)
                    return r("Cancelled.", end=True)
                amount = sess["amount"]
                provider_id = sess["provider_id"]
                user.sub_wallet_balance -= amount
                comm_id = user.primary_community_id
                if not comm_id and user_communities:
                    comm_id = user_communities[0].id
                care_req = CareRequest(
                    user_id=user.id, community_id=comm_id, provider_id=provider_id,
                    amount_needed=amount, amount_from_sub=amount, amount_from_pool=0.0,
                    social_credit=0.0, is_emergency=False, status='approved',
                )
                db.session.add(care_req)
                db.session.commit()
                try:
                    pay_provider(care_req.id)
                except Exception:
                    pass
                ussd_sessions.pop(phone, None)
                return r(
                    f"Payment confirmed!\n"
                    f"UGX {amount:,.0f} sent to provider.\n"
                    f"Request #{care_req.id}",
                    end=True
                )

            return r("Session expired. Dial again.", end=True)

        # ── Community pool path: auto-fund wallet → primary → reserve ─────────
        if care_mode == "pool":
            state = sess.get("state", "")

            # Amount input
            if state == "awaiting_amount" and "amount" not in sess:
                try:
                    amount = float(inputs[step - 1])
                    if amount <= 0:
                        raise ValueError
                except (ValueError, IndexError):
                    return r("Invalid amount. Enter a number (UGX):\n0. Back")
                sess["amount"] = amount
                sess["state"] = "awaiting_provider"
                ussd_sessions[phone] = sess
                return r("Enter provider code\n(e.g. MULAGO001):\n0. Back")

            # Provider code input
            if state == "awaiting_provider" and "provider_id" not in sess:
                provider_code = inputs[step - 1].strip().upper()
                _provider = Provider.query.filter_by(provider_code=provider_code).first()
                if not _provider:
                    sample = Provider.query.filter_by(verified=True).first()
                    hint = sample.provider_code if sample else 'MULAGO001'
                    return r(f"Invalid code '{provider_code}'.\nTry {hint}.\n0. Back")
                sess["provider_id"] = _provider.id
                sess["provider_verified"] = bool(_provider.verified)
                sess["state"] = "awaiting_confirm"
                ussd_sessions[phone] = sess
                _amt = sess["amount"]
                return r(
                    f"Confirm care request\n"
                    f"UGX {_amt:,.0f} → {_provider.name}\n"
                    f"Funds: wallet→pool→reserve\n"
                    "1. Confirm\n2. Cancel"
                )

            # Confirm + submit
            if state == "awaiting_confirm" and "provider_id" in sess:
                confirm_input = inputs[step - 1].strip()
                if confirm_input != "1":
                    ussd_sessions.pop(phone, None)
                    return r("Cancelled.", end=True)

                amount = sess["amount"]
                provider_id = sess["provider_id"]
                provider_verified = sess.get("provider_verified", False)
                primary_comm = (Community.query.get(user.primary_community_id)
                                if user.primary_community_id else None)
                global_reserve = Community.query.filter_by(is_global_reserve=True).first()
                ceiling = sess.get("ceiling") or compute_draw_ceiling(user.id)

                # Auto-fund: wallet → primary → reserve
                from_sub = round(min(user.sub_wallet_balance, amount), 2)
                remaining = round(amount - from_sub, 2)
                user.sub_wallet_balance -= from_sub

                from_pool = 0.0
                if remaining > 0 and primary_comm:
                    if not is_large_withdrawal_blocked(primary_comm, remaining):
                        avail = min(remaining, max(0.0, ceiling - from_sub), primary_comm.pool_balance)
                        from_pool = round(avail, 2)
                        primary_comm.pool_balance -= from_pool
                        remaining = round(remaining - from_pool, 2)

                from_reserve = 0.0
                if remaining > 0 and global_reserve:
                    avail_r = min(remaining, global_reserve.pool_balance)
                    from_reserve = round(avail_r, 2)
                    global_reserve.pool_balance -= from_reserve
                    remaining = round(remaining - from_reserve, 2)

                social_credit = round(remaining, 2)
                if social_credit > 0:
                    user.total_social_credit = round((user.total_social_credit or 0.0) + social_credit, 2)
                    update_recovery_parameters(user.id, social_credit)

                care_req = CareRequest(
                    user_id=user.id,
                    community_id=(primary_comm.id if primary_comm else
                                  (global_reserve.id if global_reserve else None)),
                    provider_id=provider_id,
                    amount_needed=amount, amount_from_sub=from_sub,
                    amount_from_pool=from_pool, social_credit=social_credit,
                    is_emergency=False, status='pending_witness',
                )
                if hasattr(care_req, 'amount_from_reserve'):
                    care_req.amount_from_reserve = from_reserve
                db.session.add(care_req)
                db.session.commit()

                # Fraud scoring
                fraud_score = 0.0
                try:
                    fraud_score, fraud_reasons = calculate_fraud_risk(user.id, care_req.id)
                    care_req.fraud_score = fraud_score
                    if is_fraud_flagged(fraud_score):
                        care_req.fraud_flagged = True
                        care_req.fraud_reasons = '; '.join(fraud_reasons)
                        log_fraud_alert(user.id, care_req.id, fraud_score, fraud_reasons)
                except Exception:
                    pass

                # Risk tier (replaces witness gate)
                if (amount <= 50_000 and user.trust_score >= 0.6
                        and provider_verified and fraud_score < 0.3
                        and not care_req.fraud_flagged):
                    tier = 1
                elif amount <= 200_000 and fraud_score < 0.6 and not care_req.fraud_flagged:
                    tier = 2
                else:
                    tier = 3

                care_req.risk_tier = tier
                if tier == 1:
                    care_req.status = 'approved'
                    care_req.admin_approved = True
                    user.net_support_balance = round((user.net_support_balance or 0.0) - amount, 2)
                    db.session.commit()
                    try:
                        pool_amount = from_pool + from_reserve
                        if pool_amount > 0:
                            pay_provider(care_request_id=care_req.id, amount=pool_amount,
                                         provider_id=provider_id, user_id=user.id,
                                         community_id=care_req.community_id)
                        else:
                            pay_provider(care_req.id)
                    except Exception:
                        pass
                    tier_msg = "Auto-approved! Provider notified."
                elif tier == 2:
                    care_req.status = 'pending_community_admin'
                    db.session.commit()
                    tier_msg = "Pending community admin review."
                else:
                    care_req.status = 'pending_admin'
                    db.session.commit()
                    try:
                        notify_admin_care_pending(_get_admin_phones(), user.name, amount, care_req.id)
                    except Exception:
                        pass
                    tier_msg = "Pending admin review."

                if primary_comm:
                    try:
                        enforce_pool_health(primary_comm)
                    except Exception:
                        pass

                ussd_sessions.pop(phone, None)
                ceiling_remaining = max(0.0, ceiling - from_pool - from_reserve)
                return r(
                    f"Request #{care_req.id} submitted.\n"
                    f"UGX {amount:,.0f} requested.\n"
                    f"Ceiling left: UGX {ceiling_remaining:,.0f}\n"
                    f"{tier_msg}",
                    end=True
                )

            return r("Session expired. Dial again.", end=True)

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
                need_system_admin = bool(care_req.fraud_flagged)
                if need_system_admin:
                    care_req.status = 'pending_admin'
                else:
                    care_req.status = 'pending_community_admin'
                    try:
                        _notify_community_admins_care_pending(
                            care_req, db.session.get(User, care_req.user_id))
                    except Exception:
                        pass
                db.session.commit()
            elif len(votes) >= total:
                _reverse_care_request_financials(care_req)
                care_req.status = 'rejected'
                db.session.commit()
            ussd_sessions.pop(phone, None)
            return r("Vote recorded. Thank you.", end=True)

    # ── 6. Admin panel ────────────────────────────────────────────────────────
    if choice == "6":
        if role not in ['admin', 'coadmin'] or not primary_comm:
            return r("Not authorised.", end=True)
        if step == 1:
            menu = "Admin panel\n1. Approve requests\n2. Invite code\n3. Members\n0. Back"
            if role == 'admin':
                menu = "Admin panel\n1. Approve requests\n2. Invite code\n3. Members\n4. Manage co-admins\n0. Back"
            return r(menu)
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
        elif sub == "4":
            if role != 'admin':
                return r("Only the community admin can manage co-admins.", end=True)
            all_ms = CommunityMembership.query.filter_by(community_id=primary_comm.id).all()
            manageable = [m for m in all_ms if m.role in ('member', 'coadmin')]
            if step == 2:
                if not manageable:
                    return r("No members to manage.", end=True)
                lines = []
                for i, m in enumerate(manageable[:8], 1):
                    u = User.query.get(m.user_id)
                    lines.append(f"{i}. {u.name if u else '?'} ({m.role})")
                ussd_sessions[phone] = {'coadmin_list': [m.user_id for m in manageable[:8]]}
                return r("Manage co-admins\n" + "\n".join(lines) + "\n\nEnter member number:")
            if step == 3:
                data = ussd_sessions.get(phone, {})
                coadmin_list = data.get('coadmin_list', [])
                try:
                    idx = int(inputs[2]) - 1
                    if idx < 0 or idx >= len(coadmin_list):
                        return r("Invalid number. Dial again.", end=True)
                except (ValueError, IndexError):
                    return r("Invalid input. Dial again.", end=True)
                selected_user_id = coadmin_list[idx]
                sel_ms = CommunityMembership.query.filter_by(
                    user_id=selected_user_id, community_id=primary_comm.id).first()
                sel_user = User.query.get(selected_user_id)
                data['coadmin_selected'] = selected_user_id
                ussd_sessions[phone] = data
                return r(
                    f"Selected: {sel_user.name if sel_user else '?'} ({sel_ms.role if sel_ms else '?'})\n"
                    "1. Promote to co-admin\n2. Demote to member\n0. Cancel"
                )
            if step == 4:
                data = ussd_sessions.get(phone, {})
                selected_user_id = data.get('coadmin_selected')
                if not selected_user_id:
                    return r("Session expired. Dial again.", end=True)
                action = inputs[3].strip()
                sel_ms = CommunityMembership.query.filter_by(
                    user_id=selected_user_id, community_id=primary_comm.id).first()
                sel_user = User.query.get(selected_user_id)
                if action == "1":
                    if not sel_ms or sel_ms.role == 'admin':
                        return r("Cannot promote this member.", end=True)
                    sel_ms.role = 'coadmin'
                    db.session.commit()
                    ussd_sessions.pop(phone, None)
                    return r(f"{sel_user.name if sel_user else 'Member'} promoted to co-admin.", end=True)
                elif action == "2":
                    if not sel_ms or sel_ms.role == 'admin':
                        return r("Cannot demote this member.", end=True)
                    sel_ms.role = 'member'
                    db.session.commit()
                    ussd_sessions.pop(phone, None)
                    return r(f"{sel_user.name if sel_user else 'Member'} demoted to member.", end=True)
                else:
                    ussd_sessions.pop(phone, None)
                    return r("Cancelled.", end=True)
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
                  "is calculated from your operator fee and credited to your health wallet "
                  "and community pools."),
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

    # ── 10. Change PIN ────────────────────────────────────────────────────────
    if choice == "10":
        if step == 1:
            return r("Change PIN\nEnter your current PIN:")
        if step == 2:
            entered = inputs[1].strip()
            if entered != (user.pin or '1234'):
                return r("Incorrect current PIN. Dial again.", end=True)
            return r("Enter your new 4–6 digit PIN:")
        if step == 3:
            new_pin = inputs[2].strip()
            if not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
                return r("PIN must be 4–6 digits. Dial again.", end=True)
            return r("Confirm new PIN:")
        if step == 4:
            new_pin = inputs[2].strip()
            confirm_pin = inputs[3].strip()
            if new_pin != confirm_pin:
                return r("PINs do not match. Dial again.", end=True)
            user.pin = new_pin
            db.session.commit()
            return r("PIN changed successfully.", end=True)

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
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)

    total_solidarity = db.session.query(
        func.coalesce(func.sum(Transaction.amount), 0.0)
    ).filter(Transaction.type.in_(['solidarity_wallet', 'solidarity_pool', 'solidarity_fee'])).scalar() or 0.0

    total_platform_revenue = db.session.query(
        func.coalesce(func.sum(PlatformRevenue.amount), 0.0)
    ).scalar() or 0.0
    revenue_count = PlatformRevenue.query.count()
    recent_revenue = PlatformRevenue.query.order_by(PlatformRevenue.timestamp.desc()).limit(20).all()

    active_users = db.session.query(func.count(func.distinct(Transaction.user_id))).filter(
        Transaction.timestamp >= thirty_days_ago
    ).scalar() or 0

    total_users = User.query.count()
    locked_users = User.query.filter(User.is_locked == True).count()
    inactive_users = User.query.filter(User.is_active == False).count()
    fraud_count = FraudAlert.query.filter_by(resolved=False).count()
    pending_verifications = VerifiedProvider.query.filter_by(verification_status='pending').count()
    # Platform fee available balance
    from sqlalchemy import func as sqlfunc
    total_platform_withdrawn = db.session.query(
        sqlfunc.coalesce(sqlfunc.sum(PlatformWithdrawal.amount), 0.0)
    ).scalar() or 0.0
    available_fee_balance = max(0.0, total_platform_revenue - total_platform_withdrawn)

    # Recent failed logins (last 1 hr)
    recent_failed_logins = UserLoginHistory.query.filter(
        UserLoginHistory.success == False,
        UserLoginHistory.timestamp >= one_hour_ago
    ).count()

    # Care queue stats
    pending_care = CareRequest.query.filter(CareRequest.status.in_(['pending_witness', 'pending_admin'])).count()

    # System service status
    at_configured = bool(os.getenv('AT_USERNAME') and os.getenv('AT_API_KEY'))
    mpesa_configured = bool(os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET'))

    # Communities with member counts + pool health
    communities_raw = Community.query.order_by(Community.pool_balance.desc()).all()
    communities = []
    alerts = []
    for comm in communities_raw:
        comm.member_count = CommunityMembership.query.filter_by(community_id=comm.id).count()
        communities.append(comm)
        # Only alert on the global reserve — individual community pools are managed by community admins
        if comm.is_global_reserve:
            if comm.pool_target and comm.pool_balance / comm.pool_target < 0.2:
                alerts.append({'level': 'critical', 'msg': f'Global Reserve pool is critically low ({comm.pool_balance/comm.pool_target*100:.0f}% full) — platform-wide payouts may be affected'})
            elif comm.large_withdrawal_paused:
                alerts.append({'level': 'warn', 'msg': 'Global Reserve has large withdrawals paused'})

    if fraud_count >= 5:
        alerts.append({'level': 'critical', 'msg': f'{fraud_count} unresolved fraud alerts require review'})
    elif fraud_count > 0:
        alerts.append({'level': 'warn', 'msg': f'{fraud_count} unresolved fraud alert(s)'})
    if recent_failed_logins >= 20:
        alerts.append({'level': 'critical', 'msg': f'{recent_failed_logins} failed login attempts in the last hour — possible brute-force'})
    if locked_users > 0:
        alerts.append({'level': 'info', 'msg': f'{locked_users} account(s) currently locked'})
    if not at_configured:
        alerts.append({'level': 'warn', 'msg': 'Africa\'s Talking SMS not configured — PIN reset and bulk SMS will not work'})
    if not mpesa_configured:
        alerts.append({'level': 'warn', 'msg': 'M-Pesa Daraja not configured — mobile money STK push disabled'})

    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    return render_template('admin_monitor.html',
                           total_solidarity=total_solidarity,
                           total_platform_revenue=total_platform_revenue,
                           revenue_count=revenue_count,
                           recent_revenue=recent_revenue,
                           active_users=active_users,
                           total_users=total_users,
                           locked_users=locked_users,
                           inactive_users=inactive_users,
                           fraud_count=fraud_count,
                           pending_verifications=pending_verifications,
                           available_fee_balance=available_fee_balance,
                           total_platform_withdrawn=total_platform_withdrawn,
                           pending_care=pending_care,
                           recent_failed_logins=recent_failed_logins,
                           communities=communities,
                           alerts=alerts,
                           at_configured=at_configured,
                           mpesa_configured=mpesa_configured,
                           now=now_str)


# ------------------ Admin: Trust Override ------------------

@app.route('/admin/trust')
@admin_required
def admin_trust_page():
    user = User.query.get(session['user_id'])
    return render_template('admin_trust.html', user=user)


@app.route('/admin/trust/override', methods=['POST'])
@admin_required
@super_admin_required
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


# ── Admin: Global admin management ────────────────────────────────────────────

@app.route('/admin/global-admins')
@admin_required
def admin_global_admins():
    global_admins = GlobalAdmin.query.order_by(GlobalAdmin.created_at).all()
    current_role = _get_current_admin_role() or 'super_admin'
    return render_template('admin_global_admins.html',
                           global_admins=global_admins,
                           current_user_id=session['user_id'],
                           is_super_admin=_is_super_admin(),
                           current_role=current_role,
                           error=request.args.get('error'),
                           success=request.args.get('success'))


@app.route('/admin/global-admins/add', methods=['POST'])
@admin_required
def admin_global_admins_add():
    if not _is_super_admin():
        flash('Only Super Admins can add global admins.', 'error')
        return redirect(url_for('admin_global_admins'))
    phone = request.form.get('phone', '').strip()
    role = request.form.get('role', 'support').strip()
    if role not in ('super_admin', 'support', 'operator'):
        role = 'support'
    target = User.query.filter_by(phone=phone).first()
    if not target:
        return redirect(url_for('admin_global_admins', error=f'No member found with phone {phone}.'))
    if GlobalAdmin.query.filter_by(user_id=target.id).first():
        return redirect(url_for('admin_global_admins', error=f'{target.name} is already a global admin.'))
    ga = GlobalAdmin(user_id=target.id, created_by=session['user_id'], role=role)
    db.session.add(ga)
    db.session.commit()
    _log_admin_action(session['user_id'], 'add_global_admin', target_user_id=target.id,
                      details=f'Granted {role} to {target.name} ({target.phone})')
    return redirect(url_for('admin_global_admins', success=f'{target.name} granted {role.replace("_"," ")} access.'))


@app.route('/admin/global-admins/role/<int:ga_id>', methods=['POST'])
@admin_required
def admin_global_admins_role(ga_id):
    if not _is_super_admin():
        flash('Only Super Admins can change roles.', 'error')
        return redirect(url_for('admin_global_admins'))
    ga = GlobalAdmin.query.get_or_404(ga_id)
    if ga.user_id == session['user_id']:
        return redirect(url_for('admin_global_admins', error='You cannot change your own role.'))
    new_role = request.form.get('role', 'support').strip()
    if new_role not in ('super_admin', 'support', 'operator'):
        new_role = 'support'
    old_role = ga.role
    ga.role = new_role
    db.session.commit()
    target = User.query.get(ga.user_id)
    _log_admin_action(session['user_id'], 'change_admin_role', target_user_id=ga.user_id,
                      details=f'Role changed {old_role} → {new_role} for {target.name if target else ga.user_id}')
    return redirect(url_for('admin_global_admins', success=f'Role updated to {new_role.replace("_"," ")}.'))


@app.route('/admin/global-admins/remove/<int:ga_id>', methods=['POST'])
@admin_required
def admin_global_admins_remove(ga_id):
    if not _is_super_admin():
        flash('Only Super Admins can remove global admins.', 'error')
        return redirect(url_for('admin_global_admins'))
    ga = GlobalAdmin.query.get_or_404(ga_id)
    if ga.user_id == session['user_id']:
        return redirect(url_for('admin_global_admins', error='You cannot remove yourself as global admin.'))
    target = User.query.get(ga.user_id)
    db.session.delete(ga)
    db.session.commit()
    _log_admin_action(session['user_id'], 'remove_global_admin',
                      target_user_id=ga.user_id,
                      details=f'Removed global admin from {target.name if target else ga.user_id}')
    return redirect(url_for('admin_global_admins', success='Global admin removed.'))


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


# ── Admin: Role helpers ────────────────────────────────────────────────────────

def _get_admin_role(user):
    """Return role string for a global admin, or None if not admin."""
    ga = GlobalAdmin.query.filter_by(user_id=user.id).first()
    if ga:
        return getattr(ga, 'role', None) or 'super_admin'
    if user.phone in ADMIN_PHONES:
        return 'super_admin'
    return None


def role_required(*roles):
    """Decorator: require one of the listed roles in addition to admin_required."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            user = db.session.get(User, session['user_id'])
            if not user:
                return redirect(url_for('login'))
            role = _get_admin_role(user)
            if not role:
                return render_template('admin_access_denied.html', logged_in_phone=user.phone), 403
            if role not in roles:
                return render_template('admin_access_denied.html',
                                       logged_in_phone=user.phone,
                                       role_error=f'This action requires one of: {", ".join(roles)}. Your role: {role}'), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Admin: Sequential provider code generator ─────────────────────────────────

def _generate_sequential_code(prefix: str) -> str:
    """Return next sequential code e.g. KAMPALA001, KAMPALA002."""
    import re
    prefix = re.sub(r'[^A-Z0-9]', '', prefix.upper())[:12]
    existing = Provider.query.filter(
        Provider.provider_code.like(f'{prefix}%')
    ).all()
    nums = []
    for p in existing:
        tail = p.provider_code[len(prefix):]
        if tail.isdigit():
            nums.append(int(tail))
    next_num = (max(nums) + 1) if nums else 1
    return f'{prefix}{next_num:03d}'


# ── Admin: User list ───────────────────────────────────────────────────────────

@app.route('/admin/users')
@admin_required
def admin_users():
    admin_user = User.query.get(session['user_id'])
    q = request.args.get('q', '').strip()
    community_id = request.args.get('community_id', '')
    locked = request.args.get('locked', '')
    page = max(1, int(request.args.get('page', 1)))
    per_page = 30

    query = User.query
    if q:
        query = query.filter(
            (User.name.ilike(f'%{q}%')) | (User.phone.ilike(f'%{q}%'))
        )
    if community_id:
        member_ids = [m.user_id for m in CommunityMembership.query.filter_by(community_id=int(community_id)).all()]
        query = query.filter(User.id.in_(member_ids))
    if locked == '1':
        query = query.filter(User.is_locked == True)
    elif locked == '0':
        query = query.filter((User.is_locked == False) | (User.is_locked == None))

    total = query.count()
    users = query.order_by(User.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    communities = Community.query.order_by(Community.name).all()
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template('admin_users.html',
                           user=admin_user, users=users, q=q,
                           community_id=community_id, locked=locked,
                           communities=communities, page=page,
                           total_pages=total_pages, total=total)


# ── Admin: Full user management panel ─────────────────────────────────────────

@app.route('/admin/user/<int:target_id>')
@admin_required
def admin_user_detail(target_id):
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    care_requests = (CareRequest.query.filter_by(user_id=target.id)
                     .order_by(CareRequest.created_at.desc()).limit(20).all())
    trust_events = (TrustEvent.query.filter_by(user_id=target.id)
                    .order_by(TrustEvent.timestamp.desc()).limit(20).all())
    transactions = (Transaction.query.filter_by(user_id=target.id)
                    .order_by(Transaction.timestamp.desc()).limit(30).all())
    login_history = (UserLoginHistory.query.filter_by(user_id=target.id)
                     .order_by(UserLoginHistory.timestamp.desc()).limit(10).all())
    try:
        ceiling = round(compute_draw_ceiling(target.id), 2)
    except Exception:
        ceiling = 0.0
    primary_comm = Community.query.get(target.primary_community_id) if target.primary_community_id else None
    all_comms = Community.query.order_by(Community.name).all()
    target_is_global_admin = bool(GlobalAdmin.query.filter_by(user_id=target.id).first())
    admin_role = _get_admin_role(admin_user)
    audit_logs = (AdminAuditLog.query.filter_by(target_user_id=target.id)
                  .order_by(AdminAuditLog.timestamp.desc()).limit(15).all())
    _log_admin_action(session['user_id'], 'view_user_detail', target_user_id=target.id,
                      details=f'Viewed detail panel for {target.name} ({target.phone})')
    return render_template('admin_user_detail.html',
                           user=admin_user, target=target,
                           care_requests=care_requests, trust_events=trust_events,
                           transactions=transactions, login_history=login_history,
                           ceiling=ceiling, primary_comm=primary_comm,
                           all_comms=all_comms, target_is_global_admin=target_is_global_admin,
                           admin_role=admin_role, audit_logs=audit_logs)


@app.route('/admin/user/<int:target_id>/edit', methods=['POST'])
@admin_required
def admin_user_edit(target_id):
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Reason is required for all edits.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))

    changes = []
    new_name = request.form.get('name', '').strip()
    new_phone = request.form.get('phone', '').strip()
    new_pin = request.form.get('pin', '').strip()
    new_comm_id = request.form.get('primary_community_id', '').strip()

    if new_name and new_name != target.name:
        changes.append(f'name: {target.name!r} → {new_name!r}')
        target.name = new_name
    if new_phone and new_phone != target.phone:
        existing = User.query.filter_by(phone=new_phone).first()
        if existing and existing.id != target.id:
            flash('Phone number already in use by another member.', 'error')
            return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
        changes.append(f'phone: {target.phone!r} → {new_phone!r}')
        target.phone = new_phone
    if new_pin:
        if not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
            flash('PIN must be 4–6 digits.', 'error')
            return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
        changes.append('pin: [changed]')
        target.pin = new_pin
    if new_comm_id:
        try:
            cid = int(new_comm_id)
            comm = Community.query.get(cid)
            if comm and cid != target.primary_community_id:
                changes.append(f'community: {target.primary_community_id} → {cid}')
                target.primary_community_id = cid
                if not CommunityMembership.query.filter_by(user_id=target.id, community_id=cid).first():
                    db.session.add(CommunityMembership(user_id=target.id, community_id=cid, role='member'))
        except ValueError:
            pass

    if not changes:
        flash('No changes detected.', 'info')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))

    db.session.commit()
    detail = f'Profile edit — {", ".join(changes)}. Reason: {reason}'
    _log_admin_action(session['user_id'], 'edit_user_profile', target_user_id=target.id, details=detail)
    flash('Profile updated successfully.', 'success')
    return _admin_redirect(url_for('admin_user_detail', target_id=target_id))


@app.route('/admin/user/<int:target_id>/wallet', methods=['POST'])
@admin_required
def admin_user_wallet(target_id):
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Reason is required for balance adjustments.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        flash('Invalid amount.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    direction = request.form.get('direction', 'add')
    if direction == 'deduct':
        amount = -abs(amount)
    else:
        amount = abs(amount)

    old_balance = target.sub_wallet_balance
    target.sub_wallet_balance = max(0.0, (target.sub_wallet_balance or 0.0) + amount)
    tx = Transaction(user_id=target.id, amount=amount, type='admin_adjustment',
                     description=f'Admin wallet adjustment: {reason}')
    db.session.add(tx)
    db.session.commit()
    detail = f'Wallet: UGX {old_balance:,.0f} → UGX {target.sub_wallet_balance:,.0f} ({"+" if amount>=0 else ""}{amount:,.0f}). Reason: {reason}'
    _log_admin_action(session['user_id'], 'adjust_wallet', target_user_id=target.id, details=detail)
    flash(f'Wallet adjusted by UGX {amount:+,.0f}. New balance: UGX {target.sub_wallet_balance:,.0f}', 'success')
    return _admin_redirect(url_for('admin_user_detail', target_id=target_id))


@app.route('/admin/user/<int:target_id>/social-credit', methods=['POST'])
@admin_required
def admin_user_social_credit(target_id):
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Reason is required for social credit adjustments.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        flash('Invalid amount.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    direction = request.form.get('direction', 'add')
    if direction == 'deduct':
        amount = -abs(amount)
    else:
        amount = abs(amount)

    old_credit = target.total_social_credit
    target.total_social_credit = max(0.0, (target.total_social_credit or 0.0) + amount)
    db.session.commit()
    detail = f'Social credit: UGX {old_credit:,.0f} → UGX {target.total_social_credit:,.0f}. Reason: {reason}'
    _log_admin_action(session['user_id'], 'adjust_social_credit', target_user_id=target.id, details=detail)
    flash(f'Social credit adjusted. New balance: UGX {target.total_social_credit:,.0f}', 'success')
    return _admin_redirect(url_for('admin_user_detail', target_id=target_id))


@app.route('/admin/user/<int:target_id>/lock', methods=['POST'])
@admin_required
def admin_user_lock(target_id):
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    reason = request.form.get('reason', '').strip()
    action = request.form.get('action', 'lock')
    if action == 'unlock':
        old = 'locked' if getattr(target, 'is_locked', False) else 'unlocked'
        target.is_locked = False
        target.locked_reason = None
        target.locked_by = None
        target.locked_until = None
        target.failed_login_count = 0
        db.session.commit()
        _log_admin_action(session['user_id'], 'unlock_user', target_user_id=target.id,
                          details=f'User unlocked (permanent + timed). Was: {old}')
        flash(f'{target.name} has been unlocked.', 'success')
    else:
        if not reason:
            flash('Reason is required to lock an account.', 'error')
            return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
        target.is_locked = True
        target.locked_reason = reason
        target.locked_by = admin_user.id
        db.session.commit()
        _log_admin_action(session['user_id'], 'lock_user', target_user_id=target.id,
                          details=f'Account locked. Reason: {reason}')
        flash(f'{target.name}\'s account has been locked.', 'success')
    return _admin_redirect(url_for('admin_user_detail', target_id=target_id))


@app.route('/admin/user/<int:target_id>/pin-reset', methods=['POST'])
@admin_required
@super_admin_required
def admin_user_pin_reset(target_id):
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    reason = request.form.get('reason', '').strip()
    new_pin = request.form.get('new_pin', '').strip()
    if not reason:
        flash('Reason is required to reset a PIN.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    if not new_pin or not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
        flash('New PIN must be 4–6 digits.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    target.pin = new_pin
    db.session.commit()
    _log_admin_action(session['user_id'], 'reset_pin', target_user_id=target.id,
                      details=f'PIN reset by admin. Reason: {reason}')
    flash(f'PIN for {target.name} has been reset.', 'success')
    return _admin_redirect(url_for('admin_user_detail', target_id=target_id))


@app.route('/admin/transaction/<int:tx_id>/reverse', methods=['POST'])
@admin_required
@super_admin_required
def admin_reverse_transaction(tx_id):
    admin_user = User.query.get(session['user_id'])
    tx = Transaction.query.get_or_404(tx_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Reason is required to reverse a transaction.', 'error')
        return redirect(request.referrer or url_for('admin_users'))
    if getattr(tx, 'reversed', False):
        flash('This transaction has already been reversed.', 'error')
        return redirect(request.referrer or url_for('admin_users'))

    user = User.query.get(tx.user_id)
    if user:
        user.sub_wallet_balance = max(0.0, (user.sub_wallet_balance or 0.0) - tx.amount)
    tx.reversed = True
    tx.reversed_by = admin_user.id
    tx.reversed_reason = reason
    tx.reversed_at = datetime.utcnow()
    reversal_tx = Transaction(user_id=tx.user_id, amount=-tx.amount, type='admin_reversal',
                              description=f'Reversal of tx#{tx.id}: {reason}')
    db.session.add(reversal_tx)
    db.session.commit()
    _log_admin_action(session['user_id'], 'reverse_transaction', target_user_id=tx.user_id,
                      old_value=f'amount={tx.amount},type={tx.type}',
                      new_value=f'reversed=True,reason={reason[:80]}',
                      details=f'Reversed tx#{tx.id} amt={tx.amount:,.0f}. Reason: {reason}')
    flash(f'Transaction #{tx.id} reversed successfully.', 'success')
    target_id = request.form.get('target_id')
    if target_id:
        return _admin_redirect(url_for('admin_user_detail', target_id=int(target_id)))
    return redirect(url_for('admin_users'))


# ── Admin: PaymentRecord reversal, hold, and dispute management ────────────────

@app.route('/admin/payment/<int:pr_id>/reverse', methods=['POST'])
@admin_required
@super_admin_required
def admin_reverse_payment(pr_id):
    admin_user = db.session.get(User, session['user_id'])
    pr = PaymentRecord.query.get_or_404(pr_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Reason is required to reverse a payment.', 'error')
        return redirect(request.referrer or url_for('admin_care'))
    if getattr(pr, 'reversed', False):
        flash('This payment has already been reversed.', 'error')
        return redirect(request.referrer or url_for('admin_care'))
    # Compensating ledger entry: restore pool balance
    if pr.community_id:
        community = Community.query.get(pr.community_id)
        if community:
            community.pool_balance = (community.pool_balance or 0.0) + pr.amount
    old_status = pr.status
    pr.reversed = True
    pr.reversed_by = admin_user.id
    pr.reversed_reason = reason
    pr.reversed_at = datetime.utcnow()
    pr.status = 'reversed'
    if getattr(pr, 'dispute_status', None) == 'open':
        pr.dispute_status = 'resolved'
    comp_tx = Transaction(
        user_id=pr.user_id, amount=pr.amount, type='payment_reversal',
        description=f'Reversal of payment #{pr.id} ref={pr.reference_code}: {reason}'
    )
    db.session.add(comp_tx)
    db.session.commit()
    _log_admin_action(session['user_id'], 'reverse_payment', target_user_id=pr.user_id,
                      old_value=f'status={old_status},amount={pr.amount}',
                      new_value=f'reversed=True,reason={reason[:80]}',
                      details=f'Payment #{pr.id} ref={pr.reference_code} amt={pr.amount:,.0f} reversed. Pool restored. Reason: {reason}')
    flash(f'Payment {pr.reference_code} reversed and pool balance restored.', 'success')
    return redirect(request.referrer or url_for('admin_care'))


@app.route('/admin/payment/<int:pr_id>/hold', methods=['POST'])
@admin_required
def admin_hold_payment(pr_id):
    admin_user = db.session.get(User, session['user_id'])
    pr = PaymentRecord.query.get_or_404(pr_id)
    action = request.form.get('action', 'hold')
    reason = request.form.get('reason', '').strip()
    if action == 'release':
        pr.on_hold = False
        pr.on_hold_reason = None
        db.session.commit()
        _log_admin_action(session['user_id'], 'release_payment_hold', target_user_id=pr.user_id,
                          details=f'Hold released on payment #{pr.id} ref={pr.reference_code}')
        flash(f'Hold on payment {pr.reference_code} released.', 'success')
    else:
        if not reason:
            flash('Reason is required to place a hold.', 'error')
            return redirect(request.referrer or url_for('admin_care'))
        pr.on_hold = True
        pr.on_hold_reason = reason
        db.session.commit()
        _log_admin_action(session['user_id'], 'hold_payment', target_user_id=pr.user_id,
                          details=f'Payment #{pr.id} ref={pr.reference_code} placed on hold. Reason: {reason}')
        flash(f'Payment {pr.reference_code} placed on hold.', 'success')
    return redirect(request.referrer or url_for('admin_care'))


@app.route('/report-care/<ref>', methods=['GET', 'POST'])
def file_dispute(ref):
    """Member reports a care quality issue with a provider."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    pr = PaymentRecord.query.filter_by(reference_code=ref).first_or_404()
    if pr.user_id != user.id:
        flash('You can only report care issues on your own payments.', 'error')
        return redirect(url_for('home'))
    if getattr(pr, 'reversed', False):
        flash('This payment record has already been resolved by admin.', 'info')
        return redirect(url_for('home'))
    if getattr(pr, 'dispute_status', None) == 'open':
        flash('A care report is already open for this payment. Admin will be in touch.', 'info')
        return redirect(url_for('home'))
    if request.method == 'POST':
        category = request.form.get('category', 'other').strip()
        note = request.form.get('note', '').strip()
        if not note or len(note) < 10:
            return render_template('file_dispute.html', pr=pr, user=user,
                                   error='Please describe the issue in at least 10 characters.')
        full_note = f'[{category}] {note}'
        pr.dispute_status = 'open'
        pr.dispute_note = full_note[:500]
        pr.dispute_by_user_id = user.id
        pr.dispute_at = datetime.utcnow()
        db.session.commit()
        _log_admin_action(0, 'care_report_filed', target_user_id=user.id,
                          details=f'Payment {ref} — {full_note[:200]}',
                          old_value='none', new_value='open')
        try:
            notify_admin_dispute_filed(
                _get_admin_phones(), ref, user.name, float(pr.amount or 0))
        except Exception:
            pass
        flash('Your report has been submitted. Our admin team will review it within 1–2 business days.', 'success')
        return redirect(url_for('home'))
    return render_template('file_dispute.html', pr=pr, user=user, error=None)

# Keep old URL working
@app.route('/dispute/<ref>', methods=['GET', 'POST'])
def file_dispute_legacy(ref):
    return redirect(url_for('file_dispute', ref=ref))


@app.route('/admin/disputes')
@admin_required
def admin_disputes():
    return redirect(url_for('admin_care'))


@app.route('/admin/dispute/<int:pr_id>/resolve', methods=['POST'])
@admin_required
def admin_dispute_resolve(pr_id):
    pr = PaymentRecord.query.get_or_404(pr_id)
    action = request.form.get('action', 'resolve')
    note   = request.form.get('note', '').strip()

    if action == 'warn':
        # Warn the provider: resolve the report and log a formal warning
        pr.dispute_status = 'resolved'
        warn_text = f'[Admin warning issued to provider]: {note}' if note else '[Admin warning issued to provider]'
        pr.dispute_note = (pr.dispute_note or '') + f'\n{warn_text}'
        db.session.commit()
        # Try to notify provider by SMS
        if pr.provider_id:
            prov = Provider.query.get(pr.provider_id)
            if prov and prov.contact_phone:
                try:
                    from notifications import send_sms
                    send_sms(prov.contact_phone,
                             f'[SolidarityPool] A care quality concern has been reported for your facility. '
                             f'Admin note: {note or "Please ensure all patients receive appropriate care."}')
                except Exception:
                    pass
        _log_admin_action(session['user_id'], 'provider_warned', target_user_id=pr.user_id,
                          details=f'Provider #{pr.provider_id} warned via report on payment {pr.reference_code}. Note: {note[:100]}')
        flash('Provider warning issued and report resolved.', 'success')

    elif action in ('resolve', 'dismiss'):
        pr.dispute_status = 'resolved' if action == 'resolve' else 'dismissed'
        if note:
            pr.dispute_note = (pr.dispute_note or '') + f'\n[Admin {action}]: {note}'
        db.session.commit()
        _log_admin_action(session['user_id'], f'report_{action}', target_user_id=pr.user_id,
                          details=f'Care report on payment {pr.reference_code} {action}d. Note: {note[:100]}')
        flash(f'Report {action}d successfully.', 'success')
    else:
        flash('Unknown action.', 'error')

    return redirect(request.referrer or url_for('admin_care'))


# ── Admin: Provider code management ───────────────────────────────────────────

@app.route('/admin/provider/<int:provider_id>/code', methods=['POST'])
@admin_required
def admin_provider_code(provider_id):
    admin_user = User.query.get(session['user_id'])
    provider = Provider.query.get_or_404(provider_id)
    reason = request.form.get('reason', '').strip()
    mode = request.form.get('mode', 'manual')

    if mode == 'sequential':
        prefix = request.form.get('prefix', '').strip().upper()
        if not prefix:
            flash('Prefix is required for sequential code generation.', 'error')
            return redirect(request.referrer or url_for('admin_verified_providers'))
        new_code = _generate_sequential_code(prefix)
    else:
        new_code = request.form.get('new_code', '').strip().upper()

    import re
    if not new_code or not re.match(r'^[A-Z0-9]{2,20}$', new_code):
        flash('Provider code must be 2–20 alphanumeric characters.', 'error')
        return redirect(request.referrer or url_for('admin_verified_providers'))
    existing = Provider.query.filter_by(provider_code=new_code).first()
    if existing and existing.id != provider.id:
        flash(f'Code {new_code} is already taken by {existing.name}.', 'error')
        return redirect(request.referrer or url_for('admin_verified_providers'))
    if not reason:
        flash('Reason is required to change a provider code.', 'error')
        return redirect(request.referrer or url_for('admin_verified_providers'))

    old_code = provider.provider_code
    hist = ProviderCodeHistory(provider_id=provider.id, old_code=old_code, new_code=new_code,
                               changed_by=admin_user.id, reason=reason)
    db.session.add(hist)
    provider.provider_code = new_code
    db.session.commit()
    _log_admin_action(session['user_id'], 'change_provider_code',
                      details=f'Provider #{provider.id} {provider.name}: {old_code} → {new_code}. Reason: {reason}')
    flash(f'Provider code changed from {old_code} to {new_code}.', 'success')
    return redirect(url_for('admin_verified_providers'))


@app.route('/admin/provider/<int:provider_id>/code-history')
@admin_required
def admin_provider_code_history(provider_id):
    admin_user = User.query.get(session['user_id'])
    provider = Provider.query.get_or_404(provider_id)
    history = (ProviderCodeHistory.query.filter_by(provider_id=provider_id)
               .order_by(ProviderCodeHistory.changed_at.desc()).all())
    for h in history:
        h.admin_user = User.query.get(h.changed_by) if h.changed_by else None
    return render_template('admin_provider_code_history.html',
                           user=admin_user, provider=provider, history=history)


# ── Admin: Provider management hub ────────────────────────────────────────────

@app.route('/admin/providers')
@admin_required
def admin_providers_list():
    admin_user = db.session.get(User, session['user_id'])
    q      = request.args.get('q', '').strip()
    status = request.args.get('status', 'all')

    query = Provider.query
    if q:
        query = query.filter(db.or_(
            Provider.name.ilike(f'%{q}%'),
            Provider.provider_code.ilike(f'%{q}%'),
            Provider.contact_phone.ilike(f'%{q}%'),
            Provider.contact_name.ilike(f'%{q}%'),
        ))
    if status == 'verified':
        query = query.filter_by(verified=True)
    elif status == 'suspended':
        query = query.filter_by(verified=False)

    providers = query.order_by(Provider.created_at.desc()).all()
    for p in providers:
        p._payment_count  = PaymentRecord.query.filter_by(provider_id=p.id).count()
        p._total_paid     = (db.session.query(db.func.sum(PaymentRecord.amount))
                             .filter_by(provider_id=p.id).scalar() or 0)
        p._open_disputes  = (PaymentRecord.query.filter_by(provider_id=p.id)
                             .filter(PaymentRecord.dispute_status == 'open').count())
        p._pending_withdrawals = (ProviderWithdrawal.query
                                  .filter_by(provider_id=p.id, status='pending').count())

    pending_apps = VerifiedProvider.query.filter_by(verification_status='pending').count()
    return render_template('admin_providers_list.html', user=admin_user,
                           providers=providers, q=q, status=status,
                           pending_apps=pending_apps)


@app.route('/admin/provider/<int:provider_id>')
@admin_required
def admin_provider_detail(provider_id):
    admin_user = db.session.get(User, session['user_id'])
    provider   = Provider.query.get_or_404(provider_id)

    payments = (PaymentRecord.query
                .filter_by(provider_id=provider_id)
                .order_by(PaymentRecord.created_at.desc())
                .limit(60).all())
    for p in payments:
        p._user        = db.session.get(User, p.user_id)
        p._care_req    = db.session.get(CareRequest, p.care_request_id) if p.care_request_id else None

    total_payments   = PaymentRecord.query.filter_by(provider_id=provider_id).count()
    total_amount     = (db.session.query(db.func.sum(PaymentRecord.amount))
                        .filter_by(provider_id=provider_id).scalar() or 0)
    open_disputes    = (PaymentRecord.query.filter_by(provider_id=provider_id)
                        .filter(PaymentRecord.dispute_status == 'open').count())
    reversed_count   = PaymentRecord.query.filter_by(provider_id=provider_id, reversed=True).count()
    held_count       = PaymentRecord.query.filter_by(provider_id=provider_id, on_hold=True).count()

    code_history = (ProviderCodeHistory.query
                    .filter_by(provider_id=provider_id)
                    .order_by(ProviderCodeHistory.changed_at.desc())
                    .limit(10).all())
    for h in code_history:
        h._admin = db.session.get(User, h.changed_by) if h.changed_by else None

    vp_app = None
    if provider.contact_phone:
        vp_app = VerifiedProvider.query.filter_by(phone=provider.contact_phone).first()
    if not vp_app:
        vp_app = (VerifiedProvider.query
                  .filter(VerifiedProvider.provider_name.ilike(f'%{provider.name}%'))
                  .first())

    withdrawals = (ProviderWithdrawal.query
                   .filter_by(provider_id=provider_id)
                   .order_by(ProviderWithdrawal.requested_at.desc())
                   .limit(10).all())

    return render_template('admin_provider_detail.html',
                           user=admin_user, provider=provider,
                           payments=payments,
                           total_payments=total_payments,
                           total_amount=total_amount,
                           open_disputes=open_disputes,
                           reversed_count=reversed_count,
                           held_count=held_count,
                           code_history=code_history,
                           vp_app=vp_app,
                           withdrawals=withdrawals)


@app.route('/admin/provider/<int:provider_id>/toggle-verified', methods=['POST'])
@admin_required
@super_admin_required
def admin_provider_toggle_verified(provider_id):
    provider = Provider.query.get_or_404(provider_id)
    action   = request.form.get('action', 'suspend')
    reason   = request.form.get('reason', '').strip()
    if not reason:
        flash('A reason is required.', 'error')
        return _admin_redirect(url_for('admin_provider_detail', provider_id=provider_id))
    if action == 'verify':
        provider.verified = True
        db.session.commit()
        _log_admin_action(session['user_id'], 'provider_verified',
                          details=f'Provider #{provider_id} "{provider.name}" verified. Reason: {reason}')
        flash(f'{provider.name} is now verified and can receive payments.', 'success')
    else:
        provider.verified = False
        db.session.commit()
        _log_admin_action(session['user_id'], 'provider_suspended',
                          details=f'Provider #{provider_id} "{provider.name}" suspended. Reason: {reason}')
        flash(f'{provider.name} has been suspended — they will not receive new payments.', 'success')
    return _admin_redirect(url_for('admin_provider_detail', provider_id=provider_id))


@app.route('/admin/provider/<int:provider_id>/update-contact', methods=['POST'])
@admin_required
def admin_provider_update_contact(provider_id):
    provider = Provider.query.get_or_404(provider_id)
    reason   = request.form.get('reason', '').strip()
    if not reason:
        flash('Reason is required.', 'error')
        return _admin_redirect(url_for('admin_provider_detail', provider_id=provider_id))
    old = (f'contact={provider.contact_name}/{provider.contact_phone}, '
           f'pay={provider.payment_type}/{provider.payment_details}')
    for field, col in [('contact_name', 'contact_name'), ('contact_phone', 'contact_phone'),
                       ('payment_type', 'payment_type'), ('payment_details', 'payment_details')]:
        val = request.form.get(field, '').strip()
        if val:
            setattr(provider, col, val.lstrip('+') if field == 'contact_phone' else val)
    db.session.commit()
    _log_admin_action(session['user_id'], 'provider_contact_updated',
                      details=f'Provider #{provider_id} "{provider.name}": {old} — Reason: {reason}')
    flash(f'Contact/payment details updated for {provider.name}.', 'success')
    return _admin_redirect(url_for('admin_provider_detail', provider_id=provider_id))


# ── Admin: Bulk SMS ────────────────────────────────────────────────────────────

@app.route('/admin/bulk-sms', methods=['GET', 'POST'])
@admin_required
def admin_bulk_sms():
    from loguru import logger
    admin_user = User.query.get(session['user_id'])
    communities = Community.query.order_by(Community.name).all()
    at_configured = bool(os.getenv('AT_USERNAME') and os.getenv('AT_API_KEY'))
    result = None

    if request.method == 'POST':
        message = request.form.get('message', '').strip()
        community_id = request.form.get('community_id', '').strip()
        reason = request.form.get('reason', '').strip()
        if not message:
            flash('Message is required.', 'error')
        elif not reason:
            flash('Reason is required for bulk SMS.', 'error')
        else:
            if community_id:
                memberships = CommunityMembership.query.filter_by(community_id=int(community_id)).all()
                recipients = [User.query.get(m.user_id) for m in memberships]
                recipients = [u for u in recipients if u]
                scope = f'community #{community_id}'
            else:
                recipients = User.query.filter(User.is_locked != True).all()
                scope = 'all members'

            sent = 0
            skipped = 0
            from notifications import _send_sms
            for u in recipients:
                try:
                    _send_sms(u.phone, f'[SolidarityPool] {message}')
                    sent += 1
                except Exception as e:
                    logger.error("Bulk SMS failed for {}: {}", u.phone, e)
                    skipped += 1
            result = {'sent': sent, 'skipped': skipped, 'total': len(recipients)}
            _log_admin_action(session['user_id'], 'bulk_sms',
                              details=f'Bulk SMS to {scope}: {sent}/{len(recipients)} sent. Reason: {reason}. Msg: {message[:80]}')

    return render_template('admin_bulk_sms.html', user=admin_user,
                           communities=communities, at_configured=at_configured, result=result)


# ── Admin: USSD Simulator ─────────────────────────────────────────────────────

@app.route('/admin/ussd-simulator', methods=['GET', 'POST'])
@admin_required
def admin_ussd_simulator():
    admin_user = User.query.get(session['user_id'])
    response_text = None
    session_id = request.form.get('session_id') or f'SIM_{admin_user.id}_{int(datetime.utcnow().timestamp())}'
    phone = request.form.get('phone', '').strip() or '256700000000'
    text = request.form.get('text', '').strip()

    if request.method == 'POST':
        # Call USSD logic directly (no HTTP round-trip — avoids mTLS SSL issues)
        try:
            from ussd import _route as _ussd_route
            steps = text.split('*') if text else ['']
            level = len(steps)
            response_text = _ussd_route(phone, steps, level)
        except Exception as e:
            import traceback
            response_text = f'[Simulator error: {e}]\n{traceback.format_exc()[:400]}'

    return render_template('admin_ussd_simulator.html', user=admin_user,
                           session_id=session_id, phone=phone, text=text,
                           response_text=response_text)


# ── Admin: Enhanced audit log ──────────────────────────────────────────────────

@app.route('/admin/audit-log')
@admin_required
def admin_audit_log():
    admin_user = User.query.get(session['user_id'])
    page = max(1, int(request.args.get('page', 1)))
    per_page = 50
    action_filter = request.args.get('action', '').strip()
    admin_filter = request.args.get('admin_phone', '').strip()
    target_filter = request.args.get('target_phone', '').strip()

    query = AdminAuditLog.query
    if action_filter:
        query = query.filter(AdminAuditLog.action.ilike(f'%{action_filter}%'))
    if admin_filter:
        admin_match = User.query.filter_by(phone=admin_filter).first()
        if admin_match:
            query = query.filter(AdminAuditLog.admin_id == admin_match.id)
        else:
            query = query.filter(AdminAuditLog.admin_id == -1)
    if target_filter:
        target_match = User.query.filter_by(phone=target_filter).first()
        if target_match:
            query = query.filter(AdminAuditLog.target_user_id == target_match.id)
        else:
            query = query.filter(AdminAuditLog.target_user_id == -1)

    total = query.count()
    logs = query.order_by(AdminAuditLog.timestamp.desc()).offset((page - 1) * per_page).limit(per_page).all()
    for log in logs:
        log.admin = User.query.get(log.admin_id)
        log.target_user = User.query.get(log.target_user_id) if log.target_user_id else None
    total_pages = max(1, (total + per_page - 1) // per_page)
    distinct_actions = [r[0] for r in db.session.query(AdminAuditLog.action).distinct().order_by(AdminAuditLog.action).all()]
    return render_template('admin_audit_log.html', user=admin_user, logs=logs,
                           page=page, total_pages=total_pages, total=total,
                           action_filter=action_filter, admin_filter=admin_filter,
                           target_filter=target_filter, distinct_actions=distinct_actions)


@app.route('/admin/export/audit.csv')
@admin_required
def export_audit_csv():
    logs = AdminAuditLog.query.order_by(AdminAuditLog.timestamp.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Timestamp', 'Admin', 'Admin Phone', 'Action', 'Target', 'Target Phone', 'Details', 'IP'])
    for log in logs:
        admin = User.query.get(log.admin_id)
        target = User.query.get(log.target_user_id) if log.target_user_id else None
        w.writerow([
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            admin.name if admin else f'#{log.admin_id}',
            admin.phone if admin else '',
            log.action,
            target.name if target else '',
            target.phone if target else '',
            log.details or '',
            log.ip or '',
        ])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="audit_log.csv"'})


# ── Updated admin_view_user with link to detail panel ─────────────────────────

@app.route('/admin/view-user')
@admin_required
def admin_view_user():
    from trust_graph import compute_draw_ceiling
    admin_user = User.query.get(session['user_id'])
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
    target_is_global_admin = bool(
        target and GlobalAdmin.query.filter_by(user_id=target.id).first()
    )
    return render_template('admin_user_profile.html',
                           user=admin_user, target=target, search_phone=search_phone,
                           not_found=not_found, care_requests=care_requests,
                           trust_events=trust_events, ceiling=ceiling,
                           primary_comm=primary_comm,
                           target_is_global_admin=target_is_global_admin)


# ── Forgot PIN (self-service OTP reset) ────────────────────────────────────────

@app.route('/forgot-pin', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def forgot_pin():
    from notifications import _send_sms
    from loguru import logger

    step = request.args.get('step') or request.form.get('step') or 'request'

    if request.method == 'POST':
        if step == 'request':
            phone = request.form.get('phone', '').strip()
            user = User.query.filter_by(phone=phone).first()
            if not user:
                return render_template('forgot_pin.html', step='request', phone=phone,
                                       error='No account found with that phone number.')
            if getattr(user, 'is_locked', False):
                return render_template('forgot_pin.html', step='request', phone=phone,
                                       error='This account is locked. Contact support.')
            # Generate OTP
            otp = ''.join([str(random.randint(0, 9)) for _ in range(6)])
            expires = datetime.utcnow() + timedelta(minutes=10)
            # Expire old OTPs
            PinResetOTP.query.filter_by(user_id=user.id, used=False).update({'used': True})
            otp_rec = PinResetOTP(user_id=user.id, otp=otp, expires_at=expires)
            db.session.add(otp_rec)
            db.session.commit()
            try:
                _send_sms(phone, f'[SolidarityPool] Your PIN reset code is: {otp}. Valid for 10 minutes. Do not share this.')
            except Exception as e:
                logger.warning("PIN reset SMS failed for {}: {}", phone, e)
            return render_template('forgot_pin.html', step='verify', phone=phone)

        elif step == 'verify':
            phone = request.form.get('phone', '').strip()
            otp_input = request.form.get('otp', '').strip()
            user = User.query.filter_by(phone=phone).first()
            if not user:
                return render_template('forgot_pin.html', step='request',
                                       error='Session expired. Please start again.')
            otp_rec = PinResetOTP.query.filter_by(
                user_id=user.id, otp=otp_input, used=False
            ).order_by(PinResetOTP.created_at.desc()).first()
            if not otp_rec or otp_rec.expires_at < datetime.utcnow():
                return render_template('forgot_pin.html', step='verify', phone=phone,
                                       error='Invalid or expired code. Please try again.')
            # Mark OTP as used and issue a short-lived token
            otp_rec.used = True
            token = ''.join(random.choices(string.ascii_letters + string.digits, k=48))
            otp_rec.token = token
            db.session.commit()
            return render_template('forgot_pin.html', step='set_pin', token=token, phone=phone)

        elif step == 'set_pin':
            token = request.form.get('token', '').strip()
            new_pin = request.form.get('new_pin', '').strip()
            confirm_pin = request.form.get('confirm_pin', '').strip()
            otp_rec = PinResetOTP.query.filter_by(token=token).first()
            if not otp_rec:
                return render_template('forgot_pin.html', step='request',
                                       error='Session expired. Please start again.')
            if not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
                return render_template('forgot_pin.html', step='set_pin', token=token,
                                       error='PIN must be 4–6 digits.')
            if _is_weak_pin(new_pin):
                return render_template('forgot_pin.html', step='set_pin', token=token,
                                       error='That PIN is too easy to guess. Please choose a less predictable PIN.')
            if new_pin != confirm_pin:
                return render_template('forgot_pin.html', step='set_pin', token=token,
                                       error='PINs do not match.')
            user = User.query.get(otp_rec.user_id)
            if not user:
                return render_template('forgot_pin.html', step='request',
                                       error='Account not found. Please start again.')
            user.pin = new_pin
            db.session.commit()
            _log_admin_action(user.id, 'self_pin_reset', target_user_id=user.id,
                              details=f'Self-service PIN reset via SMS OTP for {user.phone}')
            return render_template('forgot_pin.html', step='done')

    return render_template('forgot_pin.html', step='request')


# ── Admin: Settings & Platform Fee Withdrawal ──────────────────────────────────

def _get_setting(key, default=''):
    s = AdminSetting.query.filter_by(key=key).first()
    return s.value if s else default

def _set_setting(key, value, admin_id):
    s = AdminSetting.query.filter_by(key=key).first()
    if s:
        s.value = value
        s.updated_by = admin_id
        s.updated_at = datetime.utcnow()
    else:
        s = AdminSetting(key=key, value=value, updated_by=admin_id)
        db.session.add(s)
    db.session.commit()


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
@super_admin_required
def admin_settings():
    from sqlalchemy import func as sqlfunc
    admin_user = User.query.get(session['user_id'])

    total_revenue = db.session.query(
        sqlfunc.coalesce(sqlfunc.sum(PlatformRevenue.amount), 0.0)
    ).scalar() or 0.0
    total_withdrawn = db.session.query(
        sqlfunc.coalesce(sqlfunc.sum(PlatformWithdrawal.amount), 0.0)
    ).scalar() or 0.0
    available_balance = max(0.0, total_revenue - total_withdrawn)

    recent_withdrawals = PlatformWithdrawal.query\
        .order_by(PlatformWithdrawal.withdrawn_at.desc()).limit(20).all()

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'save_emergency':
            try:
                new_mult_low  = float(request.form.get('emg_mult_low', 1.2))
                new_mult_mid  = float(request.form.get('emg_mult_mid', 1.5))
                new_mult_high = float(request.form.get('emg_mult_high', 2.0))
                new_tier_low  = float(request.form.get('emg_tier_low_max', 0.4))
                new_tier_high = float(request.form.get('emg_tier_high_min', 0.7))
                new_cap       = float(request.form.get('emergency_hard_cap', 200000))
                if not (1.0 <= new_mult_low <= new_mult_mid <= new_mult_high <= 5.0):
                    raise ValueError('Multipliers must be 1.0 ≤ low ≤ mid ≤ high ≤ 5.0')
                if not (0.0 < new_tier_low < new_tier_high < 1.0):
                    raise ValueError('Tier thresholds must be 0 < low < high < 1')
                if new_cap < 10_000 or new_cap > 10_000_000:
                    raise ValueError('Hard cap must be between 10,000 and 10,000,000')
                state = SystemState.query.first()
                if state:
                    state.emg_mult_low       = new_mult_low
                    state.emergency_multiplier = new_mult_mid
                    state.emg_mult_high      = new_mult_high
                    state.emg_tier_low_max   = new_tier_low
                    state.emg_tier_high_min  = new_tier_high
                    state.emergency_hard_cap = new_cap
                    db.session.commit()
                _log_admin_action(admin_user.id, 'update_emergency_settings',
                                  details=(f'tiers: score<{new_tier_low}→{new_mult_low}×, '
                                           f'<{new_tier_high}→{new_mult_mid}×, '
                                           f'≥{new_tier_high}→{new_mult_high}×, '
                                           f'cap=UGX {new_cap:,.0f}'))
                flash('Emergency tier settings saved.', 'success')
            except (ValueError, TypeError) as e:
                flash(f'Invalid value: {e}', 'error')
            return redirect(url_for('admin_settings'))

        if action == 'save_payout':
            payout_method = request.form.get('payout_method', '').strip()
            payout_details = request.form.get('payout_details', '').strip()
            if not payout_method or not payout_details:
                flash('Both payout method and details are required.', 'error')
            else:
                _set_setting('payout_method', payout_method, admin_user.id)
                _set_setting('payout_details', payout_details, admin_user.id)
                _log_admin_action(admin_user.id, 'update_payout_settings',
                                  details=f'Payout method set to {payout_method}: {payout_details[:50]}')
                flash('Payout settings saved.', 'success')
            return redirect(url_for('admin_settings'))

        elif action == 'withdraw':
            try:
                amount = float(request.form.get('amount', 0))
                if amount <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                flash('Please enter a valid withdrawal amount.', 'error')
                return redirect(url_for('admin_settings'))

            if amount > available_balance:
                flash(f'Amount exceeds available balance of UGX {available_balance:,.0f}.', 'error')
                return redirect(url_for('admin_settings'))

            payout_method = _get_setting('payout_method')
            payout_details = _get_setting('payout_details')
            if not payout_method or not payout_details:
                flash('Set payout details before withdrawing.', 'error')
                return redirect(url_for('admin_settings'))

            notes = request.form.get('notes', '').strip()
            pw = PlatformWithdrawal(
                amount=amount,
                payout_method=payout_method,
                payout_details=payout_details,
                notes=notes,
                withdrawn_by=admin_user.id,
            )
            db.session.add(pw)
            db.session.commit()
            _log_admin_action(admin_user.id, 'platform_fee_withdrawal',
                              details=f'Withdrew UGX {amount:,.0f} via {payout_method} to {payout_details[:50]}. Notes: {notes}')
            flash(f'Withdrawal of UGX {amount:,.0f} recorded successfully.', 'success')
            return redirect(url_for('admin_settings'))

    emg = _get_all_emergency_settings()
    return render_template('admin_settings.html',
                           user=admin_user,
                           total_revenue=total_revenue,
                           total_withdrawn=total_withdrawn,
                           available_balance=available_balance,
                           recent_withdrawals=recent_withdrawals,
                           payout_method=_get_setting('payout_method'),
                           payout_details=_get_setting('payout_details'),
                           emg=emg)


# ── Admin: User deactivation (right to be forgotten) ──────────────────────────

@app.route('/admin/user/<int:target_id>/deactivate', methods=['POST'])
@admin_required
def admin_user_deactivate(target_id):
    if not _is_super_admin():
        flash('Only Super Admins can deactivate accounts.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    admin_user = User.query.get(session['user_id'])
    target = User.query.get_or_404(target_id)
    reason = request.form.get('reason', '').strip()
    mode = request.form.get('mode', 'deactivate')

    if not reason:
        flash('Reason is required for deactivation.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))
    if target.id == admin_user.id:
        flash('You cannot deactivate your own account.', 'error')
        return _admin_redirect(url_for('admin_user_detail', target_id=target_id))

    if mode == 'anonymise':
        # GDPR-style: anonymise PII but keep financial records
        original_phone = target.phone
        original_name = target.name
        target.name = f'[Deleted User #{target.id}]'
        target.phone = f'DELETED_{target.id}_{int(datetime.utcnow().timestamp())}'
        target.pin = '0000'
        target.is_active = False
        target.is_locked = True
        target.deactivated_at = datetime.utcnow()
        target.locked_reason = f'Account anonymised per admin request. Reason: {reason}'
        db.session.commit()
        _log_admin_action(session['user_id'], 'anonymise_user', target_user_id=target.id,
                          details=f'PII anonymised for {original_name} ({original_phone}). Reason: {reason}')
        flash(f'Account #{target.id} has been anonymised. PII removed, financial records retained.', 'success')
    else:
        # Simple deactivation (keep data, just block login)
        target.is_active = False
        target.is_locked = True
        target.deactivated_at = datetime.utcnow()
        target.locked_reason = f'Account deactivated by admin. Reason: {reason}'
        db.session.commit()
        _log_admin_action(session['user_id'], 'deactivate_user', target_user_id=target.id,
                          details=f'Account deactivated for {target.name} ({target.phone}). Reason: {reason}')
        flash(f'{target.name}\'s account has been deactivated.', 'success')
    return redirect(url_for('admin_users'))


# ── Support Chat ───────────────────────────────────────────────────────────────

@app.route('/support', methods=['GET', 'POST'])
def support_chat():
    user_id = session.get('user_id')
    user = db.session.get(User, user_id) if user_id else None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'new_ticket':
            subject = request.form.get('subject', '').strip()
            body    = request.form.get('body', '').strip()
            phone   = request.form.get('phone', '').strip() if not user else user.phone
            if not subject or not body:
                return render_template('support_chat.html', user=user,
                                       tickets=_user_tickets(user, phone),
                                       error='Please fill in both the subject and message.')
            # Auto-assign priority based on subject keywords
            subj_lower = subject.lower()
            if any(w in subj_lower for w in ('urgent', 'emergency', 'critical', 'fraud', 'stolen')):
                ticket_priority = 'high'
            elif any(w in subj_lower for w in ('help', 'problem', 'issue', 'error', 'wrong', 'failed')):
                ticket_priority = 'medium'
            else:
                ticket_priority = 'low'
            ticket = SupportTicket(
                user_id=user_id,
                phone=phone or None,
                subject=subject,
                status='open',
                priority=ticket_priority,
            )
            db.session.add(ticket)
            db.session.flush()
            msg = SupportMessage(
                ticket_id=ticket.id,
                sender_type='user',
                sender_id=user_id,
                body=body,
            )
            db.session.add(msg)
            db.session.commit()
            try:
                notify_admin_new_support_ticket(
                    _get_admin_phones(), subject, ticket.id,
                    from_phone=phone or (user.phone if user else ''),
                )
            except Exception:
                pass
            return redirect(url_for('support_ticket_view', ticket_id=ticket.id))

        if action == 'reply':
            ticket_id = request.form.get('ticket_id', type=int)
            body      = request.form.get('body', '').strip()
            ticket    = SupportTicket.query.get_or_404(ticket_id)
            if not _can_access_ticket(ticket, user):
                return redirect(url_for('support_chat'))
            if body:
                db.session.add(SupportMessage(
                    ticket_id=ticket.id,
                    sender_type='user',
                    sender_id=user_id,
                    body=body,
                ))
                ticket.status = 'open'
                ticket.updated_at = datetime.utcnow()
                db.session.commit()
            return redirect(url_for('support_ticket_view', ticket_id=ticket.id))

    phone = (user.phone if user else request.args.get('phone', ''))
    tickets = _user_tickets(user, phone)
    open_ticket = request.args.get('ticket', type=int)
    return render_template('support_chat.html', user=user, tickets=tickets,
                           open_ticket=open_ticket)


@app.route('/support/<int:ticket_id>')
def support_ticket_view(ticket_id):
    user_id = session.get('user_id')
    user    = db.session.get(User, user_id) if user_id else None
    ticket  = SupportTicket.query.get_or_404(ticket_id)
    if not _can_access_ticket(ticket, user):
        return redirect(url_for('support_chat'))
    # Mark admin messages as read
    if user_id:
        for m in ticket.messages:
            if m.sender_type == 'admin' and not m.read_at:
                m.read_at = datetime.utcnow()
        db.session.commit()
    return render_template('support_chat.html', user=user,
                           tickets=_user_tickets(user, ticket.phone),
                           active_ticket=ticket)


def _can_access_ticket(ticket, user):
    if user and ticket.user_id == user.id:
        return True
    if ticket.user_id is None and ticket.phone:
        return True  # anonymous always allowed to view
    return False


def _user_tickets(user, phone=None):
    if user:
        return SupportTicket.query.filter_by(user_id=user.id).order_by(
            SupportTicket.updated_at.desc()).all()
    if phone:
        return SupportTicket.query.filter_by(phone=phone, user_id=None).order_by(
            SupportTicket.updated_at.desc()).all()
    return []


# ── Admin: Support inbox ────────────────────────────────────────────────────────

@app.route('/admin/support')
@admin_required
@roles_required('super_admin', 'support')
def admin_support():
    status_filter = request.args.get('status', 'open')
    q = SupportTicket.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    tickets = q.order_by(SupportTicket.updated_at.desc()).all()
    unread  = _admin_support_unread_count()
    return render_template('admin_support.html', tickets=tickets,
                           status_filter=status_filter, unread=unread)


@app.route('/admin/support/<int:ticket_id>', methods=['GET', 'POST'])
@admin_required
def admin_support_ticket(ticket_id):
    ticket = SupportTicket.query.get_or_404(ticket_id)
    if request.method == 'POST':
        action = request.form.get('action', '')
        admin_uid = session.get('user_id')
        if action == 'reply':
            body = request.form.get('body', '').strip()
            if body:
                db.session.add(SupportMessage(
                    ticket_id=ticket.id,
                    sender_type='admin',
                    sender_id=admin_uid,
                    body=body,
                ))
                old_s = ticket.status
                ticket.status = 'pending'
                ticket.updated_at = datetime.utcnow()
                db.session.commit()
                _log_admin_action(admin_uid, 'support_ticket_replied',
                                  target_user_id=ticket.user_id,
                                  details=f'Ticket #{ticket.id}: {body[:150]}',
                                  old_value=old_s, new_value='pending')
        elif action == 'close':
            old_s = ticket.status
            ticket.status = 'closed'
            ticket.updated_at = datetime.utcnow()
            db.session.commit()
            _log_admin_action(admin_uid, 'support_ticket_closed',
                              target_user_id=ticket.user_id,
                              details=f'Ticket #{ticket.id}: {ticket.subject[:100]}',
                              old_value=old_s, new_value='closed')
        elif action == 'reopen':
            old_s = ticket.status
            ticket.status = 'open'
            ticket.updated_at = datetime.utcnow()
            db.session.commit()
            _log_admin_action(admin_uid, 'support_ticket_reopened',
                              target_user_id=ticket.user_id,
                              details=f'Ticket #{ticket.id}: {ticket.subject[:100]}',
                              old_value=old_s, new_value='open')
        elif action == 'assign':
            assignee_id = request.form.get('assignee_id', type=int)
            old_assignee = ticket.assigned_to
            ticket.assigned_to = assignee_id
            ticket.updated_at = datetime.utcnow()
            db.session.commit()
            _log_admin_action(admin_uid, 'support_ticket_assigned',
                              target_user_id=ticket.user_id,
                              details=f'Ticket #{ticket.id} assigned to admin #{assignee_id}',
                              old_value=str(old_assignee), new_value=str(assignee_id))
        elif action == 'set_priority':
            priority = request.form.get('priority', 'medium')
            if priority in ('low', 'medium', 'high'):
                old_p = getattr(ticket, 'priority', 'medium')
                ticket.priority = priority
                ticket.updated_at = datetime.utcnow()
                db.session.commit()
                _log_admin_action(admin_uid, 'support_ticket_priority_changed',
                                  target_user_id=ticket.user_id,
                                  details=f'Ticket #{ticket.id}: priority changed',
                                  old_value=old_p, new_value=priority)
        return redirect(url_for('admin_support_ticket', ticket_id=ticket.id))

    # Mark user messages as read
    for m in ticket.messages:
        if m.sender_type == 'user' and not m.read_at:
            m.read_at = datetime.utcnow()
    db.session.commit()
    unread = _admin_support_unread_count()
    return render_template('admin_support.html', active_ticket=ticket,
                           tickets=SupportTicket.query.order_by(
                               SupportTicket.updated_at.desc()).limit(30).all(),
                           status_filter='all', unread=unread)


def _admin_support_unread_count():
    return SupportMessage.query.filter(
        SupportMessage.sender_type == 'user',
        SupportMessage.read_at == None
    ).count()


# ── Admin: Monitor live JSON feed ──────────────────────────────────────────────

@app.route('/admin/pending-counts')
@admin_required
def admin_pending_counts_api():
    """JSON endpoint polled every 60 s by the admin nav to refresh badge counts."""
    return jsonify(_admin_pending_counts())


@app.route('/admin/monitor/data')
@admin_required
def admin_monitor_data():
    from sqlalchemy import func
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    return jsonify({
        'total_users': User.query.count(),
        'locked_users': User.query.filter(User.is_locked == True).count(),
        'pending_care': CareRequest.query.filter(CareRequest.status.in_(['pending_witness', 'pending_admin'])).count(),
        'fraud_count': FraudAlert.query.filter_by(resolved=False).count(),
        'failed_logins_1h': UserLoginHistory.query.filter(
            UserLoginHistory.success == False,
            UserLoginHistory.timestamp >= one_hour_ago
        ).count(),
        'ts': datetime.utcnow().isoformat(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
