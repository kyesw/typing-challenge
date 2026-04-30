# Implementation Plan: EKS Deployment Infrastructure

## Overview

This plan implements the deployment infrastructure for the Typing Game application on AWS. The backend runs on EKS with Kustomize-managed manifests, and the frontend is served as static assets through CloudFront backed by S3. Tasks are ordered so each step builds on the previous one: Dockerfile first, then dependency changes, health endpoint enhancement, Kustomize base manifests, overlays, frontend deploy script, and finally the CI/CD pipeline that wires everything together.

## Tasks

- [x] 1. Create the Backend Dockerfile
  - [x] 1.1 Create `backend/Dockerfile` with multi-stage build
    - Stage 1 (builder): Use `python:3.11-slim` base, copy `pyproject.toml`, install production dependencies with `pip install --no-cache-dir .`
    - Stage 2 (runtime): Use `python:3.11-slim` base, create non-root user `appuser` (UID 1000), copy installed packages from builder, copy `app/` source code
    - Set `EXPOSE 8000` and entrypoint `uvicorn app.main:app --host 0.0.0.0 --port 8000`
    - Run as `appuser`
    - Exclude dev dependencies and test files from the final image
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

- [x] 2. Add psycopg2-binary to backend production dependencies
  - [x] 2.1 Update `backend/pyproject.toml` to add `psycopg2-binary` to the `dependencies` list
    - Add `"psycopg2-binary>=2.9"` to the production dependencies array
    - This enables PostgreSQL connectivity via SQLAlchemy when running on EKS with RDS
    - _Requirements: 1.7, 5.6_

- [x] 3. Enhance the /health endpoint with DB connectivity check
  - [x] 3.1 Modify the `/health` endpoint in `backend/app/main.py`
    - Import `JSONResponse` from `fastapi.responses` and `text` from `sqlalchemy`
    - Replace the existing `/health` handler with one that checks DB connectivity
    - If `app.state.session_factory` is `None` or `SELECT 1` fails, return 503 with `{"status": "degraded", "environment": "..."}`
    - If DB check passes, return 200 with `{"status": "ok", "environment": "..."}`
    - Wrap the DB check in a try/except so a connection failure doesn't crash the endpoint
    - _Requirements: 5.5, 4.1, 4.2, 4.5_
  - [ ]* 3.2 Write unit tests for the enhanced /health endpoint
    - Test that /health returns 200 when DB is available
    - Test that /health returns 503 when session_factory is None
    - Test that /health returns 503 when DB query raises an exception
    - _Requirements: 5.5_

- [x] 4. Checkpoint — Verify backend changes
  - Ensure all existing backend tests still pass after the health endpoint change. Ask the user if questions arise.

- [x] 5. Create Kustomize base manifests
  - [x] 5.1 Create `k8s/base/deployment.yaml`
    - Deployment named `typing-game-backend` in namespace `typing-game`
    - 2 replicas with `RollingUpdate` strategy (`maxSurge: 1`, `maxUnavailable: 0`)
    - Container image placeholder: `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/typing-game-backend:latest`
    - Container port 8000
    - `envFrom` referencing ConfigMap `typing-game-config` and Secret `typing-game-secret`
    - Liveness probe: HTTP GET `/health` port 8000, `initialDelaySeconds: 10`, `periodSeconds: 15`, `failureThreshold: 3`
    - Readiness probe: HTTP GET `/health` port 8000, `initialDelaySeconds: 5`, `periodSeconds: 10`, `failureThreshold: 3`
    - Resource requests: `cpu: 100m`, `memory: 128Mi`; limits: `cpu: 500m`, `memory: 512Mi`
    - _Requirements: 2.2, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 9.3, 9.4_
  - [x] 5.2 Create `k8s/base/service.yaml`
    - Service named `typing-game-backend`, type `ClusterIP`
    - Port 80 → targetPort 8000
    - Selector: `app: typing-game-backend`
    - _Requirements: 2.3_
  - [x] 5.3 Create `k8s/base/configmap.yaml`
    - ConfigMap named `typing-game-config`
    - Data: `TYPING_GAME_ENVIRONMENT: development`, `TYPING_GAME_SESSION_TTL_SECONDS: "1800"`, `TYPING_GAME_MAX_GAME_DURATION_SECONDS: "120"`, `TYPING_GAME_PROMPT_SELECTION_POLICY: random`
    - _Requirements: 2.4, 9.1_
  - [x] 5.4 Create `k8s/base/secret.yaml`
    - Secret named `typing-game-secret`, type `Opaque`
    - Data: `TYPING_GAME_DATABASE_URL` with base64-encoded placeholder value
    - _Requirements: 2.5, 5.2, 9.2_
  - [x] 5.5 Create `k8s/base/ingress.yaml`
    - Ingress named `typing-game-ingress`
    - Annotations: `kubernetes.io/ingress.class: alb`, `alb.ingress.kubernetes.io/scheme: internet-facing`, `alb.ingress.kubernetes.io/target-type: ip`, `alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'`, `alb.ingress.kubernetes.io/healthcheck-path: /health`
    - Rules: paths `/players`, `/games`, `/leaderboard`, `/health` → service `typing-game-backend` port 80
    - _Requirements: 2.6_
  - [x] 5.6 Create `k8s/base/kustomization.yaml`
    - List resources: deployment.yaml, service.yaml, configmap.yaml, secret.yaml, ingress.yaml
    - Set `commonLabels: { app: typing-game-backend }`
    - Set `namespace: typing-game`
    - _Requirements: 2.1, 2.7, 10.4, 10.5_

