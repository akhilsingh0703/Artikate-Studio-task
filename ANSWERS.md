# ANSWERS.md

Written reasoning for each section. Code lives in the app packages referenced
below.

---

## Section 1 — Diagnose a Broken System

**Endpoint:** `/api/orders/summary/` — ~80ms normally, 30s+ timeouts for users
with 200+ orders after a deploy, *with no change to the view*.

### Incident investigation log (what I checked, in order, and why)

1. **Is it the whole endpoint or a subset of users?** The report says only users
   with **>200 orders** time out, and latency scales with order count. That
   immediately points at **work that grows with the number of rows**, not a
   global outage (which would hit everyone) or an infra problem.

2. **What changed in the deploy, given the view didn't?** Latency that scales
   per-row without a view change is the classic signature of an **N+1 query**
   that a *related* change unmasked — e.g. a serializer field, a model
   `__str__`, a signal, or a new `related_name` access. "No change to the view"
   is a red herring; the extra queries come from code the view *calls*.

3. **Profiler / query log.** I integrated **django-silk** and hit the endpoint
   for a heavy customer. Silk's request page shows the SQL query **count** and
   total DB time. The naive endpoint issues **1 + 2N** queries (1 for the
   orders, then per order: 1 for `customer`, 1 for `items`). At N=250 that is
   **~501 queries**, each a network round trip — tens of ms × hundreds = the 30s
   timeout. This confirms the hypothesis from step 1/2.

4. **Rule out the look-alikes.** Missing index? No — the query *count* is the
   problem, not a single slow scan. Serializer overhead? The Python work is
   trivial; DB round trips dominate. Cache invalidation? There is no cache in
   the path. So the category is unambiguous.

### Root cause

**N+1 query**, caused by the serializer touching `order.customer` and
`order.items` for every row while the queryset only selected `Order` rows. See
`orders/views.py::summary_naive` and `orders/serializers.py`.

### The fix and *why* it works (DB + ORM level)

`orders/views.py::order_summary`:

```python
Order.objects.select_related("customer").prefetch_related(
    Prefetch("items", queryset=OrderItem.objects.only(...))
)
```

- **`select_related("customer")`** — `customer` is a **forward
  ForeignKey (to-one)**. Django resolves it with a **SQL JOIN** in the *initial*
  query, so `order.customer.name` is already in memory — **zero** extra round
  trips.
- **`prefetch_related("items")`** — `items` is a **reverse FK (to-many)**; a JOIN
  would multiply rows, so Django instead runs **one** second query,
  `... WHERE order_id IN (…)`, and stitches the children onto their parents in
  Python. That collapses N per-order queries into **one**.
- Net: **2 queries regardless of order count** (constant), versus **1 + 2N**
  (linear). `.only(...)` further trims columns fetched for items.
- The serializer helpers use `len(order.items.all())` and iterate
  `order.items.all()` — both read the **prefetched cache** rather than
  re-hitting the DB (calling `.count()` would re-issue SQL and reintroduce an
  N+1).

### Before/after evidence (django-silk)

Reproduce locally (see README) and compare at `/silk/`:

| Endpoint | Queries (250-order customer) |
| --- | --- |
| `/api/orders/summary/naive/` | ~501 (1 + 2N) |
| `/api/orders/summary/` | 2 |

The query-count assertions are also enforced automatically in
`orders/tests.py` (`test_naive_endpoint_has_n_plus_one`,
`test_fixed_endpoint_is_constant`).

---

## Section 2 — SIGKILL: what happens to in-flight tasks?

**Question:** what happens to in-flight tasks if the Celery worker is
`SIGKILL`'d, and how does the implementation handle it?

`SIGKILL` (unlike `SIGTERM`) cannot be trapped — the worker dies instantly with
no cleanup and no chance to ack or requeue. The outcome depends entirely on
**when** the message was acknowledged to the broker:

- **Default Celery (`acks_late=False`)**: a task is acked the moment it is
  *received*, before it runs. A worker killed mid-run has **already acked**, so
  the broker considers the job done — **the job is silently lost**. This is the
  failure mode the task warns against.

- **This implementation (`CELERY_TASK_ACKS_LATE = True`)**: the message is acked
  only **after** `send_email_task` returns. A `SIGKILL` mid-run means the message
  was **never acked**, so when the worker's broker connection drops, Redis's
  visibility-timeout mechanism makes the message **visible again** and it is
  **redelivered** to another worker. Combined with
  **`CELERY_TASK_REJECT_ON_WORKER_LOST = True`** (a lost worker's task is
  re-queued rather than marked failed) and
  **`CELERY_WORKER_PREFETCH_MULTIPLIER = 1`** (a worker holds at most one
  un-acked message, so a crash can strand at most one job), no job is lost.

The cost of `acks_late` is **at-least-once** delivery: a worker killed *after*
`send_email` succeeded but *before* the ack will have the job redelivered.
That is handled by **idempotency** — `process_email_job` returns early when
`job.status == SENT`, so the email is never sent twice. State lives in the
`EmailJob` DB row (the source of truth), not only in the broker, which is what
lets us prove "no job is lost" after a crash.

---

