# Solar Operator — Operator Journey Map (V6, Jun 3 2026)

Every operator (tenant) journey from anonymous landing → fully onboarded → returning.
Maps every screen transition, every email trigger, and every error path. This is an
**audit artifact** — the Smoothness audit below flags friction; nothing is fixed here.

## Legend

- **green edge** = happy path (forward progress, success)
- **yellow edge** = wait / poll / async lag (operator is waiting on something)
- **red edge** = error path (failure, expiry, auth reject, abandonment)
- 📧 = an email is sent at this transition

## Flowchart

```mermaid
flowchart TD
    %% ── Anonymous → Sign up ──────────────────────────────────
    Anon([Anonymous visitor]) -->|visits solaroperator.org| Landing[Landing page]
    Landing -->|clicks Sign up CTA| W1

    %% ── Onboarding wizard ────────────────────────────────────
    subgraph WIZARD[Onboarding wizard]
        W1[1. Welcome and agreement] -->|agree to terms| W2[2. Operator info form]
        W2 -->|POST /v1/onboarding/checkout| Stripe[[Stripe Checkout]]
        Stripe -->|payment success<br/>webhook: active + stage=extension| W3[3. Install extension]
        W3 -->|GET extension-ping detects capture| W4[4. Add clients]
        W3 -.->|manual: I have installed it<br/>POST extension-installed| W4
        W4 -->|POST /v1/onboarding/clients<br/>reconcile Stripe qty| W5[5. Done]
        W5 -->|POST /v1/onboarding/complete| Done([Onboarded])
    end

    %% ── Onboarding email triggers ────────────────────────────
    W5 -.->|📧 magic-link sign-in| MailMagic[(magic-link email)]
    W5 -.->|📧 sample workbook| MailSample[(sample report email)]
    W5 -.->|📧 internal alert: onboarding complete| MailOps[(internal alert)]

    %% ── Happy path styling for wizard forward edges ──────────
    Anon -. green .-> Landing

    %% ── Error paths in wizard ────────────────────────────────
    Stripe -->|cancel → /info?cancelled=1| W2err[Back to info form]
    Stripe -->|payment success but webhook lag<br/>poll GET /status until active| W3wait{{stage still pending_payment}}
    W3wait -->|webhook lands / self-heal lookup| W3
    W3wait -->|never activates| StuckPay[STUCK: paid but inactive]
    W3 -->|extension never installed<br/>ping never returns capture| W3stuck[STUCK on Screen 3]
    W4 -->|extension-installed before payment<br/>402| W4err[Blocked: pay first]
    W4 -->|Stripe qty reconcile fails| ReconcileWarn[⚠️ internal alert,<br/>onboarding continues]

    %% ── Returning user ───────────────────────────────────────
    Return([Returning operator]) -->|POST /v1/auth/request| AuthReq[Request magic link]
    AuthReq -.->|📧 magic-link| MailMagic
    MailMagic -->|click link<br/>POST /v1/auth/verify| AuthOK[Session token issued]
    AuthOK -->|GET /accounts/{tab}| Dash[Account dashboard]
    MailMagic -->|link expired / used| AuthExpired[Expired link → re-request]
    AuthExpired -.-> AuthReq

    %% ── Dashboard tabs ───────────────────────────────────────
    subgraph DASH[Dashboard /accounts/tab]
        Dash --> TabAccount[Account tab]
        Dash --> TabClients[Clients tab]
        Dash --> TabReports[Reports tab]

        TabAccount -->|POST /v1/account/frequency| ChangeFreq[Change frequency]
        TabAccount -->|POST /v1/account/cc-on-reports| ToggleCC[Toggle cc_on_reports]
        TabAccount -->|POST /v1/account/email| ChangeEmail[Change email → sync Stripe]
        TabAccount -->|GET /v1/account/billing-portal| Billing[[Stripe billing portal]]

        TabClients -->|POST /v1/account/clients| AddClient[Add client]
        TabClients -->|PATCH clients/id| EditClient[Edit client / freq / cc]
        TabClients -->|POST clients/id/arrays| AddArray[Add array]
        TabClients -->|PATCH arrays/id| EditArray[Edit NEPOOL ID / offset]

        TabReports -->|POST clients/id/send-report| SendOneNow[Send now: one client]
        TabReports -->|POST /v1/account/send-report| SendAllNow[Send now: all my clients]
    end

    %% ── Report delivery + emails ─────────────────────────────
    SendOneNow -.->|📧 client report| MailReport[(client report email)]
    SendAllNow -.->|📧 client reports| MailReport
    MailReport -.->|if cc_on_reports on<br/>📧 [copy] to operator| MailCopy[(cc copy email)]

    %% ── Scheduler (background, no UI) ────────────────────────
    Sched([APScheduler]) -.->|Mon 09:00 UTC| SchedWeekly[deliver weekly clients]
    Sched -.->|1st of month 09:00| SchedMonthly[deliver monthly clients]
    Sched -.->|1st Jan/Apr/Jul/Oct 09:00| SchedQuarterly[deliver quarterly clients<br/>DEFAULT cadence]
    SchedWeekly -.->|📧 report| MailReport
    SchedMonthly -.->|📧 report| MailReport
    SchedQuarterly -.->|📧 report| MailReport

    %% ── Extension sync + autopopulate ────────────────────────
    Ext([Chrome extension]) -->|POST /v1/sync with activation code| SyncOK[Session captured]
    SyncOK -->|gmp_autopopulate match| AutoPop[Append arrays + accounts]
    Ext -->|bad/blank activation code<br/>401 auth fail| SyncFail[Sync rejected]

    %% ── Billing failure paths ────────────────────────────────
    Billing -->|invoice.payment_failed webhook| PayFail[Mark past_due]
    PayFail -.->|📧 payment failed| MailPayFail[(payment failed email)]
    Billing -->|subscription canceled webhook| Canceled[Mark canceled]
    Canceled -.->|📧 cancellation| MailCancel[(cancellation email)]

    %% ── Edge styling ─────────────────────────────────────────
    classDef happy fill:#d7f5dd,stroke:#10b981,color:#063;
    classDef wait fill:#fdf3d0,stroke:#E8C547,color:#6b5800;
    classDef err fill:#fce0e0,stroke:#d33,color:#800;
    classDef mail fill:#e6eefb,stroke:#3FA8D8,color:#0a3a5e;

    class Done,Dash,SyncOK,AutoPop,AuthOK happy;
    class W3wait,ReconcileWarn,PayFail wait;
    class W2err,StuckPay,W3stuck,W4err,AuthExpired,SyncFail,Canceled err;
    class MailMagic,MailSample,MailOps,MailReport,MailCopy,MailPayFail,MailCancel mail;
```

