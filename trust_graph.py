"""
Trust Graph — Reciprocity-Weighted Draw Ceiling.

Uses the live multi-factor combined score (trust_engine.get_combined_score)
so that the ceiling tightly tracks behaviour in real time.

Community pool (Phase 2):
  If the user belongs to a primary community, use community.pool_balance
  for the health factor. Otherwise fall back to SystemState.communal_pool_balance.

Neo4j upgrade path:
  MATCH (u:User {id: $uid})-[:RECRUITED_BY*1..3]->(r:User)
  RETURN r.id, r.trustScore
"""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from models import db, User, SystemState, WitnessRequest
from trust_engine import get_combined_score


class TrustGraphError(Exception):
    pass


# ── Helpers (also imported by trust_engine.simulate_ceiling_preview) ──────────

def _pool_health_factor(pool_balance: float) -> float:
    if pool_balance > 2000:
        return 1.2
    if pool_balance < 500:
        return 0.7
    return 1.0


def _recent_verified_boost(user_id: int, window_days: int = 30) -> float:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    verified = WitnessRequest.query.filter(
        WitnessRequest.user_id == user_id,
        WitnessRequest.status == 'verified',
        WitnessRequest.timestamp >= cutoff,
    ).first()
    # Also check new CareRequest model
    if not verified:
        try:
            from models import CareRequest
            verified = CareRequest.query.filter(
                CareRequest.user_id == user_id,
                CareRequest.status.in_(['paid', 'admin_approved']),
                CareRequest.created_at >= cutoff,
            ).first()
        except Exception:
            pass
    return 1.10 if verified else 1.0


def _get_pool_balance(user: User) -> float:
    """Return pool balance for user's primary community, or global pool."""
    if user.primary_community_id:
        try:
            from models import Community
            comm = Community.query.get(user.primary_community_id)
            if comm:
                return comm.pool_balance
        except Exception:
            pass
    state = SystemState.query.first()
    return state.communal_pool_balance if state else 5000.0


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_draw_ceiling(user_id: int) -> float:
    """
    Compute the solidarity draw ceiling for `user_id`.

    Components:
      own       = combined_score × $100          (0 – $100)
      recruits  = Σ recruit_score × $20 × 0.8
      witness   = +10 % if verified recently
      pool      = × 1.2 / 1.0 / 0.7 based on pool health
      cap       = $500

    Returns ceiling in dollars.  Raises TrustGraphError on failure.
    """
    try:
        user = User.query.get(user_id)
        if not user:
            raise TrustGraphError(f"User with id={user_id} not found")

        combined = get_combined_score(user_id)
        own_contribution = combined * 100.0

        recruit_sum = sum(get_combined_score(r.id) * 20.0 * 0.8 for r in user.recruits)

        raw = (own_contribution + recruit_sum) * _recent_verified_boost(user_id)

        pool = _get_pool_balance(user)
        ceiling = round(min(raw * _pool_health_factor(pool), 500.0), 2)

        logger.info(
            "Draw ceiling: user_id={} score={:.4f} own={:.2f} recruits={:.2f} "
            "witness_boost={} pool={:.0f} ceiling={:.2f}",
            user_id, combined, own_contribution, recruit_sum,
            '+10%' if _recent_verified_boost(user_id) > 1 else 'none',
            pool, ceiling,
        )
        return ceiling

    except TrustGraphError:
        raise
    except Exception as exc:
        logger.error("compute_draw_ceiling failed for user_id={}: {}", user_id, exc)
        raise TrustGraphError(f"Failed to compute draw ceiling: {exc}") from exc
