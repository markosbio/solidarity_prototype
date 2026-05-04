"""
Phase 2: Community Pods.

Blueprint mounted at /communities.
Each community has its own pool_balance and member roster.
"""
from flask import Blueprint, render_template, request, redirect, url_for, abort
from flask_login import login_required, current_user
from loguru import logger

from models import db, Community, CommunityMembership, User

communities_bp = Blueprint('communities', __name__, url_prefix='/communities')


# ── Routes ─────────────────────────────────────────────────────────────────────

@communities_bp.route('/')
@login_required
def list_communities():
    my_memberships = CommunityMembership.query.filter_by(user_id=current_user.id).all()
    my_community_ids = {m.community_id for m in my_memberships}
    all_communities = Community.query.order_by(Community.name).all()
    return render_template(
        'communities.html',
        user=current_user,
        my_memberships=my_memberships,
        my_community_ids=my_community_ids,
        all_communities=all_communities,
    )


@communities_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create_community():
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()

        if not name:
            error = 'Community name is required.'
        else:
            community = Community(
                name=name,
                description=description,
                admin_user_id=current_user.id,
                pool_balance=0.0,
            )
            db.session.add(community)
            db.session.flush()   # get community.id before commit

            # Creator becomes admin member
            membership = CommunityMembership(
                user_id=current_user.id,
                community_id=community.id,
                role='admin',
            )
            db.session.add(membership)

            # Set as primary community if user has none
            if not current_user.primary_community_id:
                current_user.primary_community_id = community.id

            db.session.commit()
            logger.info("Community created: id={} name={} admin_id={}",
                        community.id, name, current_user.id)
            return redirect(url_for('communities.list_communities'))

    return render_template('community_create.html', user=current_user, error=error)


@communities_bp.route('/join', methods=['POST'])
@login_required
def join_community():
    invite_code = request.form.get('invite_code', '').strip().upper()
    community = Community.query.filter_by(invite_code=invite_code).first()

    if not community:
        return redirect(url_for('communities.list_communities',
                                error='Invalid invite code.'))

    # Check already a member
    existing = CommunityMembership.query.filter_by(
        user_id=current_user.id, community_id=community.id
    ).first()
    if existing:
        return redirect(url_for('communities.list_communities'))

    membership = CommunityMembership(
        user_id=current_user.id,
        community_id=community.id,
        role='member',
    )
    db.session.add(membership)

    if not current_user.primary_community_id:
        current_user.primary_community_id = community.id

    db.session.commit()
    logger.info("User {} joined community {}", current_user.id, community.id)
    return redirect(url_for('communities.list_communities'))


@communities_bp.route('/<int:community_id>/set_primary', methods=['POST'])
@login_required
def set_primary(community_id):
    membership = CommunityMembership.query.filter_by(
        user_id=current_user.id, community_id=community_id
    ).first()
    if not membership:
        abort(403)
    current_user.primary_community_id = community_id
    db.session.commit()
    return redirect(url_for('communities.list_communities'))


@communities_bp.route('/<int:community_id>/contribute', methods=['POST'])
@login_required
def contribute(community_id):
    """Transfer from sub-wallet to community pool."""
    membership = CommunityMembership.query.filter_by(
        user_id=current_user.id, community_id=community_id
    ).first()
    if not membership:
        abort(403)

    community = Community.query.get_or_404(community_id)
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        return redirect(url_for('communities.list_communities'))

    if amount <= 0 or amount > current_user.sub_wallet_balance:
        return redirect(url_for('communities.list_communities'))

    current_user.sub_wallet_balance -= amount
    community.pool_balance += amount
    db.session.commit()
    logger.info("User {} contributed {:.2f} to community {}", current_user.id, amount, community_id)
    return redirect(url_for('communities.list_communities'))
