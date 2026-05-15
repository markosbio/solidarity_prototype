from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, abort, session
from loguru import logger

from models import db, Community, CommunityMembership, User

communities_bp = Blueprint('communities', __name__, url_prefix='/communities')


def _get_current_user():
    if 'user_id' not in session:
        return None
    return User.query.get(session['user_id'])


@communities_bp.route('/')
def list_communities():
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))
    error = request.args.get('error')
    my_memberships = CommunityMembership.query.filter_by(user_id=user.id).all()
    my_community_ids = {m.community_id for m in my_memberships}
    # Exclude global reserve — it is not user-facing
    all_communities = Community.query.filter_by(is_global_reserve=False).order_by(Community.name).all()
    return render_template(
        'communities.html',
        user=user,
        my_memberships=my_memberships,
        my_community_ids=my_community_ids,
        all_communities=all_communities,
        error=error,
    )


@communities_bp.route('/create', methods=['GET', 'POST'])
def create_community():
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()

        if not name:
            error = 'Community name is required.'
        else:
            import random, string
            invite = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            community = Community(
                name=name,
                description=description,
                admin_user_id=user.id,
                pool_balance=0.0,
                invite_code=invite,
            )
            db.session.add(community)
            db.session.flush()

            membership = CommunityMembership(
                user_id=user.id,
                community_id=community.id,
                role='admin',
            )
            db.session.add(membership)

            if not user.primary_community_id:
                user.primary_community_id = community.id

            db.session.commit()
            logger.info("Community created: id={} name={} admin_id={}",
                        community.id, name, user.id)
            return redirect(url_for('communities.list_communities'))

    return render_template('community_create.html', user=user, error=error)


@communities_bp.route('/join', methods=['POST'])
def join_community():
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))
    invite_code = request.form.get('invite_code', '').strip().upper()
    community = Community.query.filter_by(invite_code=invite_code).first()

    if not community:
        return redirect(url_for('communities.list_communities',
                                error='Invalid invite code.'))

    # Block joining the global reserve — it is system-only
    if community.is_global_reserve:
        return redirect(url_for('communities.list_communities',
                                error='This community cannot be joined.'))

    existing = CommunityMembership.query.filter_by(
        user_id=user.id, community_id=community.id
    ).first()
    if existing:
        return redirect(url_for('communities.list_communities'))

    membership = CommunityMembership(
        user_id=user.id,
        community_id=community.id,
        role='member',
    )
    db.session.add(membership)

    if not user.primary_community_id:
        user.primary_community_id = community.id
        user.primary_community_changed_at = datetime.utcnow()

    db.session.commit()
    logger.info("User {} joined community {}", user.id, community.id)
    return redirect(url_for('communities.list_communities'))


@communities_bp.route('/<int:community_id>/set_primary', methods=['POST'])
def set_primary(community_id):
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))

    # Cannot set the global reserve as primary
    community = Community.query.get(community_id)
    if not community or community.is_global_reserve:
        return redirect(url_for('communities.list_communities',
                                error='This community cannot be your primary community.'))

    # 90-day cooldown — prevent switching into a rich group right before a big claim
    if user.primary_community_id and user.primary_community_id != community_id:
        if user.primary_community_changed_at:
            elapsed = datetime.utcnow() - user.primary_community_changed_at
            if elapsed < timedelta(days=90):
                days_left = (timedelta(days=90) - elapsed).days + 1
                return redirect(url_for('communities.list_communities',
                                        error=f'You can only change your primary community once every 90 days. '
                                              f'{days_left} day(s) remaining.'))

    membership = CommunityMembership.query.filter_by(
        user_id=user.id, community_id=community_id
    ).first()
    if not membership:
        abort(403)

    user.primary_community_id = community_id
    user.primary_community_changed_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('communities.list_communities'))


@communities_bp.route('/<int:community_id>/contribute', methods=['POST'])
def contribute(community_id):
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))
    membership = CommunityMembership.query.filter_by(
        user_id=user.id, community_id=community_id
    ).first()
    if not membership:
        abort(403)

    community = Community.query.get_or_404(community_id)
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        return redirect(url_for('communities.list_communities'))

    if amount <= 0 or amount > user.sub_wallet_balance:
        return redirect(url_for('communities.list_communities'))

    user.sub_wallet_balance -= amount
    community.pool_balance += amount
    db.session.commit()
    logger.info("User {} contributed {:.2f} to community {}", user.id, amount, community_id)
    return redirect(url_for('communities.list_communities'))
