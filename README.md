# Cloud-Native Intelligent Hospital Operations Platform

Prototype hospital operations system with workload-aware appointment assignment, short-horizon forecasting, asynchronous clinical summary generation, observability, and backup tooling.

## What Is Implemented

- Smart appointment load balancing with workload scoring and reassignment
- JWT auth with RBAC and audit logging
- Redis + Celery background processing
- Short-horizon workload forecasting with a baseline model and optional advanced model paths
- Rule-based clinical NLP with optional transformer-backed summarization path
- Prometheus metrics and Grafana provisioning
- Periodic backups with checksum output (SQLite and PostgreSQL paths)
- Docker Compose deployment for EC2 or local environments

## Stack

- Frontend: Vue.js + Nginx
- Backend: Flask + SQLAlchemy
- Queue / broker: Celery + Redis
- Monitoring: Prometheus + Grafana
- Database: PostgreSQL (containerized) with Alembic migrations
- Optional advanced modeling: statsmodels, LightGBM, transformers

## Quick Start

1. Copy environment settings:

```bash
cp .env.example .env
```

2. Edit `.env` and replace all placeholder secrets.

3. Put real TLS certificate files in `certs/`:

- `certs/fullchain.pem`
- `certs/privkey.pem`

If you are running a local dev demo without real certs, set `ALLOW_SELF_SIGNED_TLS=True`.

4. Start the stack:

```bash
docker compose up --build -d
```

5. Open:

- Frontend: `http://localhost:8080`
- Frontend TLS: `https://localhost:8443`
- API health: `http://localhost:8080/api/health`
- Deep health: `http://localhost:8080/api/health/deep`

## Monitoring

The compose stack now includes Prometheus and Grafana.

- Prometheus: `http://127.0.0.1:9090`
- Grafana: `http://127.0.0.1:3000`

Grafana is provisioned with:

- a Prometheus datasource
- a starter dashboard at `Hospital Operations Overview`

Additional KPIs and signals now exposed:

- scheduling latency (`hospital_scheduling_latency_seconds`)
- reassignment totals (`hospital_reassignment_total`)
- predicted wait time (`hospital_predicted_wait_minutes`)
- NLP duration (`hospital_nlp_duration_seconds`)
- worker heartbeat age (`hospital_worker_heartbeat_age_seconds`)
- DB and Redis status (`hospital_db_up`, `hospital_redis_up`)
- pending summaries and overload risk (`hospital_summaries_pending`, `hospital_overload_risk_doctors`)
- forecast quality (`hospital_forecast_quality_mae`, `hospital_forecast_quality_rmse`)

Prometheus alert rules are included in `monitoring/prometheus/alerts.yml`.

These ports are bound to localhost by default so they are not exposed publicly on EC2 unless you deliberately reconfigure them.

## Security Notes

- Only the frontend ports (`8080`, `8443`) are published by default.
- Redis and the Flask API are internal-only in `docker-compose.yml`.
- Privileged self-registration is disabled by default.
- Demo data is off by default (`SEED_DEMO_DATA=False`).
- A bootstrap admin can be created with:
  - `BOOTSTRAP_ADMIN_USERNAME`
  - `BOOTSTRAP_ADMIN_PASSWORD`
  - `BOOTSTRAP_ADMIN_EMAIL`
- Registration now enforces password complexity (12+ chars, upper/lower/number/special).
- Login lockout is enabled after repeated failures (`LOGIN_MAX_FAILED_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES`).
- Audit logs are append-only at the model layer and support hash-chain integrity checks via `GET /api/audit/integrity`.
- In production/staging, CORS uses `CORS_ORIGINS_PRODUCTION` (localhost origins are stripped).
- Access/refresh token session flow is supported:
  - `POST /api/auth/refresh`
  - `POST /api/auth/logout`
  - `POST /api/auth/logout-all`
- Security event log endpoint: `GET /api/security/events`
- Optional SIEM webhook forwarding via:
  - `SECURITY_EVENT_EXPORT_ENABLED`
  - `SECURITY_EVENT_WEBHOOK_URL`
  - `SECURITY_EVENT_WEBHOOK_TIMEOUT_SECONDS`
  - `SECURITY_EVENT_WEBHOOK_TOKEN`

## Database And Migrations

- Alembic migrations live in `backend/alembic/`.
- API starts with `AUTO_RUN_MIGRATIONS=True` by default in compose.
- Runtime `ALTER TABLE` patching has been removed from app startup.
- Persisted operational tables include:
  - `forecast_history`
  - `auth_sessions`
  - `security_events`
  - `async_task_events`
  - `clinical_summary_revisions`

Manual migration command:

```bash
.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
```

## Forecasting And NLP Modes

Baseline behavior works with the default dependencies.

Optional advanced dependencies are listed in `backend/requirements-advanced.txt`.

Relevant `.env` switches:

- `FORECAST_MODEL=baseline|arima|lightgbm|auto`
- `ENABLE_TRANSFORMER_SUMMARIZATION=True|False`
- `TRANSFORMER_MODEL_NAME=...`

If advanced libraries are unavailable, the app falls back to the baseline forecasting/NLP paths instead of crashing.

Forecast API additions:

- model comparison metadata
- backtest sample output
- MAE/RMSE fields (when available)
- persisted history endpoint: `GET /api/forecast/history`

Clinical summary workflow additions:

- generation mode output (`queued` / `sync` / `sync-fallback`)
- clinician review endpoint: `PUT /api/summaries/<id>/review`
- revision history endpoint: `GET /api/summaries/<id>/revisions`

## Backups And Restore

Celery Beat runs a backup task every 15 minutes.

- SQLite backup output: `.db`
- PostgreSQL backup output: `.sql` (requires `pg_dump`, now installed in backend image)
- Backup task returns SHA-256 checksum for integrity verification

Useful scripts:

```bash
python scripts/check_stack.py
python scripts/restore_backup.py --backup-dir ./backups
python scripts/run_dr_drill.py --backup-dir ./backups --output ./dr_drill_report.json
```

Restore helper supports:

- SQLite restore to local DB path
- PostgreSQL restore via `psql` when `DATABASE_URL` is PostgreSQL
- Optional integrity check: `--verify-sha256 <digest>`
- DR verification output: `--dr-report`

`check_stack.py` defaults to frontend-proxied endpoints (`http://localhost:8080`) and can be overridden with:

- `HOSPITAL_FRONTEND_BASE` (for frontend/proxy checks)
- `HOSPITAL_API_BASE` (direct API host, with or without `/api` suffix)

`run_dr_drill.py` executes a restore drill and writes machine-readable evidence (`dr_drill_report.json`) including:

- drill start/end timestamps
- restore command/exit status
- captured restore `DR_REPORT` payload

Operational APIs:

- `GET /api/ops/worker-status`
- `GET /api/ops/tasks/events`

## EC2 Notes

For the current compose setup, you normally only need inbound rules for:

- `22` for SSH / EC2 Instance Connect
- `8080` for HTTP access
- `8443` for HTTPS access

Do not expose `5000` or `6379` publicly.

## Testing

Run the verification suite with the project virtualenv:

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Demo Credentials

If `SEED_DEMO_DATA=True`, the default seeded users are:

- admin / admin123
- receptionist / recep123
- doctor1 / doc123

Use seeded users only for demos, not public deployment.
