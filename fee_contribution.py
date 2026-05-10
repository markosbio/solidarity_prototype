"""
Fee-based solidarity contribution engine.

When a member makes a mobile money transaction (send/withdraw), their normal
operator fee is used as the base for the 8% solidarity contribution.

process_fee_contribution() is the single entry point:
  - Reads solidarity_percent from SystemState (default 8.0)
  - Calculates solidarity_amount = normal_fee * (solidarity_percent / 100)
  - Optionally rounds to nearest 10 UGX
  - Splits solidarity_amount using the existing 70/20/10 wallet/pool/fee split
  - Updates sub_wallet_balance, community pool_balance, and records Transactions
  - Records the platform fee portion as a PlatformRevenue row
  - Returns solidarity_amount
"""
from __future__ import annotations

import os
from datetime import datetime

from loguru import logger

from models import db, User, Transaction, Community, SystemState, PlatformRevenue


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


def process_fee_contribution(user_id: int, normal_fee: float,
                              round_to_10: bool = True) -> float:
    """
    Process a solidarity contribution based on a mobile money normal fee.

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

        user.sub_wallet_balance += to_wallet
        wallet_tx = Transaction(
            user_id=user_id,
            amount=to_wallet,
            type='solidarity_wallet',
            description=f'Solidarity contribution wallet share (fee UGX {normal_fee:.0f})',
        )
        db.session.add(wallet_tx)

        primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
        if primary_comm and to_pool > 0:
            primary_comm.pool_balance += to_pool
            db.session.add(Transaction(
                user_id=user_id,
                amount=to_pool,
                type='solidarity_pool',
                description=f'Solidarity contribution pool share (fee UGX {normal_fee:.0f})',
            ))

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

        # Record platform revenue
        if to_fee > 0:
            rev = PlatformRevenue(
                amount=to_fee,
                source='solidarity_fee',
                transaction_id=fee_tx.id if fee_tx else None,
            )
            db.session.add(rev)

        db.session.commit()

        logger.info(
            "Fee contribution: user_id={} normal_fee={:.0f} solidarity={:.0f} "
            "wallet={:.0f} pool={:.0f} platform_fee={:.0f}",
            user_id, normal_fee, solidarity_amount, to_wallet, to_pool, to_fee,
        )
        return solidarity_amount

    except Exception as exc:
        logger.error("process_fee_contribution failed for user_id={}: {}", user_id, exc)
        db.session.rollback()
        return 0.0
