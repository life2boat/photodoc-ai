# PhotoDoc AI — R4.2 Prebuilt Docker Images & Low-Memory VPS Deployment Runbook

**Document Version:** 1.0.0 (Release R4.2)  
**Target Environment:** Staging VPS (1 vCPU, 2 GB RAM, 25–30 GB NVMe, Debian 12 / Ubuntu 24.04 LTS)  
**Base Repository:** `life2boat/photodoc-ai`  
**Container Registry:** GitHub Container Registry (`ghcr.io/life2boat/photodoc-backend`, `ghcr.io/life2boat/photodoc-frontend`)  

---

## 1. Architectural Overview & VPS Sizing Context

### 1.1 The Low-Memory Constraint
PhotoDoc AI is designed to run reliably on an economical VPS with **1 vCPU and 2 GB RAM**. Compiling frontend web bundles (Node.js 20, Vite, Tailwind CSS, Lucide icons) and building multi-stage container images on a 2 GB server incurs peak RSS exceeding 1.2–1.8 GB. Under concurrent web traffic or database activity, local compilation frequently triggers kernel Out-Of-Memory (OOM) kills or severe swap thrashing that degrades SSH responsiveness.

### 1.2 The Prebuilt Image Architecture
To guarantee zero-build operations on the VPS:
1. **GitHub Actions CI/CD** handles 100% of image builds using Docker Buildx and multi-stage caches on push to trusted branches (`main`, `infra/r4-prebuilt-staging-images`).
2. **GHCR Registry** hosts immutable container images tagged by full Git commit SHA (`${{ github.sha }}`) and sha256 digests (`sha256:...`).
3. **Prebuilt Compose Files** (`docker-compose.staging.prebuilt.yml`, `docker-compose.staging.prebuilt.delivery.yml`) contain **NO `build:` directives**.
4. The staging VPS only **pulls** prebuilt binaries and executes them with zero compilation overhead.

---

## 2. GitHub Repository & GHCR Package Setup (Owner Action)

When GitHub Actions publishes packages to GHCR for the first time, GitHub creates the package under the repository owner (`life2boat`). The repository owner must configure access permissions as follows:

### Option A: Public Package Visibility (Recommended for Staging Simplicity)
Setting package visibility to **Public** allows the VPS to pull staging container images anonymously without managing credentials, token rotation, or secret files on the VPS:
1. In GitHub, navigate to the organization or user profile: `https://github.com/life2boat?tab=packages` (or repository packages list).
2. Select `photodoc-backend` and click **Package settings**.
3. Scroll to **Danger Zone** -> **Change package visibility** -> Select **Public** -> Type package name to confirm.
4. Repeat for `photodoc-frontend`.
5. *Result:* Docker on the VPS can run `docker compose -f docker-compose.staging.prebuilt.yml pull` without `docker login`.

### Option B: Private Packages with Read-Only Personal Access Token (PAT)
If packages must remain private:
1. The repository owner creates a fine-grained or classic Personal Access Token (PAT):
   - **GitHub Settings** -> **Developer Settings** -> **Personal Access Tokens** -> **Tokens (classic)**.
   - Note: `photodoc-vps-ghcr-reader`.
   - Expiration: 90 days (or organization policy).
   - Scopes: Select **ONLY** `read:packages` (do NOT grant `write:packages`, `delete:packages`, or full `repo` admin rights).
2. On the Staging VPS, authenticate Docker once:
   ```bash
   echo "YOUR_READ_ONLY_PAT" | docker login ghcr.io -u life2boat --password-stdin
   ```
3. Docker credentials will be stored securely in `~/.docker/config.json`.

---

## 3. Ten-Step Operator Deployment Runbook

Follow this sequence for every staging deployment. All commands are executed on the staging VPS in `/opt/photodoc-staging` or the deployment root.

### Step 1: Identify Target Commit SHA
Identify the exact commit SHA approved for release from GitHub PR #12 / PR #13 or the trusted branch:
```bash
TARGET_COMMIT="<full-40-char-commit-sha>"
SHORT_COMMIT="$(echo "${TARGET_COMMIT}" | cut -c1-7)"
echo "Deploying PhotoDoc AI revision: ${TARGET_COMMIT} (${SHORT_COMMIT})"
```

