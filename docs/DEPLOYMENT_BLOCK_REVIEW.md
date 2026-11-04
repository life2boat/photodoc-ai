# Deployment block review — Yandex.Disk deletion defect

Assessment of `/srv/photodoc/DEPLOYMENT_BLOCKED` against `main = 50191b5d321113a8c2dabfe7dd7716cdd5ec9e78`.

The marker is **not** removed by this document, and this document is not permission to
remove it. It is the evidence the owner needs to decide.

---

## 1. What the original defect was

`/srv/photodoc/DATA_LOSS_FIX_PROPOSAL.md` (read-only inspection) records the finding:

> The upload helper returns without a success result when the token or upload URL is
> absent, catches HTTP/network/file exceptions, and also returns None on success. The
> caller deletes the local file in an unconditional `finally` block.
> Consequently, a missing `YANDEX_DISK_TOKEN`, missing upload href, rejected upload,
> timeout, or other upload exception can be followed by deletion of the only retained
> local file.

The defect class: **deletion decided by the request path rather than by proof of
delivery.** Two independent faults combine — an upload function with no success/failure
contract, and a caller that treats "the call returned" as "the file is safe to delete".

## 2. Where it actually lived — a correction

The proposal describes `backend/main.py` with `upload_to_yandex_disk` at line 575 and
`background_upload_and_cleanup` at line 616. **The symbol does not appear in any commit
reachable from the refs available in this clone:**

```
git log --all -S 'background_upload_and_cleanup'    -> no results
git show 51113bc^:backend/main.py | grep unlink     -> no delete call
```

That is evidence about reachable refs, not proof about unreachable or pruned objects, so
the accurate conclusion is: the function was not part of any published revision this
clone can see, and the proposal was most likely written against a local working tree.
What the committed history *did* contain, immediately before PR #9
(`51113bc^:backend/main.py`), is the same fault class in a different shape:

```python
def upload_to_yandex_disk(local_file_path, remote_folder_path, remote_file_name):
    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token:
        return                      # silent no-op
    ...
    href = upload_url_response.json().get("href")
    if not href:
        return                      # silent no-op
    ...
    except Exception as e:
        logging.error("Ошибка загрузки на Яндекс.Диск: %s", e)   # swallowed
```

invoked fire-and-forget:

```python
background_tasks.add_task(upload_to_yandex_disk, ...)   # after the response was sent
```

So at that revision the failure mode was "upload silently does nothing and nobody
knows", which the proposal's unconditional delete would have turned into data loss. The
delete-on-failure variant was real in the inspected worktree; the silent-no-op variant
is what shipped. Both are addressed by the same fix: deletion must require proof.

## 3. Root cause

1. **No delivery contract.** The upload function had no return value distinguishing
   success from failure, so no caller could act on the outcome.
2. **Deletion was a side effect of intake**, not of a completed, verified delivery.
3. **No durable state.** Nothing recorded that a file was still owed to a customer, so a
   failed upload left no work item to retry.

## 4. Which changes fixed it

| PR | Commit | Date | What it changed |
|---|---|---|---|
| #9 | `51113bc` | 2026-10-26 | Removed the fire-and-forget upload path and introduced the durable delivery queue (`delivery_jobs`), so a failed upload became a retryable work item instead of a silent no-op. **It did not add a payment gate**: at this point `file`/`info` jobs were still enqueued for unpaid orders. |
| #21 | `a5cad62` | 2026-10-29 | Commit-failure recovery, full-state consistency and signal safety for the cleanup path. |
| #22 | `febae24` | 2026-11-02 | **Deletion-boundary revalidation**: cleanup now rechecks delivery state, file identity, pending consumers, path and actual bytes inside the final SQLite write transaction. Changed files are retained. |
| #26 | `57fede5` | 2026-11-04 | **Payment gating** — `file`/`info`/`paid_email` may only be claimed, delivered and cleaned for a **paid** order — plus the durable paid-email outbox. This is where the unpaid-delivery hole was actually closed. |

All four are ancestors of the target main (`git merge-base --is-ancestor`), and the
target main is `50191b5`, which also contains the PR #28 backup safety work.

## 5. Verification against the target main

