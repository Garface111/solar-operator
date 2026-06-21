# Auth / login hardening — multi-tenant-per-email, signup dedup, session secret

Solar Operator / EnergyAgent auth model and the launch-readiness fixes (Jun 2026).
Load this when touching login, signup, magic-link, sessions, or when Ford reports
"login glitches / account errors / I can't log into the account I made."

## The mental model that prevents the whack-a-mole

**One email can legitimately own MORE THAN ONE Tenant — one per PRODUCT.** A person can
be both a NEPOOL Operator (verifier) AND an Array Operator (owner) on the same email.
`Tenant.product` is `nepool` | `array_operator`. Every auth path must be **per-product
aware**, or a correct credential silently lands in (or fails against) the wrong account.
This multi-tenant-per-email reality is the root cause behind essentially every "login
whack-a-mole" symptom Ford has reported.

Auth primitives (all in `api/account.py`):
- `_sign_session(tenant_id, ttl=30d)` / `_verify_session` — compact HMAC-SHA256 over
  `{tid, exp}`, signed with `SESSION_SECRET`. Signs the **`Tenant.id` PK** (`ten_…`), NOT
  `tenant_key`. 30-day sessions; magic-link tokens are 15-min single-use (`LoginToken`).
- `tenant_from_session(authorization)` — resolves a bearer session token. **Deliberately
  ALLOWS inactive tenants** (read-only) so a canceled owner can still see status / export.
  Mutating endpoints gate separately on `active`/subscription → **402**, not 401.
- Password: bcrypt (`_hash_password`/`_verify_password`, rounds=12); `Tenant.password_hash`
  is nullable (null = magic-link-only account).

## The three login bugs fixed this session (and the rule each leaves behind)

### 1. password-login checked ONE arbitrary tenant → correct password failed
`/v1/auth/password-login` did `select(Tenant).where(contact_email==email).first()` (no
ordering, no product filter) and verified the password against just that one. If the
email's OTHER tenant sorted first, a CORRECT password returned the generic
"Invalid email or password". This is exactly the "I made the account but can't log in"
symptom.

**FIX (the rule):** verify the password against **EVERY** tenant for the email, collect the
matches, and pick deterministically:
```python
candidates = db.execute(select(Tenant).where(Tenant.contact_email == email)).scalars().all()
matches = [t for t in candidates if t.password_hash and _verify_password(pw, t.password_hash)]
if not matches: raise HTTPException(401, "Invalid email or password")
def _rank(t):  # requested product > active > newest
    return (0 if (want_product and (t.product or "nepool")==want_product) else 1,
            0 if t.active else 1,
            -(t.created_at.timestamp() if t.created_at else 0))
chosen = sorted(matches, key=_rank)[0]
```
The request body gained an optional `product` field; the AO `login.html` sends
`product:"array_operator"` so a shared email prefers the AO account. Still returns the
generic 401 on no match (no email enumeration).

### 2. magic-link guessed the wrong tenant
`issue_magic_link` picked `ORDER BY active DESC, created_at DESC` — ambiguous when an email
owns two tenants. **FIX:** `issue_magic_link(email, persist, product=None)` and
`/v1/auth/request` (`AuthRequest.product`) now PREFER the tenant in the requested product
when given. The AO login page passes `product:"array_operator"` on the magic-link request
too. Without a product hint it falls back to the old active/newest ordering (fine for
single-product emails).

### 3. signup only blocked ACTIVE duplicates → you accumulated two accounts
`/v1/onboarding/start` (`_create_trial_tenant` in `api/onboarding.py`) checked
`contact_email == email AND active == True`. Both of Ford's accounts were inactive, so the
guard never fired and a second `array_operator` tenant was minted on the same email — the
source of the duplication.

**FIX (the rule):** block a duplicate **within the same product** whether active OR
inactive; ALLOW the same email across DIFFERENT products:
```python
existing = db.execute(
    select(Tenant).where(Tenant.contact_email == email, Tenant.product == product)
    .order_by(Tenant.active.desc(), Tenant.created_at.desc())
).scalars().first()
if existing: raise HTTPException(409, "An account already exists for this email. Sign in instead…")
```
Keep `.first()` (NOT `scalar_one_or_none()`) — legacy/raced data can leave >1 row and
`scalar_one_or_none` raises MultipleResultsFound → 500, permanently wedging signup for that
email.

## SESSION_SECRET — the mass-logout landmine (ops, not code)

`SESSION_SECRET = os.getenv("SESSION_SECRET", "")`; if blank it falls back to
`sha256(DATABASE_URL)`. That fallback is DETERMINISTIC (sessions survive deploys) BUT it
means **if Railway ever rotates the DB password, every active session for every user dies at
once** (silent mass logout). Before launch, pin it explicitly:
1. Read the CURRENT effective value so existing sessions survive the change:
   `railway ssh "cd /app && python -c \"import os,hashlib;print(hashlib.sha256((os.getenv('DATABASE_URL','') or 'fallback-dev-secret').encode()).hexdigest())\""`
2. `railway variables --set "SESSION_SECRET=<that 64-char hash>"` (triggers a redeploy).
3. **VERIFY the full value stored** (the `railway variables` table TRUNCATES the display, and
   a one-shot set can silently store a truncated string → would invalidate every session):
   after redeploy, `railway ssh "... python -c \"import os;ss=os.getenv('SESSION_SECRET','');print(len(ss), ss=='<expected>')\""`
   → must print `64 True`.

## Verifying a login fix on prod (bearer-mangling-safe)

The redactor mangles inline bearer tokens AND inline `python3 -c "(...)"`. Use temp files:
```
cat > /tmp/lt.json <<'EOF'
{"email":"…","password":"…","product":"array_operator"}
EOF
curl -s -X POST "https://web-production-49c83.up.railway.app/v1/auth/password-login" \
  -H "Content-Type: application/json" --data @/tmp/lt.json -o /tmp/lr.json -w "HTTP %{http_code}\n"
# then parse /tmp/lr.json in a SEPARATE call: expect ok:true, product matches what you asked for
```

## Getting Ford into an account that has no password (non-destructive)

If a tenant has `password_hash=None` (e.g. created by the test/onboarding flow), password
login can't work. Non-destructive unlocks, all via `railway ssh` python:
- Set a temp password: `t.password_hash = _hash_password("Temp-…!")` (he changes it in the
  Master Account tab after).
- Mint a single-use magic token bound to THAT EXACT tenant id (no email ambiguity):
  `LoginToken(token=secrets.token_urlsafe(32), tenant_id=tid, email=…, expires_at=+24h)`.
- Or mint a ready session for `?token=` entry: `_sign_session(tid)`.
Never delete an account to "fix" login — see the deletion-safety note in the main skill.

## Tests
`tests/test_password_auth.py::TestMultiTenantEmail` (verify-vs-all-tenants, product routing,
wrong-pw-still-401) and `tests/test_onboarding.py` (inactive-dup 409, cross-product allowed).
Full suite was 912 after these fixes.

## Launch-readiness items NOT yet done (flag to Ford before "ready for thousands")
- Rate-limiting (`api/ratelimit.py`) is in-memory PER-PROCESS — not shared across Railway
  replicas. Needs a Redis-backed limiter before real concurrency.
- No load test of signup/login concurrency, DB pool sizing, or Stripe-webhook idempotency
  under burst.
- Existing duplicate accounts in prod are not auto-cleaned (the fix only stops NEW ones).
  Best practice for Ford going forward: a distinct email per product, or consolidate.
