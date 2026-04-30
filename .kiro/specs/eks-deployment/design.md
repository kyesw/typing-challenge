# Design Document: EKS Deployment Infrastructure

## Overview

This design covers the complete deployment infrastructure for the Typing Game application on AWS. The backend (FastAPI) runs on Amazon EKS with Kubernetes manifests managed by Kustomize (base + overlays for dev/staging/prod). The frontend (React/Vite/TypeScript) is built to static assets, uploaded to S3, and served through CloudFront. CloudFront acts as the single entry point for users, routing API traffic to the EKS-hosted backend via an internet-facing ALB and serving static assets from S3.

Key design decisions:
- **Kustomize over Helm**: The project has straightforward manifests with per-environment patches. Kustomize's base-plus-overlay model keeps things declarative without introducing a templating engine.
- **Multi-stage Dockerfile**: Keeps the final image small by separating the dependency install stage from the runtime stage.
- **GitHub Actions CI/CD**: Single workflow file with test gates before deploy stages. Backend and frontend test stages run in parallel; deploy stages run after both pass.
- **CloudFront dual-origin**: S3 for static assets (default behavior) and ALB for API paths. This avoids CORS entirely — the browser sees one origin.

## Architecture

```mermaid
graph TB
    subgraph "User Browser"
        Browser[Browser]
    end

    subgraph "AWS CloudFront"
        CF[CloudFront Distribution]
    end

    subgraph "S3"
        S3[S3 Asset Bucket<br/>frontend/dist/]
    end

    subgraph "AWS EKS Cluster"
        ALB[Application Load Balancer<br/>internet-facing]
        subgraph "typing-game namespace"
            SVC[ClusterIP Service<br/>port 8000]
            POD1[Backend Pod 1]
            POD2[Backend Pod 2]
            POD3[Backend Pod N]
            CM[ConfigMap<br/>TYPING_GAME_* vars]
            SEC[Secret<br/>DATABASE_URL]
        end
    end

    subgraph "AWS RDS"
        RDS[(PostgreSQL)]
    end

    subgraph "AWS ECR"
        ECR[Container Registry]
    end

    subgraph "GitHub Actions"
        CI[CI/CD Pipeline<br/>deploy.yml]
    end

    Browser -->|HTTPS| CF
    CF -->|Default: /*| S3
    CF -->|/players/*, /games/*,<br/>/leaderboard/*, /health| ALB
    ALB --> SVC
    SVC --> POD1
    SVC --> POD2
    SVC --> POD3
    POD1 --> RDS
    POD2 --> RDS
    POD3 --> RDS
    CM -.->|env vars| POD1
    SEC -.->|env vars| POD1
    CI -->|docker push| ECR
    CI -->|kubectl apply -k| ALB
    CI -->|aws s3 sync| S3
    CI -->|invalidate| CF
    ECR -.->|pull image| POD1
```

### Directory Layout

```
project-root/
├── backend/
│   ├── Dockerfile                    # Multi-stage build
│   ├── app/                          # FastAPI application
│   └── pyproject.toml
├── frontend/
│   ├── package.json
│   └── dist/                         # Build output (gitignored)
├── k8s/
│   ├── base/
│   │   ├── kustomization.yaml
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   ├── configmap.yaml
│   │   ├── secret.yaml
│   │   └── ingress.yaml
│   └── overlays/
│       ├── dev/
│       │   ├── kustomization.yaml
│       │   └── patches/
│       │       ├── deployment-patch.yaml
│       │       └── configmap-patch.yaml
│       ├── staging/
│       │   ├── kustomization.yaml
│       │   └── patches/
│       │       ├── deployment-patch.yaml
│       │       └── configmap-patch.yaml
│       └── prod/
│           ├── kustomization.yaml
│           └── patches/
│               ├── deployment-patch.yaml
│               └── configmap-patch.yaml
├── scripts/
│   └── deploy-frontend.sh            # S3 sync + CloudFront invalidation
└── .github/
    └── workflows/
        └── deploy.yml                # CI/CD pipeline
```

## Components and Interfaces

### 1. Backend Dockerfile (`backend/Dockerfile`)

Multi-stage build targeting Python 3.11-slim:

**Stage 1 — Builder:**
- Base image: `python:3.11-slim`
- Installs build dependencies for `psycopg2-binary` (if needed by the slim image)
- Copies `pyproject.toml` and installs production dependencies via `pip install --no-cache-dir .`
- Key dependency: `psycopg2-binary` added to `pyproject.toml` production dependencies

**Stage 2 — Runtime:**
- Base image: `python:3.11-slim`
- Creates non-root user `appuser` (UID 1000)
- Copies installed packages from builder stage
- Copies `app/` source code
- Exposes port 8000
- Entrypoint: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Runs as `appuser`

