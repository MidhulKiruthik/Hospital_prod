# Hospital Operations Platform Report (Repository-Aligned)

## 1. Executive Summary

This repository implements a cloud-native hospital operations platform with:

- role-based authentication and authorization (admin, doctor, receptionist, patient)
- workload-aware appointment booking and reassignment
- asynchronous task execution with Celery and Redis
- clinical summary generation with rule-based NLP and optional transformer mode
- short-horizon forecasting with baseline and optional advanced paths
- operational monitoring through Prometheus and Grafana
- backup and disaster-recovery tooling for SQLite and PostgreSQL targets

Current runtime deployment is Docker Compose in `docker-compose.yml`.

## 2. Current Runtime Architecture

```text
Browser
  |
  v
Nginx frontend container (ports 8080/8443)
  - serves SPA
  - proxies /api/* to Flask API
  |
  v
Flask API container
  - auth + RBAC + refresh sessions
  - appointments + scheduling
  - summaries + revisions + review
  - forecasting + history
  - health + metrics + ops
  |
  +--> PostgreSQL 16 (primary runtime DB)
  |
  +--> Redis
         |
         +--> Celery worker
         +--> Celery beat

Prometheus (localhost:9090) <- scrapes /metrics
Grafana (localhost:3000) <- reads Prometheus
```

## 3. Key Correction vs Earlier Report

The active Compose stack is PostgreSQL-first, not SQLite-first.

- `postgres` service is present and required by `api`, `celery_worker`, and `celery_beat`.
- `DATABASE_URL` defaults to a PostgreSQL DSN in Compose.
- Alembic migrations are enabled by default in API startup (`AUTO_RUN_MIGRATIONS=True`).
- SQLite support still exists as a fallback path, mainly for local/dev utility flows.

## 4. Repository Structure (Operationally Relevant)

- `backend`: Flask API, auth, models, scheduling, forecasting, NLP, tasks, Alembic migrations
- `frontend`: deployed Vue/Nginx SPA assets and reverse-proxy config
- `monitoring`: Prometheus config, alert rules, Grafana provisioning/dashboard
- `scripts`: stack check, restore helper, DR drill runner
- `tests`: unittest suite for app flows, security/config, forecasting/NLP, TLS/ops/DR/payment
- `frontend-react-auth`: separate React/Vite workspace present in repo but not wired into active Nginx routes
- `frontend/react-auth`: built static React auth assets also present, but active Nginx currently routes all non-API traffic to Vue SPA fallback

## 5. Backend Architecture

### 5.1 App bootstrap and config

`backend/app.py` + `backend/config.py` handle:

- config loading and production validation
- SQLAlchemy initialization
- CORS policy selection by environment
- auto migration (`AUTO_RUN_MIGRATIONS`) or schema create fallback (`AUTO_CREATE_SCHEMA`)
- optional bootstrap admin creation
- optional demo seed data

### 5.2 Data model

`backend/models.py` includes core and operational entities:

Core domain:

- `User`
- `Doctor`
- `Patient`
- `Appointment`
- `ClinicalSummary`
- `WorkloadMetric`
- `Department`
- `Notification`
- `AuditLog`

New/extended operational and security entities:

- `ClinicalSummaryRevision`
- `ForecastHistory`
- `AuthSession`
- `SecurityEvent`
- `AsyncTaskEvent`

Compatibility/profile entities:

- `DoctorProfileCompat`
- `PatientProfileCompat`
- `DoctorAvailabilityCompat`
- `AppointmentDiagnosisCompat`
- `PaymentOrderCompat`

### 5.3 Migration status

Migration framework is implemented with Alembic under `backend/alembic`.

- `b8a2bcd5965a_initial_schema.py`
- `4f4fe31e6a32_add_security_and_audit_hardening.py`

So schema evolution is no longer only runtime patching; formal migration history exists.

### 5.4 Auth and security controls

From `backend/auth.py`, `backend/app.py`, `backend/models.py`, and `backend/config.py`:

- password hashing and policy validation
- JWT access tokens
- refresh-token sessions (`AuthSession`) with revoke/logout/logout-all support
- role-based route protection
- login lockout support (`LOGIN_MAX_FAILED_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES`)
- append-only audit behavior at model layer (`AuditLog` update/delete blocked)
- audit integrity verification endpoint (`/api/audit/integrity`)
- security event capture and optional webhook export (`SECURITY_EVENT_*` config)
- rate limiting with Flask-Limiter and Redis-backed storage

## 6. Scheduling and Load Balancing

Implemented in `backend/scheduler.py`.

Workload score:

$$
\text{score} = 0.40\cdot\text{appointment\_ratio} + 0.30\cdot\text{avg\_consult\_pressure} + 0.20\cdot\text{cancellation\_penalty} + 0.10\cdot\text{queue\_pressure}
$$

Key behavior:

- overload threshold: `0.85`
- preferred doctor supported
- reassignment to same-specialty alternative if overloaded
- emergency override path
- slot availability and break-window checks via `DoctorAvailabilityCompat`
- conflict prevention for double-booking same doctor/time

## 7. Forecasting

Implemented in `backend/forecaster.py` with orchestration in `backend/tasks.py`.

Modes:

- `baseline`
- `arima`
- `lightgbm`
- `auto`

Baseline path uses lightweight statistical logic and is available without heavy optional dependencies. Forecast outputs are cached in Redis and persisted in `ForecastHistory`.

