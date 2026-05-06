# SolidarityPool

A community mutual-aid app where users build a communal pool through micro round-ups and can request care funds backed by a trust graph and peer witness verification. Designed for mobile-money regions (M-Pesa/USSD).

## Run & Operate

- **Dev**: `python app.py` (Flask dev server on port 5000)
- **Production**: `gunicorn --bind=0.0.0.0:5000 --reuse-port app:app`
- **Required env vars**: `SECRET_KEY`, `DATABASE_URL` (auto-set by Replit PostgreSQL)
- **Optional env vars**: `AT_USERNAME`, `AT_API_KEY` (Africa's Talking USSD), `MPESA_*` (M-Pesa Daraja)

## Stack

- Python 3.12, Flask 2.3.3, Flask-SQLAlchemy 3.0.5
- PostgreSQL (via Replit DB) with SQLite fallback (`sqlite:///solidarity.db`)
- Session-based auth (no Flask-Login; uses `session['user_id']`)
- Loguru for logging, Gunicorn for production

## Where things live

```
app.py              Main Flask app — routes, blueprints registered here
models.py           All SQLAlchemy models (User, Community, CareRequest, WitnessRequest, TrustEvent, etc.)
trust_engine.py     Multi-factor trust score engine (repayment/witness/network/activity)
trust_graph.py      Draw-ceiling calculator
witness.py          Weighted witness selection + accuracy tracking
recovery.py         Round-up intensifier adaptive recovery
payments.py         PaymentRecord creation (provider payment simulation)
mpesa.py            M-Pesa Daraja STK Push + callback parser
ussd.py             Africa's Talking USSD Blueprint (/ussd/callback)
communities.py      /communities blueprint (session-based, no flask_login)
providers_bp.py     /providers blueprint (list/add/verify providers)
templates/          Jinja2 HTML templates
.env.example        Template for environment variables
```

## Architecture decisions

- **Session-based auth only** — `communities.py` was rewritten to remove `flask_login` dependency; all auth uses `session['user_id']`
- **WitnessRequest model added** — was imported by `ussd.py`/`trust_graph.py` but missing from models; added as a proper SQLAlchemy model
- **TrustEvent factor columns added** — `f_repayment`, `f_witness`, `f_network`, `f_activity` columns added to persist per-event factor breakdown
- **Community seed uses `admin_user_id=None`** — avoids FK violation on fresh DB when no users exist yet
- **M-Pesa/USSD are optional** — both integrations gracefully require env vars; app works without them for the web flow

## Product

- User registration via web form (phone + name) or USSD
- Round-up micro-savings to build sub-wallet balance
- Care fund requests with peer witness verification (3 witnesses)
- Community pools with admin governance for large/emergency requests
- Trust score history with factor breakdown (repayment, witness, network, activity)
- Provider registry with invoice submission
- USSD full-menu flow via Africa's Talking

## User preferences

_Populate as you build_

## Gotchas

- `providers.py` in root is an HTML template accidentally named `.py` — actual providers blueprint is `providers_bp.py`
- USSD blueprint (`ussd.py`) creates `WitnessRequest` rows; web flow creates `CareRequest` rows — both appear in witness dashboard
- M-Pesa callback requires a publicly reachable URL set via `MPESA_CALLBACK_URL` env var

## Pointers

- Africa's Talking simulator: https://developers.africastalking.com/simulator
- M-Pesa Daraja sandbox: https://developer.safaricom.co.ke/
