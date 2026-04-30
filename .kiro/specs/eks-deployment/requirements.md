# Requirements Document

## Introduction

This specification covers the deployment infrastructure for the Typing Game application. The frontend (React + Vite + TypeScript) will be served as static assets through Amazon CloudFront backed by an S3 bucket. The backend (Python FastAPI) will run on Amazon EKS (Elastic Kubernetes Service). Kubernetes manifests will be managed with Kustomize using a base-plus-overlays pattern to support multiple environments (dev, staging, production) with minimal duplication. The backend connects to an existing Amazon RDS PostgreSQL instance for persistent data storage, and API routing between the CloudFront-hosted frontend and the EKS-hosted backend must be established.

## Glossary

- **EKS_Cluster**: The Amazon Elastic Kubernetes Service cluster that hosts the backend application pods.
- **Backend_Pod**: A Kubernetes pod running the FastAPI backend container image.
- **Backend_Service**: A Kubernetes Service resource that exposes Backend_Pods within the EKS_Cluster.
- **Backend_Ingress**: A Kubernetes Ingress resource (AWS ALB Ingress Controller) that provisions an internet-facing Application Load Balancer (ALB) to route external API traffic to the Backend_Service.
- **Backend_ALB**: The internet-facing AWS Application Load Balancer provisioned by the Backend_Ingress. The Backend_ALB serves as the CloudFront origin for all API_Path_Prefix traffic.
- **Backend_Secret**: A Kubernetes Secret resource that stores sensitive backend configuration values such as database credentials and the `TYPING_GAME_DATABASE_URL`.
- **CloudFront_Distribution**: The Amazon CloudFront CDN distribution that serves the frontend static assets to end users.
- **S3_Asset_Bucket**: The Amazon S3 bucket that stores the production frontend build artifacts (contents of `frontend/dist/`).
- **Backend_Dockerfile**: The Dockerfile that produces the container image for the FastAPI backend.
- **Container_Registry**: The container image registry (e.g., Amazon ECR) where Backend_Pod images are stored and versioned.
- **Kustomize_Base**: The set of shared Kubernetes manifests (Deployment, Service, ConfigMap, Secret, Ingress) in the `k8s/base/` directory.
- **Kustomize_Overlay**: An environment-specific directory (`k8s/overlays/{env}/`) that patches the Kustomize_Base for a target environment.
- **Health_Endpoint**: The existing `GET /health` route on the FastAPI backend that returns `{"status": "ok", "environment": "..."}`.
- **API_Path_Prefix**: The set of URL path prefixes (`/players`, `/games`, `/leaderboard`, `/health`) that identify backend API requests.
- **RDS_Instance**: The pre-provisioned Amazon RDS PostgreSQL database instance that the backend connects to for persistent data storage.
- **Origin_Access_Control**: The CloudFront mechanism that restricts S3_Asset_Bucket access so objects are only reachable through the CloudFront_Distribution.
- **CI_CD_Pipeline**: The GitHub Actions workflow that automates building, testing, and deploying the backend and frontend on every push to the main branch.
- **GitHub_Actions**: The CI/CD platform integrated with GitHub that executes the CI_CD_Pipeline workflow.
- **Pipeline_Secrets**: The GitHub Actions encrypted secrets that store sensitive values such as AWS credentials, ECR URI, and RDS connection strings used during pipeline execution.
- **Backend_Test_Stage**: The CI_CD_Pipeline stage that installs backend dependencies and runs the Python test suite using pytest.
- **Frontend_Test_Stage**: The CI_CD_Pipeline stage that installs frontend dependencies and runs the TypeScript test suite using vitest.
- **Backend_Deploy_Stage**: The CI_CD_Pipeline stage that builds the backend Docker image, pushes the image to the Container_Registry, and applies Kustomize manifests to the EKS_Cluster.
- **Frontend_Deploy_Stage**: The CI_CD_Pipeline stage that builds the frontend static assets, syncs the assets to the S3_Asset_Bucket, and invalidates the CloudFront_Distribution cache.

## Requirements

### Requirement 1: Backend Container Image

**User Story:** As a DevOps engineer, I want a Dockerfile for the FastAPI backend, so that I can build a reproducible container image for deployment on EKS.

#### Acceptance Criteria

1. THE Backend_Dockerfile SHALL produce a container image that runs the FastAPI backend using uvicorn on a configurable port (default 8000).
2. THE Backend_Dockerfile SHALL use a multi-stage build with a Python 3.11 slim base image to minimize the final image size.
3. THE Backend_Dockerfile SHALL copy only production dependencies and application code into the final stage, excluding dev dependencies and test files.
4. THE Backend_Dockerfile SHALL define a non-root user to run the application process.
5. WHEN the container starts, THE Backend_Pod SHALL read all runtime configuration from environment variables prefixed with `TYPING_GAME_`.
6. THE Backend_Dockerfile SHALL expose port 8000 as the default listening port.
7. THE Backend_Dockerfile SHALL include the `psycopg2-binary` Python package in the production dependencies to enable PostgreSQL connectivity via SQLAlchemy.

