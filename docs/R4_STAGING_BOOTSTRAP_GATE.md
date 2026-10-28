# PhotoDoc AI — R4.3 Safe Staging Bootstrap and Deployment Gate Protocol

**Document Version:** 1.0.0 (Release R4.3)  
**Target Environment:** Staging VPS (1 vCPU, 2 GB RAM, 25–30 GB NVMe, Ubuntu 24.04 LTS)  
**Base Branch:** `infra/r4-prebuilt-staging-images` (PR #13 HEAD `7c60fe08cadefd8943cfaae269f114814ea3979a`)  
**Deployment Tooling:** `scripts/staging_deploy_preflight.py`, `scripts/deploy_staging_prebuilt.sh`  

---

## 1. Architectural Scope & Target VPS Specification

### 1.1 Target VPS Specifications
PhotoDoc AI staging is designed to run reliably on an economical virtual private server:
- **Operating System:** Ubuntu 24.04 LTS (x86_64)
- **Compute:** 1 vCPU
- **Memory:** 2 GB RAM (with swap enabled)
- **Storage:** 25–30 GB NVMe SSD
- **Networking:** Static public IPv4, firewall blocking all inbound ports except SSH (22) and later reverse proxy (80/443).
- **Application Port:** Staging binds frontend strictly to loopback `127.0.0.1:8081` until an authorized public reverse proxy is configured.

### 1.2 Zero-Build Invariant
No application code, TypeScript SPA bundle, or Python packages are compiled on the VPS:
- Multi-stage Docker builds run exclusively on GitHub Actions CI.
- The VPS pulls immutable, digest-pinned container images from GHCR (`ghcr.io/life2boat/photodoc-backend`, `ghcr.io/life2boat/photodoc-frontend`).
- `docker-compose.staging.prebuilt.yml` contains **zero `build:` directives**.
- Peak memory usage during deployment is limited to container layer extraction (~100–150 MiB RSS), preventing kernel OOM killer events and swap thrashing.

---

## 2. Production-Target Safety Guard

### 2.1 Forbidden Production Targets
Under no circumstances may the staging deployment script or preflight tools target or run on the known production server:
- **Protected Production IP:** `79.137.196.14`
- **Protected Production Domains:** `photodoc-ai.ru`, `www.photodoc-ai.ru`
- **Protected Production Volumes:** `photodoc_backend-data`, `photodoc_uploads`
- **Protected Production Environment:** `backend/.env` (without `.staging.example` or synthetic testing)
- **Protected Remote Root:** `PhotoDoc_Orders` on Yandex Disk

### 2.2 Dual-Layer Fail-Closed Guards
1. **CLI Preflight Tool (`scripts/staging_deploy_preflight.py`):**
   - Evaluates `--target-host` and environment variable `PHOTODOC_TARGET_HOST`.
   - Inspects local socket addresses (`socket.gethostbyname_ex(socket.gethostname())`) without outbound probing.
   - Halts immediately with `PRODUCTION_TARGET_GUARD: BLOCKED` if any IP matches `79.137.196.14`.
2. **Bootstrap Shell Script (`scripts/deploy_staging_prebuilt.sh`):**
   - Evaluates `TARGET_HOST` and queries local machine IPs (`hostname -I`).
   - Aborts immediately with `exit 1` before touching Docker or pulling images if `79.137.196.14` is detected.
   - Staging Compose rejects production volume definitions and production `.env` mounts.

---

## 3. GHCR Access Requirements & Owner Protocol

### 3.1 Verifying Public vs. Private Package Pullability
Operators can test whether packages are publicly pullable without modifying configuration:
```bash
# Test anonymous pull for backend
docker pull ghcr.io/life2boat/photodoc-backend:7c60fe08cadefd8943cfaae269f114814ea3979a

# Test anonymous pull for frontend
docker pull ghcr.io/life2boat/photodoc-frontend:7c60fe08cadefd8943cfaae269f114814ea3979a
```
- **If Public:** Pull succeeds and downloads layers anonymously without `docker login`.
- **If Private:** Output reports `Error response from daemon: denied: denied` or `unauthorized`.

### 3.2 Minimal Read-Only Authentication (For Private Visibility)
If the repository owner maintains private packages:
1. The repository owner creates a fine-grained or classic Personal Access Token (PAT):
   - **Scopes:** Select **ONLY** `read:packages`.
   - Do **NOT** grant `write:packages`, `delete:packages`, or repository admin access.
2. Store the token securely on the staging VPS:
   ```bash
   echo "ghp_xxxxxxxxxxxx" | docker login ghcr.io -u life2boat --password-stdin
   ```
   Or export `GHCR_PAT="ghp_xxxxxxxxxxxx"` in the deployment environment.

### 3.3 Verifying Image Digests Against Release Manifest
Every trusted CI push emits an immutable `release-manifest.json` uploaded as a build artifact.
Operators and the repository owner verify that local or pulled images match the manifest:
```bash
# Verify pulled backend image digest
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/life2boat/photodoc-backend:7c60fe08cadefd8943cfaae269f114814ea3979a
# Expected: ghcr.io/life2boat/photodoc-backend@sha256:bc40fbe28d65fdb386605fdcc11a89511051efdd77d88381a8b0a0767f2893d9

# Verify pulled frontend image digest
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/life2boat/photodoc-frontend:7c60fe08cadefd8943cfaae269f114814ea3979a
# Expected: ghcr.io/life2boat/photodoc-frontend@sha256:1fc9b0d3b783f04ae1a380f4b84da129e63eb5efc8bdefb95af6e7074157379c
```

### 3.4 Summary of Actions Requiring Repository Owner Permissions
The following actions cannot be automated and require owner authorization in GitHub settings:
1. **Changing Package Visibility:** Navigating to `github.com/life2boat?tab=packages` -> Package Settings -> Danger Zone -> Change visibility (Public / Private).
2. **Generating VPS Access Tokens:** Creating fine-grained PATs with `read:packages`.
3. **Repository Environment Secrets:** Adding dedicated staging tokens (`YANDEX_DISK_STAGING_TOKEN`) for staging acceptance testing.
4. **Authorizing DNS Cutover:** Pointing staging subdomains or production domains to server IPs.

---

## 4. Operator Runbook: Idempotent Staging Bootstrap

When authorized to bootstrap or update the staging environment on a clean VPS:

### Step 1: Clone Repository & Select Verified Release Commit
```bash
git clone https://github.com/life2boat/photodoc-ai.git /opt/photodoc-staging
cd /opt/photodoc-staging
git checkout 7c60fe08cadefd8943cfaae269f114814ea3979a
```

### Step 2: Define Pinned Image References
```bash
export PHOTODOC_BACKEND_IMAGE="ghcr.io/life2boat/photodoc-backend@sha256:bc40fbe28d65fdb386605fdcc11a89511051efdd77d88381a8b0a0767f2893d9"
export PHOTODOC_FRONTEND_IMAGE="ghcr.io/life2boat/photodoc-frontend@sha256:1fc9b0d3b783f04ae1a380f4b84da129e63eb5efc8bdefb95af6e7074157379c"
export STAGING_PORT="8081"
```

### Step 3: Run Deployment Preflight Gatekeeper (Read-Only)
```bash
python3 scripts/staging_deploy_preflight.py \
  --backend-image "${PHOTODOC_BACKEND_IMAGE}" \
  --frontend-image "${PHOTODOC_FRONTEND_IMAGE}"
```
*Expected Result:* All five markers return `PASS`. Exit code `0`.

### Step 4: Execute Safe Bootstrap
```bash
./scripts/deploy_staging_prebuilt.sh \
  --backend-image "${PHOTODOC_BACKEND_IMAGE}" \
  --frontend-image "${PHOTODOC_FRONTEND_IMAGE}" \
  --confirm
```

### Step 5: Verify Container Health & Data Preservation
```bash
# Query health check
curl -f http://127.0.0.1:8081/api/health

# Verify container resource footprint
docker stats --no-stream

# Verify persistent staging named volumes
docker volume ls | grep staging
```

---

## 5. Sub-15-Second Zero-Build Rollback Procedure

If any anomaly or regression occurs after updating staging images:
1. Locate the previous known-good image digests (recorded in previous release manifest or operator logs).
2. Execute immediate rollback without rebuilding:
   ```bash
   ./scripts/deploy_staging_prebuilt.sh \
     --rollback \
     "ghcr.io/life2boat/photodoc-backend@sha256:<previous_backend_digest>" \
     "ghcr.io/life2boat/photodoc-frontend@sha256:<previous_frontend_digest>" \
     --confirm
   ```
3. Docker Compose replaces container layers in-place.
4. SQLite volume `photodoc_staging_backend-data` and customer uploads volume `photodoc_staging_uploads` are completely preserved.
5. Service recovery time: **< 15 seconds**.

---

## 6. Safety Verification Matrix

| Test Case | Tool | Enforcement | Outcome |
|---|---|---|---|
| Target is `79.137.196.14` | Preflight & Deploy Script | Fail-closed comparison | **FAIL/BLOCKED** (No actions taken) |
| Image tag is `:latest` | Preflight | Tag parser regex | **FAIL/BLOCKED** (Must be pinned digest) |
| Missing image references | Preflight & Compose | Shell expansion `:?` | **FAIL/BLOCKED** (Syntax error) |
| Compose contains `build:` | Preflight & Deploy Script | Regex inspection | **FAIL/BLOCKED** (Zero host builds) |
| Frontend bound to `0.0.0.0` | Preflight | Port binding parser | **FAIL/BLOCKED** (Must be 127.0.0.1) |
| Staging volume wiped (`-v`) | Deploy Script | Policy invariant | **NEVER EXECUTED** (Volumes preserved) |
| Production DB/env mounted | Preflight | Asset path inspection | **FAIL/BLOCKED** (Production isolated) |
| Staging Yandex token missing | Override Preflight | Expansion `:?` | **FAIL/BLOCKED** (Worker cannot start) |
