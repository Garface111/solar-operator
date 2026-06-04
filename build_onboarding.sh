#!/usr/bin/env bash
# Build the onboarding SPA and copy its dist into api/onboarding_dist/ so the
# Railpack build picks it up. Run before every commit that touches web/onboarding/.
set -euo pipefail
cd "$(dirname "$0")"
cd web/onboarding && npm ci && npm run build
cd ../..
rm -rf api/onboarding_dist
cp -r web/onboarding/dist api/onboarding_dist
echo "✓ api/onboarding_dist/ refreshed from web/onboarding/dist/"
# Regenerate the public demo workbook the `rm -rf` above just wiped, so
# /onboarding/sample.xlsx keeps serving after a frontend rebuild.
python3 -m scripts.regen_demo_workbook
echo "✓ api/onboarding_dist/sample.xlsx regenerated"
