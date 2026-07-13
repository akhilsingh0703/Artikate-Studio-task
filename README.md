# Artikate Studio — Backend Developer Assessment

Django backend covering all four assessment sections. Written reasoning lives in
[`ANSWERS.md`](./ANSWERS.md); the Section 2 architecture write-up is in
[`DESIGN.md`](./DESIGN.md).

| Section | Topic | Where |
| --- | --- | --- |
| 1 | N+1 diagnosis & fix + django-silk evidence | `orders/` · `ANSWERS.md` §1 |
| 2 | Rate-limited async job queue (Celery + Redis) | `emails/` · `DESIGN.md` |
| 3 | Multi-tenant ORM isolation | `tenants/` · `ANSWERS.md` §3 |
| 4 | Written architecture review (Q A & B) | `ANSWERS.md` §4 |

## Requirements

- Python 3.10+
- Redis (for the rate limiter and Celery broker) — `redis-server`

## Setup (under 5 minutes)

```bash
# 1. Create a virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Make sure Redis is running (default redis://127.0.0.1:6379/0)
redis-server --daemonize yes        # or: sudo service redis-server start

# 3. Migrate and create an admin user
python manage.py migrate
python manage.py createsuperuser     # optional, for /admin and /silk

# 4. Run the server
python manage.py runserver
```

Everything is configured with sane local defaults; no `.env` is required. All
tunables (Redis URL, rate limit, JWT secret) are environment variables read in
`config/settings.py`.

## Run the tests

```bash
python manage.py test
```

19 tests. The Section 2 rate-limiter/queue tests require a running Redis; they
**skip** automatically if Redis is unavailable. The 500-job burst test is the
slowest (it drives the real limiter through thousands of Redis operations).

## Section 1 — N+1 demo (see the fix with django-silk)

```bash
# Seed a customer with 250 orders (above the incident's 200-order threshold)
python manage.py seed_orders --orders 250 --items 3
python manage.py runserver
```

Then compare the two endpoints and their query counts at **http://127.0.0.1:8000/silk/**:

- Slow (N+1):  `GET /api/orders/summary/naive/?customer_id=<id>`  → ~501 queries
- Fixed:       `GET /api/orders/summary/?customer_id=<id>`        → 2 queries

Full investigation log, root-cause justification, and the DB/ORM-level
explanation of the fix are in `ANSWERS.md` §1.

## Section 2 — Rate-limited email queue (Celery + Redis)

Open three terminals (all with the venv active):

```bash
# Terminal 1 — Redis (if not already running)
redis-server

# Terminal 2 — Celery worker
celery -A config worker -l info

# Terminal 3 — submit a burst of 500 jobs, 10% intentionally invalid
python manage.py migrate
python manage.py submit_emails --count 500 --fail-rate 0.1
```

Watch the worker drain jobs at ~200/minute (the token bucket throttles the rest
with retries — no `time.sleep`). Inspect state:

```bash
redis-cli llen celery                # queued jobs remaining
redis-cli hgetall ratelimit:email:global   # token bucket contents
```

Sent/failed/dead-lettered jobs are visible in the admin under **Email jobs** and
**Dead letters** (http://127.0.0.1:8000/admin/).

- Architecture trade-offs, token-bucket rationale, atomicity, and the SIGKILL
  behaviour: `DESIGN.md` and `ANSWERS.md` §2.

## Section 3 — Multi-tenant isolation

- `tenants/models.py` — `TenantManager` auto-applies `.filter(tenant=...)` to
  **every** queryset (`.all()`, `.filter()`, `.get()`), so a forgotten filter
  cannot leak data. `all_objects` is the deliberate unscoped escape hatch.
- `tenants/middleware.py` — resolves the tenant from a JWT (`Authorization:
  Bearer`), an `X-Tenant` header, or the subdomain, binds it for the request,
  and clears it in a `finally` block.
- Tests prove the *negative* (tenant A cannot read tenant B by filter or by pk,
  and `.all()` does not bypass scoping): `tenants/tests.py`.
- Async safety / `contextvars` discussion: `ANSWERS.md` §3.

## Project layout

```
config/     Django project + Celery app (config/celery.py)
orders/     Section 1 — Customer/Order/OrderItem, summary endpoint, seed command
emails/     Section 2 — EmailJob/DeadLetter, token-bucket limiter, Celery tasks
tenants/    Section 3 — Tenant/Project, TenantManager, tenant middleware
```

## Notes

- No secrets or `.env` files are committed (`.gitignore` covers them). The
  `SECRET_KEY` default is clearly marked local-only.
- `db.sqlite3` is created locally and git-ignored.
