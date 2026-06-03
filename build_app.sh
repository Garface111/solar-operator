#!/usr/bin/env bash
# Build the customer account dashboard SPA and copy its dist into api/app_dist/
# so the Railpack build picks it up. Run before every commit that touches web/app/.
set -euo pipefail
cd "$(dirname "$0")"
cd web/app && npm ci && npm run build
cd ../..
rm -rf api/app_dist
cp -r web/app/dist api/app_dist
echo "✓ api/app_dist/ refreshed from web/app/dist/"