### Requirement 2: Kustomize Base Manifests

**User Story:** As a DevOps engineer, I want a set of base Kubernetes manifests managed by Kustomize, so that I have a single source of truth for the backend deployment that can be customized per environment.

#### Acceptance Criteria

1. THE Kustomize_Base SHALL include a `kustomization.yaml` file that references a Deployment, Service, ConfigMap, Secret, and Ingress resource.
2. THE Kustomize_Base SHALL define a Deployment resource that specifies the Backend_Pod container image, resource requests, resource limits, replica count, and a RollingUpdate deployment strategy.
3. THE Kustomize_Base SHALL define a Service resource of type ClusterIP that routes traffic to Backend_Pods on port 8000.
4. THE Kustomize_Base SHALL define a ConfigMap resource that holds default values for all non-sensitive `TYPING_GAME_`-prefixed environment variables.
5. THE Kustomize_Base SHALL define a Secret resource that holds sensitive configuration values including `TYPING_GAME_DATABASE_URL` containing the RDS PostgreSQL connection string.
6. THE Kustomize_Base SHALL define an Ingress resource annotated for the AWS ALB Ingress Controller with the `alb.ingress.kubernetes.io/scheme: internet-facing` annotation, routing API_Path_Prefix traffic to the Backend_Service and provisioning an internet-facing ALB.
7. THE Kustomize_Base SHALL reside in the `k8s/base/` directory relative to the project root.

### Requirement 3: Kustomize Environment Overlays

**User Story:** As a DevOps engineer, I want environment-specific Kustomize overlays for dev, staging, and production, so that I can deploy the backend with appropriate configuration for each environment without duplicating manifests.

#### Acceptance Criteria

1. WHEN the dev overlay is applied, THE Kustomize_Overlay SHALL set the replica count to 1 and use relaxed resource limits.
2. WHEN the staging overlay is applied, THE Kustomize_Overlay SHALL set the replica count to 2 and use resource limits matching production.
3. WHEN the production overlay is applied, THE Kustomize_Overlay SHALL set the replica count to a minimum of 3 and use production resource limits.
4. THE Kustomize_Overlay for each environment SHALL set the `TYPING_GAME_ENVIRONMENT` variable to the corresponding environment name (development, staging, production).
5. THE Kustomize_Overlay for each environment SHALL set the container image tag to an environment-appropriate value using Kustomize image transformers.
6. THE Kustomize_Overlay directories SHALL reside at `k8s/overlays/dev/`, `k8s/overlays/staging/`, and `k8s/overlays/prod/` relative to the project root.

### Requirement 4: Health Checks and Readiness Probes

**User Story:** As a DevOps engineer, I want Kubernetes health checks configured for the backend pods, so that unhealthy pods are automatically restarted and traffic is only routed to ready pods.

#### Acceptance Criteria

1. THE Kustomize_Base Deployment SHALL define a liveness probe that sends an HTTP GET request to the Health_Endpoint on port 8000.
2. THE Kustomize_Base Deployment SHALL define a readiness probe that sends an HTTP GET request to the Health_Endpoint on port 8000.
3. THE liveness probe SHALL use an initial delay of 10 seconds and a period of 15 seconds.
4. THE readiness probe SHALL use an initial delay of 5 seconds and a period of 10 seconds.
5. IF the Health_Endpoint returns a non-200 status code, THEN THE EKS_Cluster SHALL mark the Backend_Pod as not ready and stop routing traffic to the pod.
6. IF the liveness probe fails 3 consecutive times, THEN THE EKS_Cluster SHALL restart the Backend_Pod.

### Requirement 5: RDS PostgreSQL Connectivity

**User Story:** As a DevOps engineer, I want the backend to connect to the existing RDS PostgreSQL instance, so that game data is stored in a managed, durable database.

#### Acceptance Criteria

1. THE Backend_Pod SHALL connect to the RDS_Instance using the `TYPING_GAME_DATABASE_URL` environment variable containing a PostgreSQL connection string in the format `postgresql://<user>:<password>@<rds-endpoint>:<port>/<database>`.
2. THE Backend_Secret SHALL store the RDS PostgreSQL username and password as opaque Kubernetes Secret data entries.
3. THE Kustomize_Base Deployment SHALL inject the `TYPING_GAME_DATABASE_URL` value from the Backend_Secret as an environment variable into the Backend_Pod container.
4. THE Kustomize_Overlay for each environment SHALL override the Backend_Secret with the RDS_Instance connection string specific to that environment.
5. IF the Backend_Pod cannot establish a connection to the RDS_Instance, THEN THE Health_Endpoint SHALL return a non-200 status code so that the readiness probe marks the pod as not ready.
6. THE Backend_Dockerfile SHALL include the `psycopg2-binary` Python package to provide the PostgreSQL database driver for SQLAlchemy.