The container reads all configuration from `TYPING_GAME_`-prefixed environment variables at runtime — no config is baked into the image.

### 2. Kustomize Base Manifests (`k8s/base/`)

**kustomization.yaml:**
- Lists all resources: deployment, service, configmap, secret, ingress
- Sets `commonLabels: { app: typing-game-backend }`
- Sets `namespace: typing-game`

**deployment.yaml:**
- Deployment named `typing-game-backend`
- 2 replicas (base default, overridden per overlay)
- `RollingUpdate` strategy with `maxSurge: 1`, `maxUnavailable: 0`
- Container spec:
  - Image: `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/typing-game-backend:latest` (placeholder, overridden by overlays)
  - Port: 8000
  - `envFrom`: references ConfigMap and Secret
  - Liveness probe: HTTP GET `/health` port 8000, `initialDelaySeconds: 10`, `periodSeconds: 15`, `failureThreshold: 3`
  - Readiness probe: HTTP GET `/health` port 8000, `initialDelaySeconds: 5`, `periodSeconds: 10`, `failureThreshold: 3`
  - Resource requests: `cpu: 100m`, `memory: 128Mi`
  - Resource limits: `cpu: 500m`, `memory: 512Mi`

**service.yaml:**
- Service named `typing-game-backend`
- Type: `ClusterIP`
- Port 80 → targetPort 8000
- Selector: `app: typing-game-backend`

**configmap.yaml:**
- ConfigMap named `typing-game-config`
- Data entries:
  - `TYPING_GAME_ENVIRONMENT: development`
  - `TYPING_GAME_SESSION_TTL_SECONDS: "1800"`
  - `TYPING_GAME_MAX_GAME_DURATION_SECONDS: "120"`
  - `TYPING_GAME_PROMPT_SELECTION_POLICY: random`

**secret.yaml:**
- Secret named `typing-game-secret`, type `Opaque`
- Data entries (base64-encoded placeholders):
  - `TYPING_GAME_DATABASE_URL` — PostgreSQL connection string

**ingress.yaml:**
- Ingress named `typing-game-ingress`
- Annotations:
  - `kubernetes.io/ingress.class: alb`
  - `alb.ingress.kubernetes.io/scheme: internet-facing`
  - `alb.ingress.kubernetes.io/target-type: ip`
  - `alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'`
  - `alb.ingress.kubernetes.io/healthcheck-path: /health`
- Rules: paths `/players`, `/games`, `/leaderboard`, `/health` → service `typing-game-backend` port 80

### 3. Kustomize Overlays (`k8s/overlays/{env}/`)

Each overlay's `kustomization.yaml` references `../../base` and applies strategic merge patches.

| Setting | dev | staging | prod |
|---|---|---|---|
| Replicas | 1 | 2 | 3 |
| CPU request | 50m | 100m | 250m |
| CPU limit | 250m | 500m | 1000m |
| Memory request | 64Mi | 128Mi | 256Mi |
| Memory limit | 256Mi | 512Mi | 1Gi |
| TYPING_GAME_ENVIRONMENT | development | staging | production |
| Image tag | `dev-latest` | `staging-latest` | `<git-sha>` |

Each overlay uses the Kustomize `images` transformer to set the ECR image tag without patching the deployment YAML directly.

Each overlay includes a `secretGenerator` or patch to override `TYPING_GAME_DATABASE_URL` with the environment-specific RDS connection string.

### 4. Health Checks

The existing `/health` endpoint returns `{"status": "ok", "environment": "..."}` with HTTP 200. The backend's lifespan hook already catches persistence failures and logs them, keeping `/health` alive even if the DB is temporarily unreachable.

However, for the readiness probe to correctly signal "not ready" when the DB is down (Requirement 5.5), the `/health` endpoint should be enhanced to check DB connectivity and return 503 if the connection fails. This is a minor backend change:

```python
@app.get("/health", tags=["meta"])
async def health() -> JSONResponse:
    db_ok = app.state.session_factory is not None
    if db_ok:
        try:
            # lightweight connectivity check
            with app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
    if not db_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "environment": settings.environment}
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "environment": settings.environment}
    )
```

**Design rationale:** The liveness probe and readiness probe both target `/health`. If the DB is unreachable, the readiness probe fails (503) and Kubernetes stops routing traffic to the pod. The liveness probe also hits `/health`, but with `failureThreshold: 3` and `periodSeconds: 15`, a transient DB blip won't trigger a pod restart — only sustained failure (45+ seconds) will.

### 5. Frontend S3 Deployment (`scripts/deploy-frontend.sh`)

