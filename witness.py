"""
Witness selection and outcome tracking.

Phase 2 addition: community-aware selection — members of the same community
receive a significant weight bonus to keep governance local.
"""
import random
from loguru import logger
from models import db, User, WitnessRequest, CommunityMembership


class WitnessSelectionError(Exception):
    pass


def select_witnesses(user_id: int, provider_id: str = '',
                     k: int = 3, community_id: int = None) -> list:
    """
    Weighted random witness selection.

    Weight factors (higher = more likely to be selected):
      base          1.0
      accuracy      + witness_accuracy_score (0–1)
      same region   + 0.5
      same community+ 0.7  (Phase 2)
      anti-collusion- 0.3  (when pool >= 10 and same region)

    Args:
        user_id:      Requester's user ID (excluded from candidates).
        provider_id:  Unused in selection logic; kept for API compat.
        k:            Number of witnesses to select.
        community_id: If set, boost members of this community.
    """
    try:
        requester = User.query.get(user_id)
        if not requester:
            raise WitnessSelectionError(f"Requester user_id={user_id} not found")

        all_users = User.query.filter(User.id != user_id).all()
        if not all_users:
            logger.warning("No eligible witnesses found for user_id={}", user_id)
            return []

        if len(all_users) < k:
            logger.warning("Only {} eligible witnesses (requested {})", len(all_users), k)

        requester_prefix = requester.region_prefix or requester.phone[:3]

        # Build community membership set for fast lookup
        community_member_ids: set[int] = set()
        if community_id:
            rows = CommunityMembership.query.filter_by(community_id=community_id).all()
            community_member_ids = {r.user_id for r in rows}

        weighted_pool: list[tuple[float, User]] = []
        for u in all_users:
            user_prefix = u.region_prefix or u.phone[:3]
            weight = 1.0

            weight += u.witness_accuracy_score

            if user_prefix == requester_prefix:
                weight += 0.5

            if u.id in community_member_ids:
                weight += 0.7

            if len(all_users) >= 10 and user_prefix == requester_prefix:
                weight -= 0.3

            weight = max(weight, 0.1)
            weighted_pool.append((weight, u))

        selected: list[User] = []
        remaining = list(weighted_pool)
        for _ in range(min(k, len(remaining))):
            total = sum(w for w, _ in remaining)
            pick = random.uniform(0, total)
            cumulative = 0.0
            for i, (w, u) in enumerate(remaining):
                cumulative += w
                if cumulative >= pick:
                    selected.append(u)
                    remaining.pop(i)
                    break

        logger.info("Selected {} witnesses for user_id={}: ids={}",
                    len(selected), user_id, [w.id for w in selected])
        return selected

    except WitnessSelectionError:
        raise
    except Exception as exc:
        logger.error("Unexpected error in select_witnesses for user_id={}: {}", user_id, exc)
        raise WitnessSelectionError(f"Witness selection failed: {exc}") from exc


def record_witness_outcome(request_id: int, final_status: str,
                           model: str = 'witness') -> None:
    """
    Update each witness's accuracy score after a request is resolved.

    Args:
        request_id:   ID of the WitnessRequest or CareRequest.
        final_status: 'verified'/'flagged' (WitnessRequest) or
                      'paid'/'flagged' (CareRequest).
        model:        'witness' (WitnessRequest) or 'care' (CareRequest).
    """
    try:
        if model == 'care':
            from models import CareRequest
            req = CareRequest.query.get(request_id)
            votes_str = req.witness_votes if req else ''
            correct_vote = 'accept' if final_status in ('paid', 'admin_approved') else 'reject'
        else:
            req = WitnessRequest.query.get(request_id)
            votes_str = req.votes if req else ''
            correct_vote = 'accept' if final_status == 'verified' else 'reject'

        if not req or not votes_str:
            return

        for vote_entry in [v for v in votes_str.split(',') if v.strip()]:
            parts = vote_entry.split(':')
            if len(parts) != 2:
                continue
            try:
                witness = User.query.get(int(parts[0]))
            except ValueError:
                continue
            if not witness:
                continue

            witness.total_witness_calls += 1
            if parts[1] == correct_vote:
                witness.correct_witness_calls += 1

            if witness.total_witness_calls > 0:
                witness.witness_accuracy_score = round(
                    witness.correct_witness_calls / witness.total_witness_calls, 4
                )

        db.session.commit()
        logger.info("Witness accuracy updated for {}_id={}", model, request_id)

    except Exception as exc:
        logger.error("Error updating witness outcomes for {}_id={}: {}", model, request_id, exc)


def verify_consensus(request_id: int) -> str:
    req = WitnessRequest.query.get(request_id)
    if not req:
        return 'pending'
    votes_list = [v for v in (req.votes or '').split(',') if v.strip()]
    yes_count = sum(1 for v in votes_list if ':accept' in v)
    total_votes = len(votes_list)
    total_witnesses = len([w for w in (req.witness_ids or '').split(',') if w.strip()])
    if yes_count >= 2:
        return 'verified'
    if total_witnesses > 0 and total_votes >= total_witnesses:
        return 'flagged'
    return 'pending'