- [x] 6. Create Kustomize environment overlays
  - [x] 6.1 Create dev overlay at `k8s/overlays/dev/`
    - `kustomization.yaml` referencing `../../base`, with `images` transformer setting tag to `dev-latest`
    - `patches/deployment-patch.yaml`: 1 replica, CPU request 50m, CPU limit 250m, memory request 64Mi, memory limit 256Mi
    - `patches/configmap-patch.yaml`: `TYPING_GAME_ENVIRONMENT: development`
    - Include secret override for dev RDS connection string
    - _Requirements: 3.1, 3.4, 3.5, 3.6, 5.4, 9.6, 9.7_
  - [x] 6.2 Create staging overlay at `k8s/overlays/staging/`
    - `kustomization.yaml` referencing `../../base`, with `images` transformer setting tag to `staging-latest`
    - `patches/deployment-patch.yaml`: 2 replicas, CPU request 100m, CPU limit 500m, memory request 128Mi, memory limit 512Mi
    - `patches/configmap-patch.yaml`: `TYPING_GAME_ENVIRONMENT: staging`
    - Include secret override for staging RDS connection string
    - _Requirements: 3.2, 3.4, 3.5, 3.6, 5.4, 9.6, 9.7_
  - [x] 6.3 Create prod overlay at `k8s/overlays/prod/`
    - `kustomization.yaml` referencing `../../base`, with `images` transformer (tag set by CI to git SHA)
    - `patches/deployment-patch.yaml`: 3 replicas, CPU request 250m, CPU limit 1000m, memory request 256Mi, memory limit 1Gi
    - `patches/configmap-patch.yaml`: `TYPING_GAME_ENVIRONMENT: production`
    - Include secret override for prod RDS connection string
    - _Requirements: 3.3, 3.4, 3.5, 3.6, 5.4, 9.6, 9.7_

- [x] 7. Checkpoint — Validate Kustomize manifests
  - Run `kubectl kustomize k8s/overlays/dev/`, `kubectl kustomize k8s/overlays/staging/`, and `kubectl kustomize k8s/overlays/prod/` to verify all overlays render without errors. Ensure all tests pass. Ask the user if questions arise.
  - _Requirements: 10.2, 10.3_

- [x] 8. Create frontend deployment script
  - [x] 8.1 Create `scripts/deploy-frontend.sh`
    - Add `#!/usr/bin/env bash` and `set -euo pipefail`
    - Validate that `S3_BUCKET_NAME` and `CLOUDFRONT_DISTRIBUTION_ID` environment variables are set
    - Run `npm run build` in `frontend/` directory (abort on failure)
    - Run `aws s3 sync frontend/dist/ s3://$S3_BUCKET_NAME/ --delete`
    - Run `aws cloudfront create-invalidation --distribution-id $CLOUDFRONT_DISTRIBUTION_ID --paths "/*"`
    - Make the script executable
    - _Requirements: 6.1, 6.2, 6.3, 6.6_

- [x] 9. Create GitHub Actions CI/CD pipeline
  - [x] 9.1 Create `.github/workflows/deploy.yml`
    - Trigger on `push` to `main` branch
    - Define four jobs: `backend-test`, `frontend-test`, `backend-deploy`, `frontend-deploy`
    - `backend-test`: Python 3.11, `pip install .[dev]`, `pytest`
    - `frontend-test`: Node.js 20, `npm ci`, `npm run test -- --run`
    - `backend-deploy` (needs: backend-test, frontend-test): configure AWS credentials via `aws-actions/configure-aws-credentials`, login to ECR via `aws-actions/amazon-ecr-login`, `docker build` and `docker push` with `$GITHUB_SHA` tag, update kubeconfig, `kustomize edit set image`, `kubectl apply -k k8s/overlays/prod/`
    - `frontend-deploy` (needs: backend-test, frontend-test): configure AWS credentials, `npm ci && npm run build`, `aws s3 sync --delete`, `aws cloudfront create-invalidation`
    - Reference secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `ECR_REPOSITORY_URI`, `TYPING_GAME_DATABASE_URL`, `CLOUDFRONT_DISTRIBUTION_ID`, `S3_BUCKET_NAME`, `EKS_CLUSTER_NAME`
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10, 11.11, 11.12, 11.13, 11.14, 11.15, 11.16, 11.17_

- [x] 10. Final checkpoint — Full validation
  - Ensure all backend tests pass. Verify Kustomize overlays render cleanly. Verify the GitHub Actions workflow YAML is valid. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- This feature has no property-based tests — it consists of infrastructure-as-code, YAML manifests, and deployment scripts where PBT is not applicable
- The CloudFront distribution and ECR repository (Requirements 7, 8) are AWS infrastructure resources expected to be provisioned separately (e.g., via Terraform or CloudFormation) — the tasks here create the manifests, scripts, and pipeline that reference them
