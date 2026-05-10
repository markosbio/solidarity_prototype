from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(100))
    pin = db.Column(db.String(10), default='1234')
    is_admin = db.Column(db.Boolean, default=False)
    sub_wallet_balance = db.Column(db.Float, default=0.0)
    trust_score = db.Column(db.Float, default=0.5)
    total_social_credit = db.Column(db.Float, default=0.0)
    roundup_intensifier = db.Column(db.Float, default=1.0)
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    recruitment_freshness = db.Column(db.DateTime, default=datetime.utcnow)
    primary_community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=True)
    witness_accuracy_score = db.Column(db.Float, default=0.5)
    region_prefix = db.Column(db.String(10), default='')
    total_witness_calls = db.Column(db.Integer, default=0)
    correct_witness_calls = db.Column(db.Integer, default=0)
    is_global_admin = db.Column(db.Boolean, default=False)

    recruits = db.relationship('User', backref=db.backref('referrer', remote_side=[id]))


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float)
    type = db.Column(db.String(20))
    description = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class Community(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(200))
    pool_balance = db.Column(db.Float, default=0.0)
    invite_code = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    admin_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))

    # Pool health protection fields
    pool_target = db.Column(db.Float, default=2_000_000.0)
    ceiling_multiplier = db.Column(db.Float, default=1.0)
    witness_strictness = db.Column(db.String(10), default='normal')
    large_withdrawal_paused = db.Column(db.Boolean, default=False)

    admin = db.relationship('User', foreign_keys=[admin_user_id], backref='admin_communities')
    members = db.relationship('CommunityMembership', back_populates='community', cascade='all, delete-orphan')
    care_requests = db.relationship('CareRequest', backref='community', lazy=True)


class CommunityMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    role = db.Column(db.String(20), default='member')
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='community_memberships')
    community = db.relationship('Community', back_populates='members')


class Provider(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    provider_code = db.Column(db.String(50), unique=True, nullable=False)
    payment_type = db.Column(db.String(50))
    payment_details = db.Column(db.String(200))
    verified = db.Column(db.Boolean, default=False)
    contact_name = db.Column(db.String(100), nullable=True)
    contact_phone = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class VerifiedProvider(db.Model):
    """Full provider verification applications with document tracking."""
    id = db.Column(db.Integer, primary_key=True)
    provider_name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    business_license = db.Column(db.String(100))
    location = db.Column(db.String(200))
    verification_status = db.Column(db.String(20), default='pending')
    provider_wallet_number = db.Column(db.String(50))
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    review_notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    reviewer = db.relationship('User', foreign_keys=[reviewed_by])


class CareRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'))
    amount_needed = db.Column(db.Float)
    amount_from_sub = db.Column(db.Float, default=0)
    amount_from_pool = db.Column(db.Float, default=0)
    social_credit = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='pending_witness')
    is_emergency = db.Column(db.Boolean, default=False)
    witness_votes = db.Column(db.String(500), default='')
    witness_ids = db.Column(db.String(200), default='')
    admin_approved = db.Column(db.Boolean, default=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    payment_transaction_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Fraud scoring fields
    fraud_score = db.Column(db.Float, default=0.0)
    fraud_flagged = db.Column(db.Boolean, default=False)
    fraud_reasons = db.Column(db.String(500), default='')

    user = db.relationship('User', foreign_keys=[user_id])
    provider = db.relationship('Provider')
    admin = db.relationship('User', foreign_keys=[admin_id])


class FraudAlert(db.Model):
    """Log of all fraud score events for audit and review."""
    id = db.Column(db.Integer, primary_key=True)
    care_request_id = db.Column(db.Integer, db.ForeignKey('care_request.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    fraud_score = db.Column(db.Float, nullable=False)
    triggers = db.Column(db.String(1000))
    resolved = db.Column(db.Boolean, default=False)
    resolved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])
    resolver = db.relationship('User', foreign_keys=[resolved_by])
    care_request = db.relationship('CareRequest', backref='fraud_alerts')


class PaymentRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference_code = db.Column(db.String(50), unique=True, nullable=False)
    care_request_id = db.Column(db.Integer, db.ForeignKey('care_request.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='sent')
    provider_confirmed_at = db.Column(db.DateTime, nullable=True)
    treatment_started_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    care_request = db.relationship('CareRequest', backref='payments', foreign_keys=[care_request_id])
    user = db.relationship('User', backref='payments', foreign_keys=[user_id])
    provider = db.relationship('Provider', backref='payments', foreign_keys=[provider_id])
    community = db.relationship('Community', backref='payments', foreign_keys=[community_id])


class TrustEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    old_score = db.Column(db.Float)
    new_score = db.Column(db.Float)
    delta = db.Column(db.Float)
    reason = db.Column(db.String(100))
    factors = db.Column(db.String(500))
    f_repayment = db.Column(db.Float, nullable=True)
    f_witness = db.Column(db.Float, nullable=True)
    f_network = db.Column(db.Float, nullable=True)
    f_activity = db.Column(db.Float, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class SystemState(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    communal_pool_balance = db.Column(db.Float, default=0.0)
    solidarity_percent = db.Column(db.Float, default=8.0)


class WitnessRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    needed_amount = db.Column(db.Float)
    provider_id = db.Column(db.String(100))
    from_sub = db.Column(db.Float, default=0.0)
    from_pool = db.Column(db.Float, default=0.0)
    social_credit = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='pending')
    witness_ids = db.Column(db.String(200), default='')
    votes = db.Column(db.String(500), default='')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='witness_requests')


class MobileMoneyTransaction(db.Model):
    """Records every mobile money fee-based solidarity contribution."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    normal_fee = db.Column(db.Float, nullable=False)
    solidarity_amount = db.Column(db.Float, nullable=False)
    to_wallet = db.Column(db.Float, nullable=False)
    to_pool = db.Column(db.Float, nullable=False)
    to_platform = db.Column(db.Float, nullable=False)
    receipt_id = db.Column(db.String(100), unique=True, nullable=False)
    network = db.Column(db.String(20), default='unknown')
    processed = db.Column(db.Boolean, default=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='mobile_money_transactions')


class PlatformRevenue(db.Model):
    """Records every platform fee collected from solidarity contributions."""
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(50), nullable=False, default='solidarity_fee')
    transaction_id = db.Column(db.Integer, db.ForeignKey('transaction.id'), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class MpesaTopup(db.Model):
    """Tracks a pending STK Push top-up until Safaricom confirms it."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    checkout_request_id = db.Column(db.String(100), unique=True, nullable=False)
    merchant_request_id = db.Column(db.String(100))
    status = db.Column(db.String(20), default='pending')
    mpesa_receipt = db.Column(db.String(50))
    result_desc = db.Column(db.String(200))
    initiated_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime)

    user = db.relationship('User', backref='mpesa_topups')


class AdminAuditLog(db.Model):
    """Log of all admin actions for audit and accountability."""
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(500))
    ip = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship('User', foreign_keys=[admin_id], backref='audit_actions')
    target_user = db.relationship('User', foreign_keys=[target_user_id], backref='audit_logs')
