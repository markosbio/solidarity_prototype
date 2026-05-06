from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(100))
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
    
    user = db.relationship('User', foreign_keys=[user_id])
    provider = db.relationship('Provider')
    admin = db.relationship('User', foreign_keys=[admin_id])

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
