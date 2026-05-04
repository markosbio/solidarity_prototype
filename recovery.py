from loguru import logger
from models import db, User, SystemState


class RecoveryError(Exception):
    pass


def update_recovery_parameters(user_id: int, social_credit_amount: float) -> None:
    """
    Adjust the user's round-up intensifier based on outstanding social credit.
    Also applies a solidarity rebate when the communal pool is critically low.
    """
    try:
        user = User.query.get(user_id)
        if not user:
            raise RecoveryError(f"User with id={user_id} not found")

        increase = (social_credit_amount / 10.0) * 0.05
        user.roundup_intensifier = round(min(1.0 + increase, 2.0), 4)

        state = SystemState.query.first()
        if state and state.communal_pool_balance < 100.0:
            user.roundup_intensifier = round(max(1.0, user.roundup_intensifier - 0.05), 4)
            logger.warning(
                "Pool critically low ({:.2f}); applied solidarity rebate for user_id={}",
                state.communal_pool_balance, user_id
            )

        db.session.commit()
        logger.info(
            "Recovery parameters updated for user_id={}: intensifier={:.4f}",
            user_id, user.roundup_intensifier
        )

    except RecoveryError:
        raise
    except Exception as exc:
        logger.error("Unexpected error in update_recovery_parameters for user_id={}: {}", user_id, exc)
        raise RecoveryError(f"Recovery update failed: {exc}") from exc
