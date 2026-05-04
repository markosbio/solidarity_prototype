from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


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

    # Upgrade 4: Smarter witness selection fields
    witness_accuracy_score = db.Column(db.Float, default=0.5)
    region_prefix = db.Column(db.String(10), default='')
    total_witness_calls = db.Column(db.Integer, default=0)
    correct_witness_calls = db.Column(db.Integer, default=0)

    recruits = db.relationship('User', backref=db.backref('referrer', remote_side=[id]))


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float)
    type = db.Column(db.String(20))
    description = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class WitnessRequest(db.Model):
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
