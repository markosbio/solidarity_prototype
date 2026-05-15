"""
Fee-based solidarity contribution engine — Architecture v2.

Pool share routing:
  60% of the pool portion  → Global Reserve (is_global_reserve=True community)
  40% of the pool portion  → User's primary community only

Secondary communities receive no automatic contributions (they are social groups).
lifetime_contribution_score is updated on every successful contribution.
"""
from __future__ import annotations

import os
from datetime import datetime

from loguru import logger

from models import db, User, Transaction, Community, CommunityMembership, SystemState, PlatformRevenue


def _get_solidarity_percent() -> float:
    state = SystemState.query.first()
    return float(state.solidarity_percent) if state else 8.0


def _roundup_split(amount: float) -> tuple[float, float, float]:
    """Split using env-configurable 70/20/10 percentages."""
    w = float(os.getenv('ROUNDUP_WALLET_PCT', 70)) / 100
    p = float(os.getenv('ROUNDUP_POOL_PCT', 20)) / 100
    to_wallet = round(amount * w, 4)
    to_pool = round(amount * p, 4)
    to_fee = round(amount - to_wallet - to_pool, 4)
    return to_wallet, to_pool, to_fee


def _round_to_nearest_10(amount: float) -> float:
    return round(round(amount / 10) * 10, 4)


def _get_global_reserve() -> Community | None:
    """Return the system-level global reserve community."""
    return Community.query.filter_by(is_global_reserve=True).first()


def process_fee_contribution(user_id: int, normal_fee: float,
                              round_to_10: bool = True) -> float:
    """
    Process a solidarity contribution based on a mobile money normal fee.

    Pool routing (Architecture v2):
      • 60% of pool share  → Global Reserve community
      • 40% of pool share  → User's primary community

    Args:
        user_id:    Member's user ID.
        normal_fee: The standard operator fee charged for the transaction (UGX).
        round_to_10: Whether to round solidarity_amount to nearest 10 UGX.

    Returns:
        solidarity_amount credited to the member.
    """
    try:
        user = User.query.get(user_id)
        if not user:
            logger.error("process_fee_contribution: user_id={} not found", user_id)
            return 0.0

        pct = _get_solidarity_percent()
        solidarity_amount = normal_fee * (pct / 100.0)
        if round_to_10:
            solidarity_amount = _round_to_nearest_10(solidarity_amount)
        if solidarity_amount <= 0:
            return 0.0

        to_wallet, to_pool, to_fee = _roundup_split(solidarity_amount)

        # ── Wallet credit ────────────────────────────────────────────────────
        user.sub_wallet_balance += to_wallet
        db.session.add(Transaction(
            user_id=user_id,
            amount=to_wallet,
            type='solidarity_wallet',
            description=f'Solidarity contribution wallet share (fee UGX {normal_fee:.0f})',
        ))

        # ── Pool routing: 60% reserve / 40% primary community ────────────────
        if to_pool > 0:
            reserve = _get_global_reserve()
            primary = (Community.query.get(user.primary_community_id)
                       if user.primary_community_id else None)

            reserve_share = round(to_pool * 0.60, 4)
            primary_share = round(to_pool * 0.40, 4)
            # Correct for floating-point rounding
            primary_share = round(to_pool - reserve_share, 4)

            if reserve:
                reserve.pool_balance += reserve_share
                db.session.add(Transaction(
                    user_id=user_id,
                    amount=reserve_share,
                    type='solidarity_pool',
                    description=f'Solidarity reserve share (fee UGX {normal_fee:.0f})',
                ))
            else:
                # No reserve yet → treat as platform fee
                to_fee += reserve_share
                reserve_share = 0.0

            if primary and primary.id != (reserve.id if reserve else None):
                primary.pool_balance += primary_share
                db.session.add(Transaction(
                    user_id=user_id,
                    amount=primary_share,
                    type='solidarity_pool',
                    description=f'Solidarity pool share ({primary.name}) (fee UGX {normal_fee:.0f})',
                ))
            else:
                # No primary community yet → hold as platform revenue
                to_fee += primary_share

        # ── Platform fee ─────────────────────────────────────────────────────
        fee_tx = None
        if to_fee > 0:
            fee_tx = Transaction(
                user_id=user_id,
                amount=to_fee,
                type='solidarity_fee',
                description=f'Solidarity contribution platform fee (fee UGX {normal_fee:.0f})',
            )
            db.session.add(fee_tx)

        db.session.flush()

        if to_fee > 0:
            db.session.add(PlatformRevenue(
                amount=to_fee,
                source='solidarity_fee',
                transaction_id=fee_tx.id if fee_tx else None,
            ))

        # ── Lifetime contribution tracking ────────────────────────────────────
        user.lifetime_contribution_score = round(
            (user.lifetime_contribution_score or 0.0) + solidarity_amount, 4
        )

        db.session.commit()

        logger.info(
            "Fee contribution v2: user_id={} normal_fee={:.0f} solidarity={:.0f} "
            "wallet={:.0f} pool={:.0f} platform_fee={:.0f}",
            user_id, normal_fee, solidarity_amount, to_wallet, to_pool, to_fee,
        )
        return solidarity_amount

    except Exception as exc:
        logger.error("process_fee_contribution failed for user_id={}: {}", user_id, exc)
        db.session.rollback()
        return 0.0
