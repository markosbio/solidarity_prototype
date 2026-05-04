from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(100))
    sub_wallet_balance = db.Column(db.Float, default=0.0)
    trust_score = db.Column(db.Float, default=0.5)  # initial neutral
    total_social_credit = db.Column(db.Float, default=0.0)
    roundup_intensifier = db.Column(db.Float, default=1.0)  # adaptive recovery
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    recruitment_freshness = db.Column(db.DateTime, default=datetime.utcnow)
    
    recruits = db.relationship('User', backref=db.backref('referrer', remote_side=[id]))

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float)
    type = db.Column(db.String(20))  # roundup, draw, repayment
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
    status = db.Column(db.String(20))  # pending, verified, flagged
    witness_ids = db.Column(db.String(200))  # comma-separated
    votes = db.Column(db.String(500), default='')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class SystemState(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    communal_pool_balance = db.Column(db.Float, default=0.0)
