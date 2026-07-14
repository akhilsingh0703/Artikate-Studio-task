# ANSWERS

Written reasoning for each section of the assessment. Every explanation points at
the exact module it describes, so the code and the reasoning can be read side by
side.

**Contents**

- [Section 1 — Diagnosing a broken system](#section-1--diagnosing-a-broken-system)
- [Section 2 — SIGKILL and in-flight tasks](#section-2--sigkill-what-happens-to-in-flight-tasks)
- [Section 3 — Thread-local scoping under async Django](#section-3--thread-local-tenant-scoping-under-async-django)
- [Section 4 — Architecture review (A & B)](#section-4--architecture-review)

---

## Section 1 — diagnosing a broken system

The endpoint is `/api/orders/summary/`. It normally returns in ~80ms, but after
a deploy it started timing out (30s+) for users with 200+ orders — and the view
itself wasn't changed.

### Investigation log (what I checked, in order)

1. Is it everyone or a subset? The reports are only from users with more than
   ~200 orders, and the latency tracks the order count. That points at work that
   grows per row, not a global outage (which would hit everyone) or an infra
   problem.

2. What changed if the view didn't? Per-row latency with no view change is the
   classic signature of an N+1 that some *related* change unmasked — a serializer
   field, a model `__str__`, a signal, a new `related_name` access. "The view
   didn't change" is a red herring; the extra queries come from code the view
   calls.

3. Profile it. I wired up django-silk and hit the endpoint for a heavy customer.
   Silk's request page shows the SQL query count and total DB time. The naive
   endpoint fires 1 + 2N queries (one for the orders, then one for `customer` and
   one for `items` per order). At N=250 that's ~501 queries, each a round trip —
   a few ms times a few hundred is the 30-second timeout.

4. Rule out the look-alikes. A missing index? No — the problem is the *number* of
   queries, not one slow scan. Serializer overhead? The Python work is trivial;
   the round trips dominate. A cache issue? There's no cache in this path.

### Root cause

An N+1: the serializer touches `order.customer` and `order.items` for every row
while the queryset only selected `Order` rows. See
`orders/views.py::summary_naive` and `orders/serializers.py`.

### The fix, and why it works

`orders/views.py::order_summary`:

```python
Order.objects.select_related("customer").prefetch_related(
    Prefetch("items", queryset=OrderItem.objects.only(...))
)
```

- `select_related("customer")` — `customer` is a forward FK (to-one), so Django
  resolves it with a JOIN in the initial query. `order.customer.name` is already
  in memory, no extra round trips.
- `prefetch_related("items")` — `items` is a reverse FK (to-many); a JOIN would
  multiply rows, so Django runs one more query (`... WHERE order_id IN (...)`)
  and stitches the children onto their parents in Python. That's N queries
  collapsed into one.
- Net: 2 queries no matter how many orders, instead of 1 + 2N. `.only(...)` trims
  the columns pulled for items.
- The serializer helpers use `len(order.items.all())` and iterate
  `order.items.all()`, both of which read the prefetched cache. Calling
  `.count()` there would issue fresh SQL and bring the N+1 back.

### Before/after (django-silk)

Reproduce it locally (see the README) and compare at `/silk/`:

- `/api/orders/summary/naive/` — ~501 queries for a 250-order customer (1 + 2N)
- `/api/orders/summary/` — 2 queries

The same counts are asserted in `orders/tests.py`
(`test_naive_endpoint_has_n_plus_one`, `test_fixed_endpoint_is_constant`).

---

## Section 2 — SIGKILL: what happens to in-flight tasks?

`SIGKILL`, unlike `SIGTERM`, can't be trapped — the worker dies immediately with
no cleanup and no chance to ack or requeue. What actually happens to a running
task comes down to *when* the message was acked to the broker.

With default Celery (`acks_late=False`) a task is acked the moment it's received,
before it runs. A worker killed mid-run has already acked, so the broker thinks
the job is done and it's silently lost. That's the failure mode the task is
warning about.

This project sets `CELERY_TASK_ACKS_LATE = True`, so the message is acked only
after `send_email_task` returns. A `SIGKILL` mid-run means it was never acked, so
once the broker connection drops, Redis's visibility-timeout makes the message
visible again and it's redelivered to another worker. Two more settings back this
up: `CELERY_TASK_REJECT_ON_WORKER_LOST = True` requeues a lost worker's task
instead of marking it failed, and `CELERY_WORKER_PREFETCH_MULTIPLIER = 1` means a
worker holds at most one un-acked message, so a crash can strand at most one job.

The price of `acks_late` is at-least-once delivery: a worker killed *after* the
send succeeded but *before* the ack will get the job redelivered. That's handled
by idempotency — `process_email_job` returns early when `job.status == SENT`, so
the email is never sent twice. The `EmailJob` row is the source of truth, not the
broker, which is what lets the burst test actually prove "no job is lost" across
a crash.

---

## Section 3 — thread-local tenant scoping under async Django

`tenants/context.py` keeps the current tenant in `threading.local()`. Under the
usual WSGI, one-request-per-thread model that's fine: the middleware binds the
tenant at the start of the request and clears it in a `finally`, so the value
never escapes that request's thread.

It breaks under async, though:

- An `async def` view runs on the event loop, and every `await` lets the loop
  interleave other requests on the same thread. A `threading.local` value set for
  request A is still bound when the loop switches to request B on that thread, so
  B reads A's tenant. That's a cross-tenant leak — exactly what this system
  exists to prevent.
- Django also bridges sync and async through `sync_to_async` / `asgiref.sync`,
  whose thread pool reuses threads. A tenant left in a pooled thread's local can
  bleed into a later, unrelated task on that same thread.
- Awaiting across a boundary can resume the coroutine on a *different* pool
  thread, so a value set before the `await` may not even be visible after it.

The fix is `contextvars.ContextVar`. A `ContextVar` is bound to the logical
context (the coroutine / `Task`), not the OS thread. `asyncio` copies the context
per `Task`, so concurrent coroutines each see their own value even on one thread,
and `asgiref` propagates the context across the sync/async boundary. In practice:
`tenant_var = ContextVar("tenant")`, set it with `tenant_var.set(t)` in an
async-aware middleware, read it with `tenant_var.get(None)` in the manager, and
reset it with the returned token (`tenant_var.reset(token)`) in a `finally` so
nothing leaks into the next task on the loop. It also works fine under sync/WSGI,
so it's a safe drop-in for both.

---

## Section 4 — architecture review

Answering questions A and B.

### A. Django admin is slow on a table with 500,000+ rows (PK already indexed)

The PK index doesn't help here because the changelist's cost isn't PK lookups.
Three things usually dominate.

First, N+1 queries from `list_display`. Any related field rendered per row (say
`order.customer`) fires one query per row. The fix is
`list_select_related = ["customer"]` so it JOINs in one query; for reverse or
many relations, override `ModelAdmin.get_queryset` and add `prefetch_related`.

Second, the paginator's `SELECT COUNT(*)`. The changelist runs a full count to
render "1 of N" and the page range, and on 500k+ rows that count scans the table
on every page load. You can set `show_full_result_count = False` to stop the
expensive full count, and/or plug in a cheaper paginator via
`ModelAdmin.paginator` that returns an estimate (Postgres `reltuples`, or
`SHOW TABLE STATUS` on MySQL).

Third, unindexed filtering, search and ordering. `list_filter`, `search_fields`
and `ordering` generate `WHERE` / `ILIKE` / `ORDER BY` on columns that often
aren't indexed, which forces sequential scans and filesorts. Add `Meta.indexes`
(or `db_index=True`) on exactly those columns; replace substring `search_fields`
(which produce non-sargable `%term%`) with prefix search, or a `GinIndex` +
`SearchVector` for Postgres full-text; and prefer `list_filter` entries backed by
indexed columns, e.g. a `DateFieldListFilter` on an indexed date.

The trade-offs: an estimated count is fast but not exact, and every extra index
costs something on writes — both fine for an admin table that's read far more
than it's written.

### B. Offset vs cursor pagination for a 10,000-record endpoint

Offset pagination (`LIMIT n OFFSET m`) is simple and lets you jump to any page,
but the database has to scan and throw away all `m` rows before the ones you
want. `OFFSET 9900` reads 9,900 rows to return 100, so deep pages get linearly
slower — that's Django's `PageNumberPagination`. It's also unstable for infinite
scroll: if rows are inserted or deleted while the user pages, the offsets shift
and they see duplicated or skipped items.

Cursor pagination
(`WHERE (sort_key) > last_seen ORDER BY sort_key LIMIT n`) encodes the position
as the last row's sort value. The DB seeks straight to that key through the index
and reads only the next `n` rows, so it's constant time no matter how deep you
are. It's also stable under mutation: inserts and deletes before the cursor don't
change what comes after it, so infinite scroll never duplicates or skips. Django
ships `CursorPagination`. The costs are real, though: it needs a stable, unique,
indexed ordering (usually `-created_at, id` to break ties), it can only go
next/previous rather than jump to an arbitrary page, and you don't get a total
count or page numbers.

Which one depends on the access pattern. For feeds and mobile infinite scroll —
high volume, append-heavy, forward-only, where deep-page speed and
mutation-stability matter — cursor is the right call. Offset is fine for small,
bounded, admin-style tables where users want page numbers and random access and
the dataset is small enough that scanning discarded rows is cheap. For 10,000
rows behind an infinite scroll, I'd default to cursor.
