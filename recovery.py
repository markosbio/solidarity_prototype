from models import db, User, SystemState

def update_recovery_parameters(user_id, social_credit_amount):
    """
    Adjust user's round-up intensifier based on social credit size.
    Also triggers solidarity rebate if pool health low.
    """
    user = User.query.get(user_id)
    if not user:
        return
    
    # Linear increase: each $10 of social credit increases intensifier by 0.05
    increase = (social_credit_amount / 10.0) * 0.05
    user.roundup_intensifier = min(1.0 + increase, 2.0)  # cap at 2x
    
    # Check pool health: if below $100, redistribute a solidarity rebate
    state = SystemState.query.first()
    if state.communal_pool_balance < 100.0:
        # Take 10% of the increased future round-ups (simulated) – in real app you'd adjust all users' parameters
        # For prototype, we just log and reduce the intensifier slightly to simulate rebate
        user.roundup_intensifier = max(1.0, user.roundup_intensifier - 0.05)
        # In production, you'd redistribute from a 'solidarity fund'
    
    db.session.commit()