### Requirement 6: Frontend Build and S3 Deployment

**User Story:** As a DevOps engineer, I want an automated process to build the frontend and upload the artifacts to S3, so that the latest frontend is available through CloudFront after each release.

#### Acceptance Criteria

1. WHEN a frontend deployment is triggered, THE build process SHALL run `npm run build` in the `frontend/` directory to produce optimized static assets in `frontend/dist/`.
2. WHEN the build completes successfully, THE deployment process SHALL sync the contents of `frontend/dist/` to the S3_Asset_Bucket using `aws s3 sync` with the `--delete` flag to remove stale files.
3. WHEN the S3 sync completes, THE deployment process SHALL create a CloudFront invalidation for `/*` to clear cached assets.
4. THE S3_Asset_Bucket SHALL have public access blocked and serve objects only through Origin_Access_Control.
5. THE S3_Asset_Bucket SHALL enable versioning to support rollback of frontend deployments.
6. IF the `npm run build` command exits with a non-zero status, THEN THE deployment process SHALL abort and report the build failure without modifying the S3_Asset_Bucket.

### Requirement 7: CloudFront Distribution Configuration

**User Story:** As a DevOps engineer, I want a CloudFront distribution configured to serve the frontend and route API calls to the backend, so that users get fast static asset delivery and seamless API connectivity from a single domain.

#### Acceptance Criteria

1. THE CloudFront_Distribution SHALL define a default origin pointing to the S3_Asset_Bucket using Origin_Access_Control.
2. THE CloudFront_Distribution SHALL define a second origin pointing to the Backend_ALB, using the ALB DNS name as the origin domain for all API traffic.
3. THE CloudFront_Distribution SHALL configure cache behaviors that route requests matching API_Path_Prefix patterns (`/players/*`, `/games/*`, `/leaderboard/*`, `/health`) to the Backend_ALB origin.
4. THE CloudFront_Distribution SHALL configure the default cache behavior (`*`) to serve static assets from the S3_Asset_Bucket origin.
5. WHEN a request for a path that does not match any S3 object is received on the default behavior, THE CloudFront_Distribution SHALL return `index.html` with a 200 status code to support client-side routing.
6. THE CloudFront_Distribution SHALL enforce HTTPS by redirecting HTTP requests to HTTPS.
7. THE CloudFront_Distribution SHALL disable caching for API_Path_Prefix cache behaviors (cache policy with TTL of 0) to ensure API responses are always fresh.
8. THE CloudFront_Distribution SHALL enable compression (gzip, Brotli) for static asset responses.

### Requirement 8: Container Registry and Image Management

**User Story:** As a DevOps engineer, I want container images stored in a managed registry with a clear tagging strategy, so that deployments reference immutable, versioned images.

#### Acceptance Criteria

1. THE Container_Registry SHALL be an Amazon ECR repository in the same AWS region as the EKS_Cluster.
2. THE Container_Registry SHALL have an image lifecycle policy that retains the 20 most recent tagged images and removes untagged images older than 7 days.
3. WHEN a new backend image is built, THE build process SHALL tag the image with both the git commit SHA and a semantic version tag.
4. THE Container_Registry SHALL have image scanning enabled on push to detect known vulnerabilities.
5. THE Kustomize_Overlay for each environment SHALL reference images by their full ECR URI including the image tag.

### Requirement 9: Backend Environment Configuration on EKS

**User Story:** As a DevOps engineer, I want all backend configuration managed through Kubernetes ConfigMaps and Secrets, so that configuration changes do not require image rebuilds.

#### Acceptance Criteria

1. THE Kustomize_Base ConfigMap SHALL include entries for all non-sensitive `TYPING_GAME_`-prefixed environment variables: `TYPING_GAME_ENVIRONMENT`, `TYPING_GAME_SESSION_TTL_SECONDS`, `TYPING_GAME_MAX_GAME_DURATION_SECONDS`, and `TYPING_GAME_PROMPT_SELECTION_POLICY`.
2. THE Backend_Secret SHALL include the `TYPING_GAME_DATABASE_URL` entry containing the RDS PostgreSQL connection string, keeping database credentials out of the ConfigMap.
3. THE Kustomize_Base Deployment SHALL inject ConfigMap entries as environment variables into the Backend_Pod container.
4. THE Kustomize_Base Deployment SHALL inject Backend_Secret entries as environment variables into the Backend_Pod container.
5. WHEN a ConfigMap value is updated, THE deployment process SHALL perform a rolling restart of Backend_Pods to pick up the new configuration.
6. THE Kustomize_Overlay for each environment SHALL override ConfigMap values appropriate for that environment (e.g., session TTL, game duration, rate limits).
7. THE Kustomize_Overlay for each environment SHALL override Backend_Secret values with the RDS_Instance credentials specific to that environment.

