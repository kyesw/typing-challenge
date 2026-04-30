#!/usr/bin/env bash
set -euo pipefail

# Validate required environment variables
if [[ -z "${S3_BUCKET_NAME:-}" ]]; then
  echo "Error: S3_BUCKET_NAME environment variable is not set" >&2
  exit 1
fi

if [[ -z "${CLOUDFRONT_DISTRIBUTION_ID:-}" ]]; then
  echo "Error: CLOUDFRONT_DISTRIBUTION_ID environment variable is not set" >&2
  exit 1
fi

# Build frontend assets
echo "Building frontend..."
npm run build --prefix frontend/

# Sync build output to S3
echo "Syncing frontend/dist/ to s3://${S3_BUCKET_NAME}/..."
aws s3 sync frontend/dist/ "s3://${S3_BUCKET_NAME}/" --delete

# Invalidate CloudFront cache
echo "Creating CloudFront invalidation..."
aws cloudfront create-invalidation \
  --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
  --paths "/*"

echo "Frontend deployment complete."
