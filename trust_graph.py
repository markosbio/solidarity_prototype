from models import db, User

def compute_draw_ceiling(user_id):
    """
    Compute solidarity draw ceiling based on:
    - user's own trust score
    - weighted sum of trust scores of recruits (depth 1 for simplicity)
    - pool health factor (simple: if pool > 1000, boost; else reduce)
    """
    user = User.query.get(user_id)
    if not user:
        return 0.0
    
    # Base from user's own trust score (0-1) scaled to max $100
    own_contribution = user.trust_score * 100
    
    # Recruits contribution: each recruit adds 0.8 * recruit_trust_score * 20
    recruits = user.recruits
    recruit_sum = 0.0
    for rec in recruits:
        recruit_sum += rec.trust_score * 20 * 0.8  # weight 0.8
    # Recruitment freshness factor: recent recruits count more softly
    # For simplicity, we ignore freshness in this prototype (but structure is there)
    
    raw_ceiling = own_contribution + recruit_sum
    
    # Pool health factor: if communal pool > 2000, multiplier 1.2; if < 500, 0.7
    from models import SystemState
    state = SystemState.query.first()
    pool = state.communal_pool_balance if state else 5000
    if pool > 2000:
        health_factor = 1.2
    elif pool < 500:
        health_factor = 0.7
    else:
        health_factor = 1.0
    
    ceiling = raw_ceiling * health_factor
    return min(ceiling, 500.0)  # cap per request for safety
