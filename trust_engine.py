"""
Trust Score Reputation Engine.

Recomputes a user's trust_score from four weighted factors:

  Factor                  Weight  Source
  ──────────────────────  ──────  ──────────────────────────────────────────────
  Repayment health          40%   ratio of lifetime round-up contributions
                                  to total social credit drawn
  Witness reliability       25%   witness_accuracy_score (updated by witness.py)
  Network quality           20%   mean trust score of depth-1 recruits,
                                  decayed if recruits themselves have low scores
  Activity                  15%   recency-weighted round-up frequency
                                  (# roundups in last 30 days vs. all-time)

Score is clamped to [0.10, 1.00] and rounded to 4 decimal places.

A TrustEvent audit row is written every time the score changes, recording the
old score, new score, delta, and the triggering reason.
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
    """
    Ratio of total round-up contributions to total social credit drawn.
    No social credit → healthy baseline of 0.8.
    Perfect repayment (contributions >= credit) → 1.0.
    Partial repayment → linear interpolation, floored at 0.0.
    """
    total_roundups = (
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount), 0.0))
        .filter(Transaction.user_id == user.id, Transaction.type.in_(['roundup', 'mpesa_roundup']))
        .scalar()
    )
    credit = user.total_social_credit or 0.0

    if credit <= 0:
        return 0.80  # no debt; modest baseline (not perfect — haven't been tested yet)

    ratio = total_roundups / credit
    return min(ratio, 1.0)


def _witness_factor(user: User) -> float:
    """Witness accuracy score is already normalised to [0, 1]."""
    return float(user.witness_accuracy_score or 0.5)


def _network_factor(user: User) -> float:
    """
    Mean trust score of direct recruits, weighted by their own repayment health.
    Returns 0.5 (neutral) when the user has no recruits.
    """
    recruits = user.recruits
    if not recruits:
        return 0.50

    weighted_sum = 0.0
    weight_total = 0.0
    for r in recruits:
        # Recruits with higher repayment health contribute more
        recruit_repayment = _repayment_factor(r)
        w = 0.5 + (recruit_repayment * 0.5)  # weight in [0.5, 1.0]
        weighted_sum += r.trust_score * w
        weight_total += w

    return weighted_sum / weight_total if weight_total > 0 else 0.5


def _activity_factor(user: User) -> float:
    """
    Recency-weighted round-up frequency.

    score = (roundups_last_30_days * 2 + roundups_all_time) / (2 + roundups_all_time + 1)

    Rationale: recent activity is worth twice as much as old activity.
    A brand-new user with 0 roundups gets 0.0; a very active user trends toward 1.0.
    Soft cap at 1.0.
    """
    cutoff = datetime.utcnow() - timedelta(days=30)

    all_time: int = (
        db.session.query(db.func.count(Transaction.id))
        .filter(Transaction.user_id == user.id, Transaction.type.in_(['roundup', 'mpesa_roundup']))
        .scalar()
    ) or 0

    recent: int = (
        db.session.query(db.func.count(Transaction.id))
        .filter(
            Transaction.user_id == user.id,
            Transaction.type.in_(['roundup', 'mpesa_roundup']),
            Transaction.timestamp >= cutoff,
        )
        .scalar()
    ) or 0

    numerator = recent * 2 + all_time
    denominator = all_time + 3  # +3 keeps new users from dividing near-zero
    return min(numerator / denominator, 1.0)


# ── Main entry point ──────────────────────────────────────────────────────────

def recompute_trust_score(user_id: int, reason: str = 'auto') -> float:
    """
    Recompute and persist the trust score for `user_id`.

    Args:
        user_id: Primary key of the user to update.
        reason:  Human-readable label for the triggering event, stored in TrustEvent.

    Returns:
        The new trust score (float).

    Raises:
        TrustEngineError: if the user is not found or a database error occurs.
    """
    try:
        user = User.query.get(user_id)
        if not user:
            raise TrustEngineError(f"User with id={user_id} not found")

        old_score = round(float(user.trust_score), 4)

        f_repayment = _repayment_factor(user)
        f_witness   = _witness_factor(user)
        f_network   = _network_factor(user)
        f_activity  = _activity_factor(user)

        raw = (
            WEIGHT_REPAYMENT * f_repayment
            + WEIGHT_WITNESS  * f_witness
            + WEIGHT_NETWORK  * f_network
            + WEIGHT_ACTIVITY * f_activity
        )
        new_score = round(max(SCORE_MIN, min(SCORE_MAX, raw)), 4)

        user.trust_score = new_score

        event = TrustEvent(
            user_id=user_id,
            old_score=old_score,
            new_score=new_score,
            delta=round(new_score - old_score, 4),
            reason=reason,
            f_repayment=round(f_repayment, 4),
            f_witness=round(f_witness, 4),
            f_network=round(f_network, 4),
            f_activity=round(f_activity, 4),
        )
        db.session.add(event)
        db.session.commit()

        logger.info(
            "Trust score updated: user_id={} {} → {} (reason={}, "
            "repayment={:.3f} witness={:.3f} network={:.3f} activity={:.3f})",
            user_id, old_score, new_score, reason,
            f_repayment, f_witness, f_network, f_activity,
        )
        return new_score

    except TrustEngineError:
        raise
    except Exception as exc:
        logger.error("Unexpected error in recompute_trust_score for user_id={}: {}", user_id, exc)
        raise TrustEngineError(f"Trust score computation failed: {exc}") from exc