## 8. Clinical NLP and Summary Workflow

Implemented in `backend/nlp.py` and `backend/tasks.py`.

- default rule-based extraction for symptoms, diagnoses, medications, procedures, vitals, duration
- optional transformer summarization path
- async generation through Celery task `tasks.generate_clinical_summary`
- sync fallback when queue path is unavailable
- summary metadata persisted (`generation_method`, `generation_model`, `processing_time_s`, hash fields)
- review and revision support via `ClinicalSummaryRevision`

## 9. Background Processing

Celery setup: `backend/celery_worker.py` + `backend/tasks.py`.

Beat schedule:

- workload snapshots every 30 seconds
- forecast refresh every 60 seconds
- backups every 15 minutes

Task event telemetry is persisted in `AsyncTaskEvent` and exposed by ops endpoints.

## 10. Frontend and Routing Reality

Active runtime frontend is still the Vue SPA under `frontend/assets/js` served by Nginx.

- Nginx proxy routes `/api/*` to Flask API
- Nginx fallback routes all non-API paths to `/index.html`
- Vue router guards role-specific sections (`/admin`, `/doctor`, `/patient`)
- JWT token injection and 401 redirect handling are configured in frontend runtime code

React auth assets/workspace exist in repo (`frontend/react-auth` and `frontend-react-auth`), but active Nginx config does not currently map `/login` or `/register` to React-specific entrypoints.

## 11. API Surface Overview

`backend/app.py` currently defines a large API surface (84 route decorators).

Major families:

- health and deep health
- auth (`login`, `register`, `refresh`, `logout`, `logout-all`, `me`)
- doctor/patient/admin compatibility endpoints
- appointments (book/complete/cancel/reschedule)
- summaries (get/regenerate/review/revisions)
- forecasting (`workload`, `demand`, `history`, `best-doctor`)
- dashboard/metrics/ops endpoints
- notifications, audit, security events
- public discovery endpoints

Representative ops/security endpoints:

- `/api/health`
- `/api/health/deep`
- `/metrics`
- `/api/audit/integrity`
- `/api/security/events`
- `/api/ops/worker-status`
- `/api/ops/tasks/events`

## 12. Monitoring and Alerting

Monitoring stack is fully wired in Compose.

- Prometheus scrape config: `monitoring/prometheus/prometheus.yml`
- Prometheus alert rules: `monitoring/prometheus/alerts.yml`
- Grafana datasource/dashboard provisioning under `monitoring/grafana`

Alert rules include:

- DB connectivity down
- Redis connectivity down
- stale backup age
- stale worker heartbeat

Prometheus and Grafana are bound to localhost (`127.0.0.1`) by default in Compose.

## 13. Backup and Disaster Recovery

Backup task (`tasks.run_backup`) supports both DB schemes:

- SQLite: `.db` copy backup
- PostgreSQL: `.sql` backup via `pg_dump`

Additional behavior:

- retention cleanup
- SHA-256 checksum generation
- task event logging

Restore and drill tooling:

- `scripts/restore_backup.py` supports sqlite/postgres restore, optional checksum verification, and strict sqlite path validation (filesystem-only, no `:memory:` restore target)
- `scripts/run_dr_drill.py` executes restore drills and writes machine-readable report JSON including timestamps, duration, exit status, stdout/stderr tails, and parsed `DR_REPORT`
- `scripts/check_stack.py` checks health/deep-health/dashboard/metrics and now defaults to frontend-proxied endpoints (`http://localhost:8080`) with overrides via `HOSPITAL_FRONTEND_BASE` or `HOSPITAL_API_BASE`

## 14. Infrastructure and Deployment

Compose services:

- `postgres`
- `redis`
- `api`
- `celery_worker`
- `celery_beat`
- `frontend`
- `prometheus`
- `grafana`

Public/edge ports:

- `8080` HTTP frontend
- `8443` HTTPS frontend

Localhost-bound ports:

- `9090` Prometheus
- `3000` Grafana

Internal-only by default:

- Flask API
- Redis
- PostgreSQL

## 15. Testing Status

Repository includes broad unittest coverage under `tests`, including:

- scheduling/availability constraints
- app flow behavior
- forecasting/NLP behavior
- transformer mode handling
- payment flow
- backup/restore and DR drill scripts
- ops script URL/path parsing safeguards (`test_ops_scripts.py`, `test_dr_drill.py`)
- TLS and production config validation
- PostgreSQL configuration/SQL generation checks

## 16. Strengths

- real scheduling logic with deterministic overload handling
- asynchronous processing integrated with fallback paths
- formal migration framework (Alembic) now present
- stronger security posture (refresh sessions, lockout, audit integrity)
- integrated monitoring and alerting
- practical DR tooling with automated drill report generation

## 17. Current Gaps / Risks

- active frontend runtime still points to Vue auth paths; React auth assets are present but not fully wired by Nginx
- production readiness still depends on correct secret/cert/env management at deployment time
- PostgreSQL backup/restore relies on external client tooling availability (`pg_dump`, `psql`) and proper network/credential setup
- advanced forecasting/NLP paths still depend on optional packages and sufficient data history

## 18. Overall Assessment

This codebase is a strong cloud-native operations prototype with substantial implementation depth across scheduling, async processing, forecasting, observability, and recovery tooling.

Compared with earlier project status, the architecture has materially matured due to PostgreSQL-centered deployment, Alembic migrations, expanded security/session controls, and richer operational instrumentation.
