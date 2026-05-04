# SolidarityPool Prototype

A community mutual-aid application where users build a communal pool through micro round-ups and can request care funds backed by a trust graph and peer witness verification.

## Architecture

| Layer | Technology |
|-------|-----------|
| Web framework | Flask 2.3 |
| ORM | Flask-SQLAlchemy |
| Database | PostgreSQL (Replit-managed via `DATABASE_URL`) |
| Authentication | Flask-Login |
| Logging | Loguru → `logs/solidarity.log` |
| USSD | Africa's Talking webhook (`/ussd/callback`) |
| Mobile payments | M-Pesa Daraja STK Push (`/mpesa/stk_push`) |
| Production server | Gunicorn |

## Key Files

```
app.py              Main Flask application (routes, blueprints, error handlers)
models.py           SQLAlchemy models (User, Transaction, WitnessRequest,
                    SystemState, MpesaTransaction)
trust_graph.py      Draw-ceiling calculator (Neo4j-upgrade-ready)
witness.py          Weighted witness selection + accuracy tracking
recovery.py         Round-up intensifier adaptive recovery
ussd.py             Africa's Talking USSD Blueprint (/ussd/callback)
mpesa.py            M-Pesa Daraja STK Push + callback parser
templates/          Jinja2 HTML templates
logs/               Rotating log files (auto-created)
.env.example        Template for required environment variables
requirements.txt    Python dependencies
```

## Environment Variables

Copy `.env.example` to `.env` and fill in credentials. Required for live integrations:

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Flask session signing key |
| `DATABASE_URL` | PostgreSQL connection string (auto-set by Replit) |
| `AT_USERNAME` | Africa's Talking username (`sandbox` for testing) |
| `AT_API_KEY` | Africa's Talking API key |
| `MPESA_ENV` | `sandbox` or `production` |
| `MPESA_CONSUMER_KEY` | Safaricom Developer Portal |
| `MPESA_CONSUMER_SECRET` | Safaricom Developer Portal |
| `MPESA_PASSKEY` | Lipa na M-Pesa passkey |
| `MPESA_SHORTCODE` | Till / Paybill number |
| `MPESA_CALLBACK_URL` | Public URL for payment confirmations |

## Five Production Upgrades (implemented)

### 1. USSD Integration (Africa's Talking)
- Blueprint at `ussd.py`, mounted on `/ussd/callback`
- Full menu flow: register, check balance, simulate round-up, request care, trust score
- Handles CON/END responses per Africa's Talking protocol
- Test using the AT simulator at https://developers.africastalking.com/simulator

### 2. M-Pesa Daraja API
- `mpesa.py` handles access token, STK Push, and callback parsing
- Route `POST /mpesa/stk_push` (login required) initiates payment
- Route `POST /mpesa/callback` receives Safaricom confirmation and credits sub-wallet
- Stores every transaction in `MpesaTransaction` table with status tracking

### 3. Neo4j-Ready Trust Graph
- `trust_graph.py` retains SQLAlchemy implementation for current scale
- File contains the equivalent Cypher query and migration instructions as comments
- Custom `TrustGraphError` exception for explicit failure handling

### 4. Smarter Witness Selection
- `witness.py` uses weighted random sampling (no longer purely random)
- Weight factors: base 1.0 + accuracy bonus (up to +1.0) + region-match bonus (+0.5)
- Anti-collusion discount applied when pool ≥ 10 users
- `record_witness_outcome()` updates each witness's `witness_accuracy_score` after resolution
- New User fields: `witness_accuracy_score`, `region_prefix`, `total_witness_calls`, `correct_witness_calls`

### 5. Production Code Quality
- **Flask-Login**: `@login_required` on all protected routes; `login_user`/`logout_user`
- **Loguru**: Structured logs with rotation at `logs/solidarity.log`
- **Custom exceptions**: `TrustGraphError`, `WitnessSelectionError`, `RecoveryError`, `MpesaError`
- **Specific error handling**: Catches `ValueError`, `KeyError`, domain-specific exceptions — no bare `except`
- **HTTP error handlers**: 400, 403, 404, 500 with `templates/error.html`
- **Environment variables**: `SECRET_KEY` and `DATABASE_URL` via `python-dotenv`
- **Duplicate-vote guard**: Prevents same witness voting twice on a request

## Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials
python app.py
```

## Deployment

Configured for Gunicorn autoscale deployment:
```
gunicorn --bind=0.0.0.0:5000 --reuse-port app:app
```