A shell script that:
1. Runs `npm run build` in `frontend/`
2. Exits immediately on build failure (set -e)
3. Runs `aws s3 sync frontend/dist/ s3://$S3_BUCKET_NAME/ --delete`
4. Runs `aws cloudfront create-invalidation --distribution-id $CLOUDFRONT_DISTRIBUTION_ID --paths "/*"`

The script expects `S3_BUCKET_NAME` and `CLOUDFRONT_DISTRIBUTION_ID` as environment variables. In CI, these come from GitHub Actions secrets.

### 6. CloudFront Distribution

**Origins:**

| Origin ID | Domain | Type | Protocol |
|---|---|---|---|
| `s3-frontend` | `<bucket>.s3.<region>.amazonaws.com` | S3 (OAC) | HTTPS |
| `alb-backend` | `<alb-dns-name>` | Custom (ALB) | HTTPS |

**Cache Behaviors:**

| Path Pattern | Origin | Cache Policy | Notes |
|---|---|---|---|
| `/players/*` | alb-backend | CachingDisabled (TTL=0) | Forward all headers, query strings |
| `/games/*` | alb-backend | CachingDisabled (TTL=0) | Forward all headers, query strings |
| `/leaderboard/*` | alb-backend | CachingDisabled (TTL=0) | Forward all headers, query strings |
| `/health` | alb-backend | CachingDisabled (TTL=0) | Forward all headers |
| `*` (default) | s3-frontend | CachingOptimized | Compress gzip+Brotli |

**SPA Fallback:** Custom error response: when S3 returns 403 or 404, CloudFront returns `/index.html` with status 200. This supports React Router client-side routing.

**HTTPS:** Viewer protocol policy set to `redirect-to-https` on all behaviors.

**Compression:** Enabled for the default (S3) behavior — gzip and Brotli.

### 7. ECR Container Registry

- Repository name: `typing-game-backend`
- Same AWS region as the EKS cluster
- **Lifecycle policy:**
  - Keep the 20 most recent tagged images
  - Remove untagged images older than 7 days
- **Image scanning:** Enabled on push (`scanOnPush: true`)
- **Tagging strategy:** Each image is tagged with:
  - Git commit SHA (e.g., `abc1234`) — immutable reference for deployments
  - Semantic version (e.g., `v1.2.3`) — human-readable release marker
  - The CI pipeline always uses the commit SHA for Kustomize image overrides

### 8. GitHub Actions CI/CD Pipeline (`.github/workflows/deploy.yml`)

```mermaid
graph LR
    subgraph "Test Stage (parallel)"
        BT[Backend Tests<br/>pytest]
        FT[Frontend Tests<br/>vitest --run]
    end

    subgraph "Deploy Stage (parallel, after tests)"
        BD[Backend Deploy<br/>docker build → ECR push<br/>kubectl apply -k]
        FD[Frontend Deploy<br/>npm build → S3 sync<br/>CF invalidation]
    end

    BT --> BD
    BT --> FD
    FT --> BD
    FT --> FD
```

**Trigger:** `push` to `main` branch

**Jobs:**

1. **backend-test** — Python 3.11, `pip install .[dev]`, `pytest`
2. **frontend-test** — Node.js 20, `npm ci`, `npm run test -- --run`
3. **backend-deploy** (needs: backend-test, frontend-test)
   - Configure AWS credentials via `aws-actions/configure-aws-credentials`
   - Login to ECR via `aws-actions/amazon-ecr-login`
   - `docker build -t $ECR_URI:$GITHUB_SHA -f backend/Dockerfile backend/`
   - `docker push $ECR_URI:$GITHUB_SHA`
   - Update kubeconfig: `aws eks update-kubeconfig --name <cluster-name>`
   - Set image via kustomize: `cd k8s/overlays/prod && kustomize edit set image typing-game-backend=$ECR_URI:$GITHUB_SHA`
   - `kubectl apply -k k8s/overlays/prod/`
4. **frontend-deploy** (needs: backend-test, frontend-test)
   - Configure AWS credentials
   - `cd frontend && npm ci && npm run build`
   - `aws s3 sync frontend/dist/ s3://$S3_BUCKET_NAME/ --delete`
   - `aws cloudfront create-invalidation --distribution-id $CF_DIST_ID --paths "/*"`

**Pipeline Secrets (GitHub repository secrets):**
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `ECR_REPOSITORY_URI`
- `TYPING_GAME_DATABASE_URL`
- `CLOUDFRONT_DISTRIBUTION_ID`
- `S3_BUCKET_NAME`
- `EKS_CLUSTER_NAME`

## Data Models

This feature does not introduce new application data models. The existing SQLAlchemy models remain unchanged.

The deployment infrastructure introduces the following configuration data structures:

### Kubernetes ConfigMap Data