| # | Requirement | Evidence |
|---|---|---|
| 4 | Unpaid orders cannot trigger delivery | `delivery.py:128` — `claim_due` only selects `file`/`info`/`paid_email` when `orders.payment_status='paid'`; `main.py:1091` — `deliver_job` calls `require_paid_order(job["order_id"])` (`main.py:817`) |
| 5 | Unpaid orders cannot trigger local deletion | `delivery.py:332-333` — the cleanup transaction rechecks payment and **returns `False` without unlinking** (it does not mark the job `not_applicable`; that state is used for kinds that have no local file, such as `info`/`admin_email`); `delivery.py:593-594` rechecks again at the final boundary; `delivery.py:687` filters cleanup candidates on paid |
| 6 | Failed Yandex operations preserve retryable state | The upload path now raises rather than returning: `main.py:933` (no token), `main.py:1018` (missing href), `main.py:1036` ("Yandex upload was not confirmed by resource metadata"), plus folder/lookup failures at `:829-878`. A raise marks the job `failed`, increments `attempts` and **retains the source**; nothing deletes on an unconfirmed upload |
| 7 | Cleanup cannot remove files required by unfinished orders | `delivery.py:289-297` documents and enforces nine preconditions: `delivered` state, fencing lease, path confinement, `order_id` prefix isolation, size+SHA-256 identity, **no other unfinished job referencing the same path**, durable `cleanup_phase='deleting'` intent committed before unlink, lease re-validation inside the write transaction, and idempotent crash recovery |
| 8 | Worker retries cannot bypass payment validation | The paid predicate lives in the claim query (`delivery.py:128`), so every claim re-evaluates it; the dispatcher re-asserts it (`main.py:1091`), and cleanup re-asserts it twice (`delivery.py:332`, `:593`). There is no path that delivers or deletes without re-reading `payment_status` |

### Regression tests covering the original failure

| Suite | Focus | Result |
|---|---|---|
| `test_delivery_delete_boundary.py` | source replaced/queued/revoked at the deletion boundary | pass |
| `test_delivery_lifecycle_safety.py` | real subprocess crashes around commit and cleanup | pass |
| `test_photo_cleanup_privacy.py` | zero-retention cleanup cannot delete a needed source | pass |
| `test_payment_delivery_reliability.py` | unpaid never delivered/cleaned, outbox at-most-once | pass |
| `test_payment_security.py` | payment state machine and callback integrity | pass |

```
97 passed, 1 skipped  (the five suites above)
```

Specifically, `test_unpaid_never_claims_uploads_or_deletes_even_legacy_delivered`
(parametrised over `unpaid/pending/failed/cancelled/None` × `file/info`) asserts the
original defect directly: an unpaid order receives **zero** uploads and **zero** local
deletions, including when a legacy job is already marked `delivered`.

## 6. Remaining risks

* **The deletion contract is only as good as its tests.** They are strong and include
  real subprocess crashes, but they run against SQLite on one host; multi-host
  concurrency is not represented.
* **`fsync`/power-loss durability of the delivery intent is best-effort.** The
  `cleanup_phase='deleting'` intent is committed before the unlink, but durability of
  that commit depends on the filesystem (see `docs/BACKUP_AND_RESTORE.md` §2.1).
* **Yandex.Disk remains an external dependency with no transactional guarantee.** The
  code verifies the remote object by size and SHA-256 before allowing deletion, which
  converts a silent loss into a retained source, but a Yandex-side deletion after
  verification is not detectable.
* **The staging deployment itself is still untested against this fix.** The running
  image is `febae24` (PR #22); PR #26's payment gating and the PR #28 backup tooling
  have not run on the VPS. That is a deployment-readiness question, not an evidence gap
  in the fix.
* **No off-host backup exists**, so an operational mistake during the eventual cutover
  is not recoverable off-host (see the S6-02 activation checklist).

## 7. Recommendation

**Lifting `DEPLOYMENT_BLOCKED` can be recommended**, on these conditions:

1. The marker's two stated requirements are satisfied: the deletion defect is fixed in
   the target main (PR #22 + PR #26, verified above with `file:line` and passing
   regression tests), and the owner gives explicit deployment approval — which this
   document does not constitute.
2. The block is lifted as an owner action, at the start of a deployment window, not
   pre-emptively.
3. Before the cutover, a fresh backup must pass the gate. For the first transition only,
   that means the pre-outbox path (`--allow-pre-outbox-transition` /
   `BACKUP_GATE_ALLOW_PRE_OUTBOX=1`), because the live database cannot yet contain the
   outbox table.
4. The worker stays disabled through the cutover, and the unpaid-order jobs that PR #26
   parks are expected to remain `pending` rather than being "repaired".

If the owner prefers to keep the block in place, nothing in the current system is
compromised by waiting: staging is internal-only, the worker is disabled, and no
customer traffic reaches it.
