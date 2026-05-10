"""
Pool health protection rules.

Monitors each community's pool balance as a percentage of its target and
applies automatic protective actions when the pool drops below thresholds.

Thresholds:
  >= 60%  — Healthy   (no restrictions)
  30–59%  — Fair      (draw ceiling reduced to 80%)
  < 30%   — Low       (ceiling 60%, witness strictness 3-of-3, large pause)
  < 10%   — Critical  (ceiling 40%, all rules active, solidarity_percent boosted)
"""
from __future__ import annotations

from loguru import logger

from models import db, Community, CommunityMembership, User, SystemState
from notifications import notify_pool_low, _send_sms


_LARGE_WITHDRAWAL_THRESHOLD = 100_000.0


def get_pool_health_pct(community: Community) -> float:
    target = community.pool_target if community.pool_target else 2_000_000.0
    return min(100.0, max(0.0, community.pool_balance / target * 100.0))


def enforce_pool_health(community: Community) -> dict:
    """
    Evaluate pool health and apply protective rules in-place on `community`.
    Commits changes. Returns a dict describing actions taken.
    """
    pct = get_pool_health_pct(community)
    actions: list[str] = []
    prev_multiplier = community.ceiling_multiplier
    prev_strictness = community.witness_strictness
    prev_paused = community.large_withdrawal_paused

    if pct >= 60:
        community.ceiling_multiplier = 1.0
        community.witness_strictness = 'normal'
        community.large_withdrawal_paused = False
    elif pct >= 30:
        community.ceiling_multiplier = 0.80
        community.witness_strictness = 'normal'
        community.large_withdrawal_paused = False
        actions.append('ceiling_reduced_20pct')
    elif pct >= 10:
        community.ceiling_multiplier = 0.60
        community.witness_strictness = 'strict'
        community.large_withdrawal_paused = True
        actions.append('ceiling_reduced_40pct')
        actions.append('witness_3of3_required')
        actions.append('large_withdrawals_paused')
    else:
        community.ceiling_multiplier = 0.40
        community.witness_strictness = 'strict'
        community.large_withdrawal_paused = True
        actions.append('ceiling_reduced_60pct')
        actions.append('witness_3of3_required')
        actions.append('large_withdrawals_paused')
        actions.append('critical_pool')

    db.session.commit()

    changed = (
        community.ceiling_multiplier != prev_multiplier
        or community.witness_strictness != prev_strictness
        or community.large_withdrawal_paused != prev_paused
    )

    if changed or pct < 60:
        logger.info(
            "Pool health: community_id={} pct={:.1f}% multiplier={} strictness={} paused={} actions={}",
            community.id, pct, community.ceiling_multiplier,
            community.witness_strictness, community.large_withdrawal_paused, actions,
        )

    if pct < 30 and changed:
        _notify_pool_health_admins(community, pct, actions)

    return {
        'pct': round(pct, 1),
        'ceiling_multiplier': community.ceiling_multiplier,
        'witness_strictness': community.witness_strictness,
        'large_withdrawal_paused': community.large_withdrawal_paused,
        'actions': actions,
    }


def is_large_withdrawal_blocked(community: Community, amount: float) -> bool:
    """Return True if this withdrawal should be blocked due to pool health rules."""
    return bool(community.large_withdrawal_paused and amount > _LARGE_WITHDRAWAL_THRESHOLD)


def required_witness_approvals(community: Community, total_witnesses: int) -> int:
    """Return the minimum number of 'accept' votes required to pass."""
    if community.witness_strictness == 'strict':
        return total_witnesses
    return max(2, total_witnesses // 2 + 1)


def _notify_pool_health_admins(community: Community, pct: float, actions: list[str]) -> None:
    if not community.admin_user_id:
        return
    admin = User.query.get(community.admin_user_id)
    if not admin:
        return
    restrictions = []
    if 'witness_3of3_required' in actions:
        restrictions.append('unanimous witness votes now required')
    if 'large_withdrawals_paused' in actions:
        restrictions.append(f'withdrawals over UGX {_LARGE_WITHDRAWAL_THRESHOLD:,.0f} are paused')
    if 'ceiling_reduced_40pct' in actions or 'ceiling_reduced_60pct' in actions:
        pct_cut = 40 if 'ceiling_reduced_40pct' in actions else 60
        restrictions.append(f'draw ceilings reduced by {pct_cut}%')

    msg = (
        f"[SolidarityPool] Pool alert for '{community.name}': "
        f"{pct:.0f}% health. "
        f"Protective rules active: {', '.join(restrictions)}. "
        f"Encourage members to contribute to restore the pool."
    )
    _send_sms(admin.phone, msg)


def enforce_all_community_pools() -> None:
    """Run enforcement on every community — call from a periodic task or after any transaction."""
    for community in Community.query.all():
        try:
            enforce_pool_health(community)
        except Exception as exc:
            logger.error("Pool health enforcement failed for community_id={}: {}", community.id, exc)