### Step 2: Verify Image Digests in GitHub Actions or Release Manifest
Inspect the GitHub Actions workflow run summary for that commit or download `release-manifest.json` from the CI run artifacts. Confirm the immutable digests:
```bash
# Example release-manifest.json content:
# {
#   "commit_sha": "8609a05d304c327457b75e35e1c5801f3930031e",
#   "images": {
#     "backend": {
#       "image": "ghcr.io/life2boat/photodoc-backend:8609a05d304c327457b75e35e1c5801f3930031e",
#       "digest": "sha256:0123456789abcdef..."
#     },
#     "frontend": {
#       "image": "ghcr.io/life2boat/photodoc-frontend:8609a05d304c327457b75e35e1c5801f3930031e",
#       "digest": "sha256:fedcba9876543210..."
#     }
#   }
# }
```

### Step 3: Authenticate Docker with GHCR (If Using Private Packages)
If using Option B (private packages):
```bash
if [ -n "${GHCR_PAT:-}" ]; then
  echo "${GHCR_PAT}" | docker login ghcr.io -u life2boat --password-stdin
fi
```
*(If packages are public, skip this step).*

### Step 4: Export Image References
Define the exact images to be deployed. Using immutable sha256 digests is strongly recommended for strict immutability, or use the 40-character commit SHA tag:
```bash
# By Immutable Digest (Recommended):
export PHOTODOC_BACKEND_IMAGE="ghcr.io/life2boat/photodoc-backend@sha256:<backend_digest>"
export PHOTODOC_FRONTEND_IMAGE="ghcr.io/life2boat/photodoc-frontend@sha256:<frontend_digest>"

# OR By Exact Full Commit SHA Tag:
export PHOTODOC_BACKEND_IMAGE="ghcr.io/life2boat/photodoc-backend:${TARGET_COMMIT}"
export PHOTODOC_FRONTEND_IMAGE="ghcr.io/life2boat/photodoc-frontend:${TARGET_COMMIT}"

export STAGING_PORT="8081"
```

### Step 5: Pull Images Explicitly Before Modifying Runtime
Pull images over the network before stopping or restarting running containers to minimize downtime:
```bash
docker compose -p photodoc-staging -f docker-compose.staging.prebuilt.yml pull
```

### Step 6: Run Database Schema Migrations Before Application Startup
Execute schema reconciliations against the staging SQLite volume using an isolated, ephemeral Python container before the new backend boots:
```bash
DB_VOLUME="photodoc_staging_backend-data"

# Check schema state
docker run --rm --user 65534:65534 \
  -v "${DB_VOLUME}:/data:ro" \
  "${PHOTODOC_BACKEND_IMAGE}" \
  python migrations/reconcile_delivery_schema.py --db /data/orders.db --check

# Apply any pending safe P1 schema additions (order_token_hash, delivery_jobs)
docker run --rm --user 0 \
  -v "${DB_VOLUME}:/data" \
  "${PHOTODOC_BACKEND_IMAGE}" \
  python migrations/reconcile_delivery_schema.py --db /data/orders.db --apply
```

### Step 7: Start Containers Without Local Compilation
Start containers using the `--no-build` flag. Because `docker-compose.staging.prebuilt.yml` has no `build:` sections, Docker Compose connects directly to the pulled images:
```bash
docker compose -p photodoc-staging \
  -f docker-compose.staging.prebuilt.yml \
  up -d --no-build --remove-orphans
```

### Step 8: Verify Container Health via `/api/health`
Verify that the Nginx frontend reverse proxy and FastAPI backend are healthy:
```bash
# Wait for healthy state
docker compose -p photodoc-staging -f docker-compose.staging.prebuilt.yml ps

# Query health check via loopback
HEALTH_OUTPUT="$(curl --fail -sS "http://127.0.0.1:${STAGING_PORT}/api/health")"
echo "Health check response: ${HEALTH_OUTPUT}"
echo "${HEALTH_OUTPUT}" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'
echo "DEPLOYMENT_HEALTH=PASS"
```

