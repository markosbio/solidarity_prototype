"""
Trust Score Reputation Engine.

Four weighted factors → combined score [0.10, 1.00]:
  Repayment health   40%   round-up contributions vs. social credit drawn
  Witness reliability25%   witness_accuracy_score
  Network quality    20%   weighted mean trust of depth-1 recruits
  Activity           15%   recency-weighted round-up frequency

get_combined_score()        — read-only, used by trust_graph.py
recompute_trust_score()     — read + write (persists score + TrustEvent audit row)
simulate_ceiling_preview()  — hypothetical deltas for /api/ceiling_preview
"""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from models import db, User, Transaction, TrustEvent

WEIGHT_REPAYMENT = 0.40
WEIGHT_WITNESS   = 0.25
WEIGHT_NETWORK   = 0.20
WEIGHT_ACTIVITY  = 0.15

SCORE_MIN = 0.10
SCORE_MAX = 1.00


class TrustEngineError(Exception):
    pass


# ── Factor calculators ────────────────────────────────────────────────────────

def _repayment_factor(user: User) -> float:
    total_roundups = (
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount), 0.0))
        .filter(Transaction.user_id == user.id,
                Transaction.type.in_(['roundup', 'mpesa_roundup']))
        .scalar()
    )
    credit = user.total_social_credit or 0.0
    if credit <= 0:
        return 0.80
    return min(total_roundups / credit, 1.0)


def _witness_factor(user: User) -> float:
    return float(user.witness_accuracy_score or 0.5)


def _network_factor(user: User) -> float:
    recruits = user.recruits
    if not recruits:
        return 0.50
    weighted_sum = 0.0
    weight_total = 0.0
    for r in recruits:
        w = 0.5 + (_repayment_factor(r) * 0.5)
        weighted_sum += r.trust_score * w
        weight_total += w
    return weighted_sum / weight_total if weight_total > 0 else 0.5


def _activity_factor(user: User) -> float:
    cutoff = datetime.utcnow() - timedelta(days=30)
    all_time: int = (
        db.session.query(db.func.count(Transaction.id))
        .filter(Transaction.user_id == user.id,
                Transaction.type.in_(['roundup', 'mpesa_roundup']))
        .scalar()
    ) or 0
    recent: int = (
        db.session.query(db.func.count(Transaction.id))
        .filter(Transaction.user_id == user.id,
                Transaction.type.in_(['roundup', 'mpesa_roundup']),
                Transaction.timestamp >= cutoff)
        .scalar()
    ) or 0
    numerator = recent * 2 + all_time
    denominator = all_time + 3
    return min(numerator / denominator, 1.0)


def _raw_score(f_r: float, f_w: float, f_n: float, f_a: float) -> float:
    raw = (WEIGHT_REPAYMENT * f_r + WEIGHT_WITNESS * f_w
           + WEIGHT_NETWORK * f_n + WEIGHT_ACTIVITY * f_a)
    return round(max(SCORE_MIN, min(SCORE_MAX, raw)), 4)


# ── Read-only combined score ──────────────────────────────────────────────────

def get_combined_score(user_id: int) -> float:
    """Compute the multi-factor score without writing. Safe to call anywhere."""
    try:
        user = User.query.get(user_id)
        if not user:
            return SCORE_MIN
        return _raw_score(
            _repayment_factor(user),
            _witness_factor(user),
            _network_factor(user),
            _activity_factor(user),
        )
    except Exception as exc:
        logger.error("get_combined_score failed for user_id={}: {}", user_id, exc)
        return SCORE_MIN


# ── Persist + audit ───────────────────────────────────────────────────────────

def recompute_trust_score(user_id: int, reason: str = 'auto') -> float:
    """Recompute, persist, and log a TrustEvent for user_id."""
    try:
        user = User.query.get(user_id)
        if not user:
            raise TrustEngineError(f"User with id={user_id} not found")

        old_score = round(float(user.trust_score), 4)
        f_r = _repayment_factor(user)
        f_w = _witness_factor(user)
        f_n = _network_factor(user)
        f_a = _activity_factor(user)

        new_score = _raw_score(f_r, f_w, f_n, f_a)
        user.trust_score = new_score

        db.session.add(TrustEvent(
            user_id=user_id,
            old_score=old_score,
            new_score=new_score,
            delta=round(new_score - old_score, 4),
            reason=reason,
            f_repayment=round(f_r, 4),
            f_witness=round(f_w, 4),
            f_network=round(f_n, 4),
            f_activity=round(f_a, 4),
        ))
        db.session.commit()

        logger.info("Trust score: user_id={} {} → {} ({})", user_id, old_score, new_score, reason)
        return new_score

    except TrustEngineError:
        raise
    except Exception as exc:
        logger.error("recompute_trust_score failed for user_id={}: {}", user_id, exc)
        raise TrustEngineError(f"Trust score computation failed: {exc}") from exc


