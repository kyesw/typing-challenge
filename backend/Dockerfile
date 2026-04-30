# ---- Stage 1: Builder ----
FROM python:3.11-slim AS builder

WORKDIR /build

# Copy project metadata and install production dependencies only
COPY pyproject.toml .
# Create a minimal app package so pip install . can resolve the project
COPY app/__init__.py app/__init__.py

RUN pip install --no-cache-dir .

# ---- Stage 2: Runtime ----
FROM python:3.11-slim AS runtime

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy application source code (tests and dev files excluded via .dockerignore)
COPY app/ ./app/

# Switch to non-root user
USER appuser

EXPOSE 8000

ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
