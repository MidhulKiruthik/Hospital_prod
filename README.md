# Cloud-Native Intelligent Hospital Operations Platform
**BCSE408L — Cloud Computing | DA1 Project**

> Group: Midhul Kiruthik M · Madhan Karthikeyan · Sachin VP · Sharvesh

---

## Architecture Overview

```
VueJS Frontend ──HTTPS REST──► Flask API (Stateless)
                                    │
                              ┌─────┴──────┐
                              │  JWT RBAC  │
                              └─────┬──────┘
                    ┌───────────────┼─────────────────┐
                    ▼               ▼                  ▼
               Redis Cache   Redis Broker        SQLite DB
               (workload     (task queue)        (persistent)
               forecasts)         │
                              Celery Workers
                              ├── NLP Summary Generator
                              ├── Workload Snapshot (30s)
                              ├── Forecast Updater (60s)
                              └── Backup Snapshot (every 15 min)
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vue.js 3 · Chart.js 4 |
| Backend API | Flask 3 · Python 3.11 |
| Task Queue | Celery 5.x |
| Cache / Broker | Redis 6.x |
| Database | SQLite (prototype) / PostgreSQL (prod) |
| NLP | Rule-based + optional DistilBERT/T5 |
| Monitoring | Prometheus + Grafana (via metrics endpoint) |
| Auth | JWT + RBAC |
| API Protection | Flask-Limiter rate limits |
| Data Protection | Encrypted clinical summaries at rest |

---

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
git clone <repo>
cd hospital-ops
docker-compose up --build
```

- Frontend:  http://localhost:8080
- Frontend (TLS): https://localhost:8443
- API:       http://localhost:5000
- API Docs:  http://localhost:5000/api/health
- Prometheus metrics: http://localhost:5000/metrics

### Option 2: Local Development

**Prerequisites:** Python 3.11+, Redis running on localhost:6379

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy env config
cp ../.env.example .env

# Run Flask API
python app.py

# In another terminal: Run Celery Worker
celery -A celery_worker.celery worker --loglevel=info

# In another terminal: Run Celery Beat (periodic tasks)
celery -A celery_worker.celery beat --loglevel=info
```

Open `frontend/index.html` in your browser (or serve with any static server).

---

## Demo Login Credentials

| Role | Username | Password |
|------|----------|----------|
| Admin | `admin` | `admin123` |
| Receptionist | `receptionist` | `recep123` |
| Doctor 1 | `doctor1` | `doc123` |

---

## API Reference

### Authentication
```
POST /api/auth/login          { username, password }
POST /api/auth/register       { username, password, role, email }
GET  /api/auth/me
```

### Appointments
```
GET  /api/appointments        ?status=booked&date=2026-02-09&page=1
POST /api/appointments        { patient_id, specialty, scheduled_at, priority, notes }
PUT  /api/appointments/:id/complete   { notes }
PUT  /api/appointments/:id/cancel     { reason }
```

### Scheduling / Workload
```
GET  /api/doctors/workloads              All doctor workload scores
GET  /api/doctors/:id/workload           Single doctor score
GET  /api/forecast/best-doctor?specialty=Cardiology&at=2026-02-09T10:00
```

### Forecasting
```
GET  /api/forecast/workload?doctor_id=1&horizon=120
GET  /api/forecast/demand?hours=4
```

### Clinical Summaries
```
GET  /api/summaries/:appointment_id
POST /api/summaries/:appointment_id/regenerate
```

### Dashboard & Audit
```
GET  /api/dashboard
GET  /api/audit              (admin only)
GET  /api/notifications
GET  /metrics                (Prometheus format)
```

---

## Workload Score Formula

```
score = 0.40 × appointment_ratio
      + 0.30 × avg_consult_pressure
      + 0.20 × cancellation_penalty
      + 0.10 × queue_pressure

Overload threshold = 0.85
```

When a doctor's score exceeds the threshold, the Smart Appointment Load Balancer
automatically reassigns incoming bookings to the next available doctor with the
lowest workload score.

---

## Reliability Targets

| Metric | Target |
|--------|--------|
| Availability | 99.9% |
| RPO | 15 minutes |
| RTO | 30 minutes |
| Forecast cadence | 30–60 seconds |

Current implementation notes:
- Backups run every 15 minutes to align with RPO target.
- Retention is configurable via `BACKUP_RETENTION_HOURS`.
- Backup location is configurable via `BACKUP_DIR`.

---

## Simulation Parameters (Data & Methodology)

- Doctors: 30–50
- Patients/day: 400–600 (Poisson distribution)
- Avg consultation: 8–12 minutes
- Cancellation rate: 10–15%

---

## Project Structure

```
hospital-ops/
├── backend/
│   ├── app.py              # Flask API + all routes
│   ├── config.py           # Configuration
│   ├── models.py           # SQLAlchemy ORM models
│   ├── auth.py             # JWT auth + RBAC middleware
│   ├── scheduler.py        # Smart Appointment Load Balancer
│   ├── forecaster.py       # Workload forecasting engine
│   ├── nlp.py              # Clinical NLP processor
│   ├── tasks.py            # Celery async tasks
│   ├── celery_worker.py    # Celery entry point
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── index.html          # Vue.js 3 SPA dashboard
├── docker-compose.yml
├── .env.example
└── README.md
```
