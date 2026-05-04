"""
Trust Graph — Reciprocity-Weighted Draw Ceiling.

The draw ceiling is the maximum amount a user may pull from the communal pool
in a single care request. It is driven entirely by the multi-factor trust score
produced by trust_engine, making behaviour → score → access a closed loop:

  Good behaviour  →  higher combined score
  Higher score    →  larger draw ceiling
  More access     →  more contributions to repay  →  higher score

Components
──────────
  base            combined_score × $100            (0 – $100)
  recruit bonus   Σ recruit_combined_score × $20 × 0.8  (0 – unbounded)
  witness boost   +10 % if user has a verified request in last 30 days
  pool factor     ×1.2 if pool > $2 000 / ×0.7 if pool < $500
  hard cap        $500

Neo4j upgrade path
──────────────────
  Replace the SQLAlchemy calls below with Cypher queries when scale demands it.
  Equivalent Cypher for depth-3 trust traversal:
    MATCH (u:User {id: $uid})-[:RECRUITED_BY*1..3]->(r:User)
    RETURN r.id, r.trustScore

  Install:  pip install neo4j
  Connect:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
"""
from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from models import db, User, SystemState, WitnessRequest
from trust_engine import get_combined_score


class TrustGraphError(Exception):
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pool_health_factor(pool_balance: float) -> float:
    if pool_balance > 2000:
        return 1.2
    if pool_balance < 500:
        return 0.7
    return 1.0


def _recent_verified_boost(user_id: int, window_days: int = 30) -> float:
    """
    Return a 10 % boost multiplier (i.e. 1.10) if the user has had at least one
    care request verified by witnesses within the last `window_days` days.
    This rewards users who have already demonstrated trustworthy behaviour through
    the peer-witness system.
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    verified_recently = (
        WitnessRequest.query
        .filter(
            WitnessRequest.user_id == user_id,
            WitnessRequest.status == 'verified',
            WitnessRequest.timestamp >= cutoff,
        )
        .first()
    )
    return 1.10 if verified_recently else 1.0


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_draw_ceiling(user_id: int) -> float:
    """
    Compute the solidarity draw ceiling for `user_id`.

    Uses the live multi-factor combined score (repayment + witness + network +
    activity) rather than the stored trust_score field, so the ceiling reflects
    the user's real-time behaviour.

    Returns the ceiling in dollars, capped at $500.
    Raises TrustGraphError on any unexpected failure.
    """
    try:
        user = User.query.get(user_id)
        if not user:
            raise TrustGraphError(f"User with id={user_id} not found")

        # ── 1. Own contribution (multi-factor score → $0–$100) ────────────────
        combined = get_combined_score(user_id)
        own_contribution = combined * 100.0

        # ── 2. Recruit bonus (each recruit's live score → up to $16/recruit) ──
        recruits = user.recruits
        recruit_sum = 0.0
        for rec in recruits:
            rec_score = get_combined_score(rec.id)
            recruit_sum += rec_score * 20.0 * 0.8   # weight 0.8 dampens network gaming

        raw_ceiling = own_contribution + recruit_sum

        # ── 3. Verified-witness boost (+10 % if recently verified) ─────────────
        witness_multiplier = _recent_verified_boost(user_id)
        raw_ceiling *= witness_multiplier

        # ── 4. Pool health factor ─────────────────────────────────────────────
        state = SystemState.query.first()
        pool = state.communal_pool_balance if state else 5000.0
        health_factor = _pool_health_factor(pool)

        ceiling = round(raw_ceiling * health_factor, 2)
        ceiling = min(ceiling, 500.0)

        logger.info(
            "Draw ceiling computed: user_id={} combined_score={:.4f} "
            "own={:.2f} recruits={:.2f} witness_boost={} pool_factor={} ceiling={:.2f}",
            user_id, combined, own_contribution, recruit_sum,
            f"+10%" if witness_multiplier > 1 else "none",
            health_factor, ceiling,
        )
        return ceiling

    except TrustGraphError:
        raise
    except Exception as exc:
        logger.error("Unexpected error in compute_draw_ceiling for user_id={}: {}", user_id, exc)
        raise TrustGraphError(f"Failed to compute draw ceiling: {exc}") from exc