### Requirement 10: Deployment Execution with Kustomize

**User Story:** As a DevOps engineer, I want to deploy any environment with a single Kustomize command, so that deployments are simple and repeatable.

#### Acceptance Criteria

1. WHEN a deployment is initiated for an environment, THE operator SHALL apply manifests using `kubectl apply -k k8s/overlays/{env}/` where `{env}` is one of dev, staging, or prod.
2. THE Kustomize_Base and Kustomize_Overlay combination SHALL produce a valid, self-contained set of Kubernetes manifests when rendered with `kubectl kustomize k8s/overlays/{env}/`.
3. THE rendered manifests SHALL pass `kubectl apply --dry-run=client` validation without errors.
4. THE Kustomize_Base SHALL use a common label (`app: typing-game-backend`) applied to all resources for consistent selection and filtering.
5. THE Kustomize_Base SHALL define a namespace field so that all resources are created in a dedicated namespace (e.g., `typing-game`).

### Requirement 11: CI/CD Pipeline

**User Story:** As a DevOps engineer, I want a GitHub Actions CI/CD pipeline that builds, tests, and deploys the backend and frontend on every push to the main branch, so that validated changes are automatically released without manual intervention.

#### Acceptance Criteria

1. THE CI_CD_Pipeline SHALL be defined as a GitHub_Actions workflow file at `.github/workflows/deploy.yml`.
2. WHEN code is pushed to the `main` branch, THE GitHub_Actions platform SHALL trigger the CI_CD_Pipeline.
3. THE Backend_Test_Stage SHALL install Python 3.11 dependencies and run `pytest` against the backend test suite.
4. THE Frontend_Test_Stage SHALL install Node.js dependencies and run `vitest --run` against the frontend test suite.
5. IF the Backend_Test_Stage exits with a non-zero status, THEN THE CI_CD_Pipeline SHALL skip all subsequent stages and report the failure.
6. IF the Frontend_Test_Stage exits with a non-zero status, THEN THE CI_CD_Pipeline SHALL skip all subsequent stages and report the failure.
7. WHEN the Backend_Test_Stage and Frontend_Test_Stage both pass, THE Backend_Deploy_Stage SHALL build the backend Docker image using the Backend_Dockerfile and tag the image with the git commit SHA.
8. WHEN the backend Docker image is built, THE Backend_Deploy_Stage SHALL authenticate with the Container_Registry and push the tagged image to the ECR repository.
9. WHEN the image is pushed to the Container_Registry, THE Backend_Deploy_Stage SHALL update the Kustomize image tag to the git commit SHA and apply manifests to the EKS_Cluster using `kubectl apply -k k8s/overlays/prod/`.
10. WHEN the Backend_Test_Stage and Frontend_Test_Stage both pass, THE Frontend_Deploy_Stage SHALL run `npm run build` in the `frontend/` directory to produce optimized static assets.
11. WHEN the frontend build completes, THE Frontend_Deploy_Stage SHALL sync the contents of `frontend/dist/` to the S3_Asset_Bucket using `aws s3 sync --delete`.
12. WHEN the S3 sync completes, THE Frontend_Deploy_Stage SHALL create a CloudFront invalidation for `/*` on the CloudFront_Distribution.
13. THE CI_CD_Pipeline SHALL retrieve AWS credentials, the ECR repository URI, the RDS connection string, and the CloudFront distribution ID from Pipeline_Secrets.
14. THE CI_CD_Pipeline SHALL configure AWS credentials using the `aws-actions/configure-aws-credentials` GitHub Action with an IAM access key stored in Pipeline_Secrets.
15. THE CI_CD_Pipeline SHALL use Pipeline_Secrets named `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `ECR_REPOSITORY_URI`, `TYPING_GAME_DATABASE_URL`, and `CLOUDFRONT_DISTRIBUTION_ID`.
16. IF the `docker build` command exits with a non-zero status, THEN THE CI_CD_Pipeline SHALL abort the Backend_Deploy_Stage and report the failure.
17. IF the `kubectl apply -k` command exits with a non-zero status, THEN THE CI_CD_Pipeline SHALL report the deployment failure.