# ── Ceiling preview simulation ────────────────────────────────────────────────

def simulate_ceiling_preview(user_id: int) -> dict:
    """
    Return how much the draw ceiling would change with:
      - one more round-up contribution (boosts activity factor)
      - one more recruit at neutral trust (0.50) (boosts network factor + recruit bonus)

    No DB writes. Used by GET /api/ceiling_preview.
    """
    from models import SystemState, WitnessRequest
    from trust_graph import _pool_health_factor, _recent_verified_boost

    user = User.query.get(user_id)
    if not user:
        return {}

    # ── Current factors ───────────────────────────────────────────────────────
    f_r = _repayment_factor(user)
    f_w = _witness_factor(user)
    f_n = _network_factor(user)
    f_a = _activity_factor(user)

    current_score = _raw_score(f_r, f_w, f_n, f_a)

    state = SystemState.query.first()
    pool = state.communal_pool_balance if state else 5000.0
    health = _pool_health_factor(pool)
    w_boost = _recent_verified_boost(user_id)

    recruit_bonus = sum(get_combined_score(r.id) * 20.0 * 0.8 for r in user.recruits)
    current_ceiling = round(min((current_score * 100.0 + recruit_bonus) * w_boost * health, 500.0), 2)

    # ── Simulate +1 roundup ───────────────────────────────────────────────────
    cutoff = datetime.utcnow() - timedelta(days=30)
    all_time = (db.session.query(db.func.count(Transaction.id))
                .filter(Transaction.user_id == user_id,
                        Transaction.type.in_(['roundup', 'mpesa_roundup']))
                .scalar()) or 0
    recent = (db.session.query(db.func.count(Transaction.id))
              .filter(Transaction.user_id == user_id,
                      Transaction.type.in_(['roundup', 'mpesa_roundup']),
                      Transaction.timestamp >= cutoff)
              .scalar()) or 0

    sim_a = min(((recent + 1) * 2 + (all_time + 1)) / (all_time + 1 + 3), 1.0)
    sim_score_ru = _raw_score(f_r, f_w, f_n, sim_a)
    sim_ceiling_ru = round(
        min((sim_score_ru * 100.0 + recruit_bonus) * w_boost * health, 500.0), 2)

    # ── Simulate +1 recruit at neutral score (0.5) ────────────────────────────
    neutral = 0.50
    recruits = user.recruits
    if recruits:
        cur_ws = sum(r.trust_score * (0.5 + _repayment_factor(r) * 0.5) for r in recruits)
        cur_wt = sum(0.5 + _repayment_factor(r) * 0.5 for r in recruits)
        extra_w = 0.75   # neutral recruit: repayment_factor ≈ 0.5 → weight = 0.75
        sim_n = min((cur_ws + neutral * extra_w) / (cur_wt + extra_w), 1.0)
    else:
        sim_n = neutral * 0.5   # first recruit shifts from 0.5 baseline

    sim_score_rec = _raw_score(f_r, f_w, sim_n, f_a)
    sim_recruit_bonus = recruit_bonus + neutral * 20.0 * 0.8
    sim_ceiling_rec = round(
        min((sim_score_rec * 100.0 + sim_recruit_bonus) * w_boost * health, 500.0), 2)

    return {
        'current_score': current_score,
        'current_ceiling': current_ceiling,
        'factors': {
            'repayment': round(f_r, 4),
            'witness':   round(f_w, 4),
            'network':   round(f_n, 4),
            'activity':  round(f_a, 4),
        },
        'if_one_more_roundup': {
            'score':         sim_score_ru,
            'ceiling':       sim_ceiling_ru,
            'score_delta':   round(sim_score_ru   - current_score,   4),
            'ceiling_delta': round(sim_ceiling_ru - current_ceiling, 2),
        },
        'if_one_more_recruit': {
            'score':         sim_score_rec,
            'ceiling':       sim_ceiling_rec,
            'score_delta':   round(sim_score_rec  - current_score,   4),
            'ceiling_delta': round(sim_ceiling_rec - current_ceiling, 2),
        },
    }
