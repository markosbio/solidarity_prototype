from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import random, string

db = SQLAlchemy()


def _make_invite_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    sub_wallet_balance = db.Column(db.Float, default=0.0)
    trust_score = db.Column(db.Float, default=0.5)
    total_social_credit = db.Column(db.Float, default=0.0)
    roundup_intensifier = db.Column(db.Float, default=1.0)
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    recruitment_freshness = db.Column(db.DateTime, default=datetime.utcnow)
    witness_accuracy_score = db.Column(db.Float, default=0.5)
    region_prefix = db.Column(db.String(10), default='')
    total_witness_calls = db.Column(db.Integer, default=0)
    correct_witness_calls = db.Column(db.Integer, default=0)
    # Community & admin fields (new columns, nullable for migration safety)
    primary_community_id = db.Column(db.Integer, nullable=True)
    is_global_admin = db.Column(db.Boolean, default=False)

    recruits = db.relationship('User', backref=db.backref('referrer', remote_side=[id]))


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float)
    type = db.Column(db.String(20))
    description = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class WitnessRequest(db.Model):
    """Legacy model — kept for backward compat. New flows use CareRequest."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    needed_amount = db.Column(db.Float)
    provider_id = db.Column(db.String(50))
    from_sub = db.Column(db.Float)
    from_pool = db.Column(db.Float)
    social_credit = db.Column(db.Float)
    status = db.Column(db.String(20))
    witness_ids = db.Column(db.String(200), default='')
    votes = db.Column(db.String(500), default='')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class SystemState(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    communal_pool_balance = db.Column(db.Float, default=0.0)


class TrustEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    old_score = db.Column(db.Float, nullable=False)
    new_score = db.Column(db.Float, nullable=False)
    delta = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(100), default='auto')
    f_repayment = db.Column(db.Float)
    f_witness = db.Column(db.Float)
    f_network = db.Column(db.Float)
    f_activity = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class MpesaTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    checkout_request_id = db.Column(db.String(100), unique=True)
    merchant_request_id = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    amount = db.Column(db.Float)
    purpose = db.Column(db.String(50))
    status = db.Column(db.String(20), default='pending')
    mpesa_receipt = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# ── Phase 1: Provider Registry ─────────────────────────────────────────────────

class Provider(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    provider_code = db.Column(db.String(50), unique=True, nullable=False)
    payment_type = db.Column(db.String(50))       # mpesa, bank, till, paybill
    payment_details = db.Column(db.String(200))   # merchant number, account, etc.
    verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CareRequest(db.Model):
    """Primary care-request model. Replaces WitnessRequest for all new flows."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'), nullable=False)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=True)
    amount_needed = db.Column(db.Float, nullable=False)
    amount_from_sub = db.Column(db.Float, default=0.0)
    amount_from_pool = db.Column(db.Float, default=0.0)
    social_credit = db.Column(db.Float, default=0.0)
    # Status lifecycle:
    # pending → witness_approved → admin_approved → paid → completed
    # pending → flagged (witnesses reject)
    # pending → admin_approved (emergency, skip admin wait)
    status = db.Column(db.String(30), default='pending')
    witness_ids = db.Column(db.String(200), default='')
    witness_votes = db.Column(db.String(500), default='')
    witness_approved = db.Column(db.Boolean, default=False)
    admin_approved = db.Column(db.Boolean, default=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    is_emergency = db.Column(db.Boolean, default=False)
    payment_transaction_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    requester = db.relationship('User', foreign_keys=[user_id], backref='care_requests')
    provider = db.relationship('Provider', backref='care_requests')
    approver = db.relationship('User', foreign_keys=[admin_id])


# ── Phase 2: Community Pods ────────────────────────────────────────────────────

class Community(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(200))
    pool_balance = db.Column(db.Float, default=0.0)
    admin_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    invite_code = db.Column(db.String(20), unique=True, default=_make_invite_code)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship('User', foreign_keys=[admin_user_id])
    memberships = db.relationship('CommunityMembership', backref='community',
                                  cascade='all, delete-orphan')


class CommunityMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'), nullable=False)
    role = db.Column(db.String(20), default='member')  # member, admin, coadmin
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='memberships')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'community_id', name='uq_user_community'),
    )