> Mermaid note: link styling (`linkStyle`) by color is renderer-version-dependent, so
> edge intent is encoded in **labels** (cancel / lag / never / fail / expired = red;
> poll / wait / webhook lag = yellow; everything else = green) and node `classDef`
> colors. Read the labels, not just the arrowheads.

## Email triggers (complete list)

| Trigger | Function | When |
|---|---|---|
| Magic-link sign-in | `issue_magic_link` → `account.py` | `/complete`, and every `/v1/auth/request` |
| Sample workbook | `send_sample_workbook_email` | onboarding `/complete` (best-effort) |
| Welcome | `send_welcome_email` | `/complete` (onboarding) **and** legacy webhook for non-token tenants |
| Client report | `deliver_for_client` → `send_workbook_email` | send-now (per client / all) + scheduler weekly/monthly/quarterly |
| CC copy `[copy]` | `deliver_for_client` when `tenant.cc_on_reports` | alongside every client report |
| Payment failed | `send_payment_failed_email` | `invoice.payment_failed` webhook |
| Cancellation | `send_cancellation_email` | `customer.subscription.deleted` webhook |
| Internal alerts | `send_internal_alert` | onboarding complete, reconcile failure, delivery failures |

## Smoothness audit (friction / ambiguity — NOT fixed here)

1. **Webhook lag after payment (Screen 2 → 3).** `/status` is polled until `active`, but
   if `checkout.session.completed` never lands and the operator didn't return via the
   self-heal `/v1/checkout/{sid}` lookup (that path is legacy-flow only — the onboarding
   success_url goes straight to `/extension`), they sit "paid but inactive" with no
   recovery affordance. **No self-heal in the onboarding wizard.** High-severity.

2. **Screen 3 dead-end (extension never installs).** `extension-ping` only ever returns
   `installed:false`; the sole escape is the manual "I've installed it" button, which
   advances the stage even if no capture ever happened — so an operator can reach Screen 4
   with a non-functional extension and not find out until reports silently never generate.
   No "test connection" affordance (this is exactly V3).

3. **No email preview before any report goes out.** Send-now and scheduled deliveries fire
   under the operator's professional name with zero preview/customization (this is V2).
   Trust risk: the operator can't see what their client receives.

4. **`cc_on_reports` is tenant-wide, not per-client.** An operator who wants a copy of one
   client's reports gets copies of all of them. Ambiguous granularity.

5. **Frequency change has no "applies-when" feedback.** Changing to quarterly on the 2nd of
   a quarter month means the next report is ~3 months out, with no UI indication of the next
   send date. Operator may expect an immediate send.

6. **Quarterly default (V1, now shipped) vs. existing monthly users.** The migration only
   flips test signups <7 days old; older users keep monthly. Fine, but there's no in-product
   nudge telling them quarterly is now recommended.

7. **Stripe quantity reconcile is best-effort + silent to the operator.** If it fails after
   Screen 4, only Ford gets an internal alert; the operator believes billing matches their
   array count when it may not. Money-path opacity.

8. **`/v1/sync` auth failure is invisible to the operator.** A bad/blank activation code
   401s the extension, but nothing surfaces in the dashboard — the operator just sees reports
   never populate. No "last successful sync" health indicator on the dashboard.

9. **Magic-link expiry loops back to request with no inline explanation.** Expired/used links
   bounce to a re-request, but the operator isn't told *why* (expired vs. already-used).

10. **Cancel from Stripe Checkout → `/info?cancelled=1`.** Lands back on the info form with a
    query flag, but the form may re-prompt for email and hit the 409 "account already exists"
    guard if a pending tenant row was created — abandonment can wedge re-entry. Needs a check.

11. **Add-array NEPOOL ID is free-text with no validation.** Editing/typing a NEPOOL-GIS ID
    has no format check; a typo silently produces a wrong `"<Array> (<id>)"` sheet title.

12. **Two emails at `/complete` (magic-link + sample) can race in the inbox.** Operator may
    click the sample-notification before the magic-link arrives, with unclear next step.
