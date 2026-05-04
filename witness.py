import random
from loguru import logger
from models import db, User, WitnessRequest


class WitnessSelectionError(Exception):
    pass


def select_witnesses(user_id: int, provider_id: str, k: int = 3) -> list:
    """
    Upgrade 4: Weighted witness selection based on honesty (accuracy score)
    and proximity (region prefix matching).

    Weights:
      - Base weight: 1.0
      - Accuracy bonus: up to +1.0 for perfect accuracy score
      - Region match bonus: +0.5 if same region prefix as requester
      - Penalty: -0.8 if same region prefix (anti-collusion for same-prefix users
        only applies when pool is large enough to avoid it)
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
            logger.warning("Fewer eligible witnesses ({}) than requested ({})", len(all_users), k)

        requester_prefix = (requester.region_prefix or requester.phone[:3])

        weighted_pool: list[tuple[float, User]] = []
        for u in all_users:
            user_prefix = (u.region_prefix or u.phone[:3])
            weight = 1.0

            # Honesty bonus: scale accuracy score (0.0–1.0) to a +1.0 bonus
            weight += u.witness_accuracy_score

            # Proximity: same region earns a bonus
            if user_prefix == requester_prefix:
                weight += 0.5

            # Anti-collusion: slight penalty for very close matches when pool is large
            if len(all_users) >= 10 and user_prefix == requester_prefix:
                weight -= 0.3

            # Clamp weight to non-negative
            weight = max(weight, 0.1)
            weighted_pool.append((weight, u))

        # Weighted random sampling without replacement
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

        logger.info(
            "Selected {} witnesses for user_id={}: {}",
            len(selected), user_id, [w.id for w in selected]
        )
        return selected

    except WitnessSelectionError:
        raise
    except Exception as exc:
        logger.error("Unexpected error in select_witnesses for user_id={}: {}", user_id, exc)
        raise WitnessSelectionError(f"Witness selection failed: {exc}") from exc


def record_witness_outcome(request_id: int, final_status: str) -> None:
    """
    After a WitnessRequest is resolved, update each witness's accuracy score.
    A 'verified' outcome rewards witnesses who voted 'accept'; 'flagged' rewards 'reject'.
    """
    try:
        req = WitnessRequest.query.get(request_id)
        if not req or not req.votes:
            return

        correct_vote = 'accept' if final_status == 'verified' else 'reject'
        votes_list = [v for v in req.votes.split(',') if v.strip()]

        for vote_entry in votes_list:
            parts = vote_entry.split(':')
            if len(parts) != 2:
                continue
            witness_id_str, vote = parts
            try:
                witness = User.query.get(int(witness_id_str))
            except ValueError:
                continue
            if not witness:
                continue

            witness.total_witness_calls += 1
            if vote == correct_vote:
                witness.correct_witness_calls += 1

            if witness.total_witness_calls > 0:
                witness.witness_accuracy_score = round(
                    witness.correct_witness_calls / witness.total_witness_calls, 4
                )

        db.session.commit()
        logger.info("Updated witness accuracy scores for request_id={}", request_id)

    except Exception as exc:
        logger.error("Error updating witness outcomes for request_id={}: {}", request_id, exc)


def verify_consensus(request_id: int) -> str:
    """Check current consensus state without writing. Returns 'verified', 'flagged', or 'pending'."""
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