## Section 3 — Thread-local tenant scoping under async Django

**Question:** failure modes of thread-local tenant scoping in async views, and
what to change for async safety.

`tenants/context.py` stores the current tenant in `threading.local()`. Under the
classic **WSGI, one-request-per-thread** model this is safe: the middleware
binds the tenant at the start of the request and clears it in a `finally` block,
so the value is confined to that request's thread.

**Why it breaks under async:**

- An **`async def` view runs on the event loop**, and `await` points let the
  loop **interleave many requests on the same thread**. A `threading.local`
  value set for request A is still bound when the loop switches to request B on
  that same thread — so **request B reads request A's tenant**. That is a
  cross-tenant data leak: exactly the failure this system exists to prevent.
- Django also runs sync code from async (and vice-versa) via **`sync_to_async` /
  `asgiref.sync`**, whose **thread pool reuses threads**. A tenant left in a
  pooled thread's local can bleed into a later, unrelated task on that thread.
- `await`-ing across a boundary can resume the coroutine on a **different**
  pool thread, so the value set before the `await` isn't even visible after it.

**Fix: `contextvars.ContextVar`.** Replace `threading.local` with a
`ContextVar`. Unlike thread-locals, a `ContextVar` is bound to the **logical
context** (the coroutine/`Task`), not the OS thread. `asyncio` copies the
context per `Task`, so concurrent coroutines each see their own value even on the
same thread, and `asgiref` propagates the context correctly across the
sync/async boundary. Concretely: `tenant_var = ContextVar("tenant")`, set with
`tenant_var.set(t)` in an async-aware middleware, read via `tenant_var.get(None)`
in the manager, and **reset with the token** (`tenant_var.reset(token)`) in a
`finally` block so nothing leaks into the next task on the loop. `ContextVar`
also works fine under sync/WSGI, so it is a safe drop-in for both models.

---

## Section 4 — Written Architecture Review

*Answering **Question A** and **Question B**.*

### A. Django Admin slow with 500,000+ rows (PK already indexed)

Three root causes beyond the PK index:

1. **N+1 queries from `list_display` FK columns.** Rendering a related field
   (e.g. `order.customer`) on every changelist row issues one query per row.
   **Fix:** set **`list_select_related = ["customer"]`** on the `ModelAdmin` to
   JOIN them in one query; for reverse/many relations use a queryset override
   with `prefetch_related` via **`ModelAdmin.get_queryset`**.

2. **`SELECT COUNT(*)` for the paginator.** The changelist runs a full-table
   count to render "1 of N" and the page range; on 500k+ rows that count scans
   the table on every load. **Fix:** swap in a cheap paginator via
   **`ModelAdmin.paginator`** (an estimated-count paginator using
   `reltuples`/`SHOW TABLE STATUS`), and/or set
   **`show_full_result_count = False`** so Django stops issuing the expensive
   full count.

3. **Unindexed filters/search/ordering.** **`list_filter`**,
   **`search_fields`**, and **`ordering`** generate `WHERE`/`ILIKE`/`ORDER BY`
   on columns that are often unindexed, forcing sequential scans and filesorts.
   **Fix:** add **`Meta.indexes`** (or `db_index=True`) on exactly those
   columns; replace substring `search_fields` (which produce non-sargable
   `%term%`) with prefix search or a **`GinIndex` + `SearchVector`** for
   Postgres full-text, and prefer **`list_filter`** entries backed by indexed
   columns (e.g. `DateFieldListFilter` on an indexed date).

Trade-off: an estimated paginator sacrifices an exact row count for speed, and
extra indexes add write overhead — acceptable for an admin-read-heavy table.

### B. Offset vs cursor pagination for a 10,000-record endpoint

**Offset (`LIMIT n OFFSET m`)** is simple and allows random page jumps, but at
scale the database must **scan and discard all `m` preceding rows** for each
page — `OFFSET 9900` reads 9,900 rows to return 100, so deep pages get linearly
slower (Django's `PageNumberPagination`). Worse for **mobile infinite scroll**:
if rows are **inserted/deleted during paging**, offsets shift, so users see
**duplicated or skipped items** as the window slides over mutating data.

**Cursor (`WHERE (sort_key) > last_seen ORDER BY sort_key LIMIT n`)** encodes the
position as the last row's sort value. The DB **seeks via the index** to that key
and reads only the next `n` rows — **constant time regardless of depth** — and it
is **stable under mutation**: inserts/deletes before the cursor don't shift what
comes after it, so infinite scroll never duplicates or skips. Django provides
**`CursorPagination`**. Costs: it requires a **stable, unique, indexed ordering**
(typically `-created_at, id` to break ties), it **cannot jump to an arbitrary
page** (only next/previous), and total counts/`OFFSET`-style page numbers aren't
available.

**When to choose which:** use **cursor** for feeds and mobile infinite scroll —
high-volume, append-heavy, forward-only, where deep-page performance and
mutation-stability matter most. Use **offset** for small/bounded admin-style
tables where users need page numbers and random access and the dataset is small
enough that scanning discarded rows is cheap. For 10,000 rows behind an infinite
scroll, **cursor** is the right default.
