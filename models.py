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
    
    recruits = db.relationship('User', backref=db.backref('referrer', remote_side=[id]))

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float)
    type = db.Column(db.String(20))  # roundup, draw, repayment
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

class CommunityMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    community_id = db.Column(db.Integer, db.ForeignKey('community.id'))
    role = db.Column(db.String(20), default='member')  # member, admin, coadmin
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
    status = db.Column(db.String(20), default='pending')  # pending_witness, pending_admin, admin_approved, paid, rejected
    is_emergency = db.Column(db.Boolean, default=False)
    witness_votes = db.Column(db.String(500), default='')
    witness_ids = db.Column(db.String(200), default='')   # comma-separated user IDs
    admin_approved = db.Column(db.Boolean, default=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    payment_transaction_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = db.relationship('User', foreign_keys=[user_id])
    community = db.relationship('Community')
    provider = db.relationship('Provider')
    admin = db.relationship('User', foreign_keys=[admin_id])

# For global system state (fallback, deprecated but kept for compatibility)
class SystemState(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    communal_pool_balance = db.Column(db.Float, default=0.0)
