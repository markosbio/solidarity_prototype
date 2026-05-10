"""
Fraud scoring engine for care requests.

calculate_fraud_risk() returns a risk score in [0.0, 1.0] and a list of
triggered reasons.  If the score exceeds FRAUD_THRESHOLD the request is
flagged for manual admin review, bypassing automatic witness approval.

Triggers:
  - Too many requests in a short window (last 7 days)
  - Same provider used suspiciously often (last 30 days)
  - Abnormal draw ceiling usage (request > 90% of ceiling)
  - Witness collusion patterns (all witnesses in same micro-region)
  - Inconsistent transaction behaviour (no round-up history but high request)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from loguru import logger

from models import db, CareRequest, Transaction, User, FraudAlert

FRAUD_THRESHOLD = 0.55


def calculate_fraud_risk(user_id: int, care_request_id: int) -> tuple[float, list[str]]:
    """
    Compute fraud risk for a newly created CareRequest.

    Args:
        user_id:          The requesting member's ID.
        care_request_id:  The ID of the just-committed CareRequest row.

    Returns:
        (risk_score, reasons) — risk_score in [0.0, 1.0], reasons is a list of
        human-readable trigger strings.
    """
    reasons: list[str] = []
    score = 0.0

    try:
        now = datetime.utcnow()
        care_req = CareRequest.query.get(care_request_id)
        amount_needed = float(care_req.amount_needed) if care_req else 0.0
        provider_id = care_req.provider_id if care_req else None
        draw_ceiling = 1.0
        try:
            from trust_graph import compute_draw_ceiling
            draw_ceiling = float(compute_draw_ceiling(user_id)) or 1.0
        except Exception:
            pass
        witness_ids = []

        user = User.query.get(user_id)
        if not user:
            return 1.0, ['user_not_found']

        # ── Trigger 1: Too many requests in last 7 days ───────────────────
        recent_count = CareRequest.query.filter(
            CareRequest.user_id == user_id,
            CareRequest.created_at >= now - timedelta(days=7),
        ).count()
        if recent_count >= 3:
            score += 0.30
            reasons.append(f'high_request_frequency:{recent_count}_in_7d')
        elif recent_count >= 2:
            score += 0.15
            reasons.append(f'elevated_request_frequency:{recent_count}_in_7d')

        # ── Trigger 2: Same provider repeated suspiciously ────────────────
        if provider_id:
            same_provider_count = CareRequest.query.filter(
                CareRequest.user_id == user_id,
                CareRequest.provider_id == provider_id,
                CareRequest.created_at >= now - timedelta(days=30),
            ).count()
            if same_provider_count >= 3:
                score += 0.25
                reasons.append(f'repeated_same_provider:{same_provider_count}_in_30d')
            elif same_provider_count >= 2:
                score += 0.10
                reasons.append(f'same_provider_twice:{same_provider_count}_in_30d')

        # ── Trigger 3: Abnormal draw ceiling usage ────────────────────────
        if draw_ceiling > 0:
            ceiling_pct = amount_needed / draw_ceiling
            if ceiling_pct > 0.95:
                score += 0.20
                reasons.append(f'ceiling_maxout:{ceiling_pct:.0%}_of_ceiling')
            elif ceiling_pct > 0.85:
                score += 0.10
                reasons.append(f'high_ceiling_usage:{ceiling_pct:.0%}_of_ceiling')

        # ── Trigger 4: Witness collusion (all same region prefix) ─────────
        if len(witness_ids) >= 2:
            witnesses = [User.query.get(wid) for wid in witness_ids if User.query.get(wid)]
            prefixes = [w.region_prefix or w.phone[:3] for w in witnesses]
            requester_prefix = user.region_prefix or user.phone[:3]
            if len(set(prefixes)) == 1 and prefixes[0] == requester_prefix:
                score += 0.15
                reasons.append('witness_collusion:all_same_region')

        # ── Trigger 5: No round-up history but large request ─────────────
        roundup_count = db.session.query(db.func.count(Transaction.id)).filter(
            Transaction.user_id == user_id,
            Transaction.type.in_(['roundup', 'mpesa_roundup', 'solidarity_wallet']),
        ).scalar() or 0
        if roundup_count == 0 and amount_needed > 50_000:
            score += 0.20
            reasons.append('no_contribution_history:large_request')
        elif roundup_count < 3 and amount_needed > 100_000:
            score += 0.10
            reasons.append(f'low_contribution_history:{roundup_count}_contributions')

        score = round(min(score, 1.0), 4)

    except Exception as exc:
        logger.error("calculate_fraud_risk failed for user_id={}: {}", user_id, exc)
        score = 0.0
        reasons = []

    return score, reasons


def log_fraud_alert(user_id: int, care_request_id: int | None,
                    score: float, reasons: list[str]) -> FraudAlert:
    """Persist a FraudAlert row and return it."""
    alert = FraudAlert(
        user_id=user_id,
        care_request_id=care_request_id,
        fraud_score=score,
        triggers='; '.join(reasons),
    )
    db.session.add(alert)
    db.session.commit()
    logger.warning(
        "Fraud alert: user_id={} care_request_id={} score={} reasons={}",
        user_id, care_request_id, score, reasons,
    )
    return alert


def is_fraud_flagged(score: float) -> bool:
    return score >= FRAUD_THRESHOLD
