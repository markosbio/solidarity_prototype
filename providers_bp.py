from flask import Blueprint, render_template, request, redirect, url_for, session, abort
from models import db, Provider, User, Community, CommunityMembership

providers_bp = Blueprint('providers', __name__, url_prefix='/providers')


def _get_current_user():
    if 'user_id' not in session:
        return None
    return User.query.get(session['user_id'])


def _is_admin(user):
    if not user:
        return False
    if getattr(user, 'is_global_admin', False):
        return True
    if user.primary_community_id:
        m = CommunityMembership.query.filter_by(
            user_id=user.id, community_id=user.primary_community_id
        ).first()
        return m and m.role in ['admin', 'coadmin']
    return False


@providers_bp.route('/')
def list_providers():
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))
    providers = Provider.query.order_by(Provider.name).all()
    return render_template('providers.html', user=user, providers=providers, is_admin=_is_admin(user))


@providers_bp.route('/add', methods=['GET', 'POST'])
def add_provider():
    user = _get_current_user()
    if not user:
        return redirect(url_for('register'))
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        provider_code = request.form.get('provider_code', '').strip().upper()
        payment_type = request.form.get('payment_type', '')
        payment_details = request.form.get('payment_details', '').strip()
        if not name or not provider_code:
            error = 'Name and provider code are required.'
        elif Provider.query.filter_by(provider_code=provider_code).first():
            error = f"Provider code '{provider_code}' is already taken."
        else:
            p = Provider(
                name=name,
                provider_code=provider_code,
                payment_type=payment_type,
                payment_details=payment_details,
                verified=True,
            )
            db.session.add(p)
            db.session.commit()
            return redirect(url_for('providers.list_providers'))
    return render_template('providers_add.html', user=user, error=error)


@providers_bp.route('/<int:provider_id>/verify', methods=['POST'])
def verify_provider(provider_id):
    user = _get_current_user()
    if not user or not _is_admin(user):
        abort(403)
    provider = Provider.query.get_or_404(provider_id)
    provider.verified = not provider.verified
    db.session.commit()
    return redirect(url_for('providers.list_providers'))