### Step 9: Measure Memory Consumption Before and After Start
Inspect memory consumption to verify the process remains well within the 2 GB RAM envelope:
```bash
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"

# Expected baseline on low-memory staging:
# photodoc-staging-backend    < 1.0%    ~70-110 MiB / 2 GiB    ~4-6%
# photodoc-staging-frontend   < 0.1%    ~10-25 MiB / 2 GiB     ~0.5-1.2%
```

### Step 10: Rollback Procedure (Sub-60-Second Recovery)
If any anomaly, health check failure, or regression is detected:
1. Identify the previous known good commit SHA or digests:
   ```bash
   PREV_BACKEND="ghcr.io/life2boat/photodoc-backend:<prev_good_sha>"
   PREV_FRONTEND="ghcr.io/life2boat/photodoc-frontend:<prev_good_sha>"
   ```
2. Pull and restart immediately without rebuild:
   ```bash
   PHOTODOC_BACKEND_IMAGE="${PREV_BACKEND}" \
   PHOTODOC_FRONTEND_IMAGE="${PREV_FRONTEND}" \
   docker compose -p photodoc-staging -f docker-compose.staging.prebuilt.yml up -d --no-build
   ```
3. Verify recovery via `/api/health`:
   ```bash
   curl --fail -sS "http://127.0.0.1:${STAGING_PORT}/api/health"
   ```
*Time to restore service: < 15 seconds (containers are swapped in-place without rebuilding).*

---

## 4. Staging Delivery Acceptance Override (Opt-In Testing)

To test end-to-end Yandex Disk delivery on staging without production secrets:

1. Obtain a dedicated staging Yandex Disk OAuth token (never use production token).
2. Launch with the prebuilt delivery override:
   ```bash
   PHOTODOC_BACKEND_IMAGE="${PHOTODOC_BACKEND_IMAGE}" \
   PHOTODOC_FRONTEND_IMAGE="${PHOTODOC_FRONTEND_IMAGE}" \
   YANDEX_DISK_STAGING_TOKEN="y0_dedicated_staging_token" \
   docker compose -p photodoc-staging \
     -f docker-compose.staging.prebuilt.yml \
     -f docker-compose.staging.prebuilt.delivery.yml \
     up -d --no-build
   ```
3. Run the staging preflight verifier:
   ```bash
   YANDEX_DISK_STAGING_TOKEN="y0_dedicated_staging_token" \
   python scripts/verify_staging_delivery_preflight.py --prebuilt --check-live-token
   ```
4. Verify remote isolation: deliveries target `PhotoDoc_Staging_R4/` on Yandex Disk, leaving `PhotoDoc_Orders` completely untouched.

---

## 5. Fail-Closed Safety Matrix

| Scenario | Behavior | Result |
|---|---|---|
| Unset `PHOTODOC_BACKEND_IMAGE` | Docker Compose halts immediately with variable error | **FAIL CLOSED** (No container starts) |
| Unset `PHOTODOC_FRONTEND_IMAGE` | Docker Compose halts immediately with variable error | **FAIL CLOSED** (No container starts) |
| Running `docker compose up --build` | Docker Compose ignores `--build` or errors (no `build:` key) | **NO HOST COMPILATION** (OOM prevented) |
| Missing `YANDEX_DISK_STAGING_TOKEN` in override | Compose halts with `Error: YANDEX_DISK_STAGING_TOKEN is required` | **FAIL CLOSED** (Worker cannot boot) |
| Target destination set to `PhotoDoc_Orders` | Preflight verifier and delivery manager reject token | **PRODUCTION ISOLATED** |
| Production volumes accessed | Staging uses `photodoc_staging_backend-data` volume | **DATA STRICTLY ISOLATED** |

---

## 6. Summary of Operator Files

| File | Purpose | Build Directive Present |
|---|---|---|
| `docker-compose.staging.prebuilt.yml` | Base staging Compose with GHCR image references | **NO** |
| `docker-compose.staging.prebuilt.delivery.yml` | Opt-in delivery test override requiring token | **NO** |
| `scripts/verify_staging_delivery_preflight.py` | Automated isolation, syntax & token verification | N/A |
| `backend/tests/test_staging_prebuilt_compose.py` | Pytest verification of prebuilt compose architecture | N/A |
| `.github/workflows/playwright.yml` | CI build, test, and GHCR publishing pipeline | N/A |
