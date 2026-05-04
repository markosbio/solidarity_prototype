import random
from models import db, User, WitnessRequest

def select_witnesses(user_id, provider_id, k=3):
    """
    Select k potential witnesses from all users except requester.
    For prototype: random selection + simple anti-collusion:
    avoid users with same phone prefix as requester or provider if possible.
    """
    all_users = User.query.filter(User.id != user_id).all()
    if len(all_users) < k:
        return all_users
    
    # Simple anti-collusion: compute a weight
    requester = User.query.get(user_id)
    weighted_users = []
    for u in all_users:
        # Lower weight if same phone area code (first 3 digits)
        penalty = 0.0
        if requester and u.phone[:3] == requester.phone[:3]:
            penalty = 0.5
        # Also avoid provider ID matching (not used much in prototype)
        weight = 1.0 - penalty
        weighted_users.extend([u] * int(weight * 10))  # crude weighted sampling
    
    # Shuffle and take first k
    random.shuffle(weighted_users)
    witnesses = weighted_users[:k]
    # Ensure we have exactly k unique
    unique = []
    for w in witnesses:
        if w not in unique:
            unique.append(w)
        if len(unique) == k:
            break
    return unique[:k]

def verify_consensus(request_id):
    """Called later, but we implement in route for demo."""
    pass
