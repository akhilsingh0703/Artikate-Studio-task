# Artikate Studio — Backend Assessment

A Django backend covering the four assessment sections. The written reasoning is
split across `ANSWERS.md` (Sections 1, 3 and 4) and `DESIGN.md` (the Section 2
queue design).

Section map:

- Section 1 — N+1 diagnosis and fix, with silk evidence — `orders/`, `ANSWERS.md` §1
- Section 2 — rate-limited async email queue (Celery + Redis) — `emails/`, `DESIGN.md`
- Section 3 — multi-tenant ORM isolation — `tenants/`, `ANSWERS.md` §3
- Section 4 — architecture write-up (questions A and B, plus bonus C) — `ANSWERS.md` §4
- Section 5 (optional) — live demo recording — [`Live System Recording.mp4`](./Live%20System%20Recording.mp4)

## Requirements

- Python 3.10+
- Redis, for the rate limiter and the Celery broker

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Redis, if it isn't already up (default redis://127.0.0.1:6379/0)
redis-server --daemonize yes        # or: sudo service redis-server start

python manage.py migrate
python manage.py createsuperuser     # optional, for /admin and /silk
python manage.py runserver
```

There's no `.env` to fill in — everything has a local default. The tunables
(Redis URL, rate limit, JWT secret) are read from env vars in
`config/settings.py` if you want to override them.

## Tests

```bash
python manage.py test
```

19 tests. The Section 2 limiter/queue tests need Redis running and skip
themselves if it isn't reachable. The 500-job burst test is the slow one — it
pushes the real limiter through thousands of Redis calls, so the suite takes a
couple of minutes.

## Section 1 — seeing the N+1 in silk

```bash
python manage.py seed_orders --orders 250 --items 3
python manage.py runserver
```

Then open http://127.0.0.1:8000/silk/ and compare the query counts for the two
endpoints:

- `GET /api/orders/summary/naive/?customer_id=<id>` — ~501 queries (the bug)
- `GET /api/orders/summary/?customer_id=<id>` — 2 queries (the fix)

The investigation log and the DB/ORM explanation are in `ANSWERS.md` §1.

## Section 2 — running the email queue

Three terminals, all with the venv active:

```bash
# 1: Redis (skip if already running)
redis-server

# 2: Celery worker
celery -A config worker -l info

# 3: fire 500 jobs, 10% of them intentionally invalid
python manage.py migrate
python manage.py submit_emails --count 500 --fail-rate 0.1
```

The worker drains at ~200/min; the sliding-window limiter throttles the rest by
rescheduling retries rather than sleeping. Poke at the state with:

```bash
redis-cli llen celery
redis-cli zcard ratelimit:email:global
```

Sent/failed/dead-lettered jobs show up in the admin under Email jobs and Dead
letters. The trade-offs and the SIGKILL story are in `DESIGN.md` and
`ANSWERS.md` §2.

## Section 3 — tenant isolation

- `tenants/models.py` — `TenantManager` adds `.filter(tenant=...)` to every
  queryset (`.all()`, `.filter()`, `.get()`), so a forgotten filter can't leak
  another tenant's rows. `all_objects` is the deliberate unscoped escape hatch.
- `tenants/middleware.py` — resolves the tenant from a JWT, an `X-Tenant`
  header, or the subdomain, binds it for the request and clears it in a
  `finally`.
- `tenants/tests.py` — proves the negative: tenant A can't reach B's rows by
  filter or by pk, and `.all()` doesn't bypass the scoping.

The async-safety / `contextvars` discussion is in `ANSWERS.md` §3.

## Layout

```
config/     Django project + Celery app (config/celery.py)
orders/     Section 1 — Customer/Order/OrderItem, summary endpoint, seed command
emails/     Section 2 — EmailJob/DeadLetter, sliding-window limiter, Celery tasks
tenants/    Section 3 — Tenant/Project, TenantManager, tenant middleware
```

## Notes

- No secrets or `.env` files are committed. The `SECRET_KEY` default is marked
  local-only.
- `db.sqlite3` is created locally and git-ignored.