```yaml
TYPING_GAME_ENVIRONMENT: "development"          # string
TYPING_GAME_SESSION_TTL_SECONDS: "1800"          # string (parsed as int by pydantic-settings)
TYPING_GAME_MAX_GAME_DURATION_SECONDS: "120"     # string (parsed as int by pydantic-settings)
TYPING_GAME_PROMPT_SELECTION_POLICY: "random"    # string (enum value)
```

### Kubernetes Secret Data

```yaml
TYPING_GAME_DATABASE_URL: <base64-encoded>       # postgresql://user:pass@host:5432/dbname
```

### ECR Image Reference Format

```
<account-id>.dkr.ecr.<region>.amazonaws.com/typing-game-backend:<tag>
```

Where `<tag>` is the git commit SHA for production deployments.

## Error Handling

### Build Failures
- **Dockerfile build failure:** The CI pipeline aborts the backend-deploy job. GitHub Actions reports the failure. No image is pushed to ECR, no manifests are applied.
- **Frontend build failure:** `npm run build` exits non-zero. The `deploy-frontend.sh` script exits immediately (`set -e`). S3 bucket is not modified. GitHub Actions reports the failure.

### Deployment Failures
- **`kubectl apply -k` failure:** The CI pipeline reports the failure. Kubernetes does not modify existing resources if the apply fails validation. Existing pods continue running.
- **S3 sync failure:** The CI pipeline reports the failure. CloudFront invalidation is not triggered. Previous assets remain cached.
- **ECR push failure:** The CI pipeline aborts. No image tag update occurs, so Kustomize still references the previous image.

### Runtime Failures
- **RDS connectivity loss:** The `/health` endpoint returns 503. The readiness probe marks the pod as not ready. Kubernetes stops routing traffic to the pod. The liveness probe (with `failureThreshold: 3`, `periodSeconds: 15`) restarts the pod after ~45 seconds of sustained failure.
- **Pod crash:** Kubernetes automatically restarts the pod per the deployment's restart policy. The `RollingUpdate` strategy with `maxUnavailable: 0` ensures at least the current replica count minus zero pods are available during updates.
- **Image pull failure:** The pod enters `ImagePullBackOff` state. Kubernetes retries with exponential backoff. The readiness probe prevents traffic routing to the failing pod.

### Configuration Errors
- **Missing environment variable:** `pydantic-settings` raises a validation error at startup. The pod crashes and Kubernetes restarts it. The readiness probe prevents traffic routing.
- **Invalid Secret value:** Same behavior — the application fails to start, pod restarts, readiness probe gates traffic.

## Testing Strategy

### Why Property-Based Testing Does Not Apply

This feature consists entirely of:
- **Infrastructure as Code** — Kubernetes manifests (YAML), Kustomize overlays, Dockerfile
- **CI/CD pipeline definitions** — GitHub Actions workflow (YAML)
- **Deployment scripts** — Shell scripts for S3 sync and CloudFront invalidation
- **Cloud resource configuration** — ECR lifecycle policies, CloudFront behaviors

These are declarative configurations and infrastructure artifacts, not functions with inputs and outputs. There are no universal properties that hold across a range of generated inputs. Property-based testing is not appropriate here.

### Recommended Testing Approach

**1. Kustomize Manifest Validation (CI-integrated)**
- Run `kubectl kustomize k8s/overlays/{env}/` for each environment and verify the output renders without errors
- Run `kubectl apply --dry-run=client -f -` on the rendered output to catch schema violations
- Validate that common labels (`app: typing-game-backend`) appear on all resources
- Validate that the namespace (`typing-game`) is set on all resources

**2. Dockerfile Build Verification**
- Build the Docker image in CI and verify it starts successfully
- Run a smoke test: start the container, hit `/health`, confirm 200 response
- Verify the container runs as non-root (check `whoami` output)

**3. Kustomize Overlay Correctness (example-based)**
- For each overlay, render the manifests and assert:
  - Correct replica count (dev=1, staging=2, prod=3)
  - Correct `TYPING_GAME_ENVIRONMENT` value
  - Image tag is set (not the base placeholder)
  - Resource limits match the expected values for the environment

**4. CI/CD Pipeline Validation**
- Use `act` (local GitHub Actions runner) or a dry-run to validate workflow syntax
- Verify that test stages gate deploy stages (dependency chain)
- Verify that secrets are referenced correctly (no hardcoded values)

**5. Integration Testing**
- Deploy to a dev environment and verify end-to-end:
  - Backend pods are running and healthy
  - ALB routes traffic to the backend
  - CloudFront serves frontend assets
  - CloudFront routes API paths to the ALB
  - SPA fallback returns `index.html` for unknown paths
  - HTTPS redirect works
