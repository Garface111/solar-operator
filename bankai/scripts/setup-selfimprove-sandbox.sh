#!/bin/bash
# Build the credential-free, network-cut jail that evaluates the copilot's OWN
# code proposals. Agent-authored tests are code under this system's threat
# model, so they run as an unprivileged user with no network and no reach into
# the live secrets or database. Idempotent; run as root on the runtime box.
#
# After running, VERIFY before trusting (bankai.selfimprove_sandbox.verify_sandbox
# does this in code): the jail must block network AND fail to read /opt/bankai/.env.
#
# Then add to /opt/bankai/.env:
#   SELFIMPROVE_SANDBOX=unshare --net --fork -- setpriv --reuid bnkeval --regid bnkeval --clear-groups --
#   SELFIMPROVE_SANDBOX_USER=bnkeval
#   SELFIMPROVE_REPO_DIR=/srv/bankai-eval/repo
#   SELFIMPROVE_VENV_PYTHON=/srv/bankai-eval/venv/bin/python
#
# IMPORTANT: refresh the base clone whenever you deploy new code, so proposals
# are tested against current source (the deploy recipe does the rsync below).
set -e

RUNTIME=/opt/bankai
EVAL=/srv/bankai-eval

# 1. Dedicated unprivileged jail user (no home, no shell).
id bnkeval >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin bnkeval

# 2. Base clone = the deployed source WITHOUT secrets/db/venv/vault.
mkdir -p "$EVAL"
rsync -a --delete \
  --exclude '.env' --exclude 'venv/' --exclude '*.db' --exclude '*.sqlite' \
  --exclude 'documents/' --exclude 'reports/' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude 'node_modules/' \
  --exclude 'whatsapp-bridge/node_modules/' \
  "$RUNTIME/" "$EVAL/repo/"
test ! -e "$EVAL/repo/.env" || { echo "FAIL: .env leaked into the clone"; exit 1; }

# 3. A venv the unprivileged jail user can reach (it cannot traverse /root).
if [ ! -x "$EVAL/venv/bin/python" ]; then
  python3 -m venv "$EVAL/venv"
  "$EVAL/venv/bin/pip" -q install --upgrade pip
  "$EVAL/venv/bin/pip" -q install sqlalchemy httpx pytest python-dotenv fpdf2
fi

# 4. The jail user owns the eval area; root keeps the secrets (600) and DB (700).
chown -R bnkeval:bnkeval "$EVAL"
chmod 755 "$EVAL"
echo "sandbox ready. Verify with bankai.selfimprove_sandbox.verify_sandbox() before trusting."
