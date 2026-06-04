# Friction Comb — Lens 3: Implicit Knowledge / Undocumented Assumptions

READ-ONLY audit. No code was changed. Scope: every operator-facing screen where
the system relies on context it never gives the user — status indicators, codes,
side-effecting settings, background processes, field labels, pricing math, email
behavior, extension behavior, data freshness, and privacy.

Audited: `api/notify.py`, `api/delivery.py`, `api/email_templates.py`,
`web/onboarding/src/screens/*`, `web/app/src/screens/*`,
`web/app/src/components/*`, `web/app/src/ui/*`.

---

## Executive summary

The product is competent and the *happy path* copy is mostly good. The implicit-
knowledge debt clusters in three places: (1) **fields and counts that mean
something to the engineer but nothing to a stamping agent** (bill offset, NEPOOL
ID, "utility accounts" / "bills on file"), (2) **background processes the operator
can't see** (auto-populate has no "last synced" anywhere — the dashboard is a
black box about whether capture is working), and (3) **money + data-loss actions
whose mechanics are never reconciled on screen** (you're billed per array but the
dashboard never shows an array count or charge; deleting an array is silent,
final, and destroys bills, while deleting a client is a reversible soft-delete —
opposite semantics, no signposting).

### Count by severity
- **Blocker:** 0
- **High:** 8
- **Medium:** 9
- **Polish:** 4

### Top 5 biggest knowledge gaps
1. **Bill offset (months)** — a raw engineering field exposed inline on every
   array with placeholder `1` and zero explanation. Wrong value silently shifts
   which month's generation lands in which report row. (Finding 1)
2. **NEPOOL-GIS ID** — the canonical identifier that becomes the report sheet
   title, presented as "optional" with placeholder `NON12345` and no hint of
   what it is, where to find it, or why a missing one degrades the report. (Finding 2)
3. **Pricing never reconciles on screen** — onboarding promises "$45/array/month"
   but neither checkout nor the dashboard ever shows how many arrays you're billed
   for. "Utility accounts" and "Bills on file" are shown instead; array count is
   not. The operator cannot verify their own invoice. (Finding 3)
4. **Auto-populate is invisible** — "arrays appear once GMP auto-populate runs"
   with no "last synced" timestamp, no capture status, no freshness signal
   anywhere in the dashboard. The operator can't tell working from broken. (Finding 4)
5. **Delete-array (final, destructive) vs deactivate-client (soft, reversible)** —
   opposite reversibility semantics on adjacent screens, no signposting. Array
   delete destroys linked utility accounts and bills permanently. (Finding 5)

---

### Finding 1 — "Bill offset (months)" is a naked engineering field

**Where it shows up:** Dashboard → Clients → expand client → array row, "Bill
offset" column. `web/app/src/components/ArrayList.tsx:178-192` (inline edit) and
`web/app/src/components/ArrayList.tsx:481-489` (new-array form, defaults to `1`).

**What's implicit:** The operator must already know that GMP bills generation in
the *prior* month for most arrays, that this field shifts which calendar month a
bill's MWh is attributed to, and that some arrays (Bruce's Starlake) use `0` for
same-month. The label is "Bill offset", the placeholder is "1", and there is no
helper text at all. This is the writer's `bill_offset_months` model field leaked
verbatim into the UI.

**Why it matters:** A wrong offset doesn't error — it silently puts the right MWh
in the wrong month row of a NEPOOL report that a regulator reads. Highest-trust-
risk field in the app, and the one with the least explanation. Likely support
load when an operator "fixes" a number they don't understand.

**Severity:** High

**Recommended addition:** Inline helper text: "Most GMP arrays bill the prior
month's generation — leave this at 1. Set 0 only if this array's bill shows the
same month it's generated." Consider a tooltip linking to a one-paragraph doc.

**Files involved:** `web/app/src/components/ArrayList.tsx:178-192,481-489`,
`api/models.py` (`Array.bill_offset_months`)

---

### Finding 2 — NEPOOL-GIS ID: no what / where / why

**Where it shows up:** Onboarding → Clients (`web/onboarding/src/screens/Clients.tsx:283-293`,
labeled "NEPOOL-GIS ID (optional)", placeholder `NON12345`); Dashboard array row
(`web/app/src/components/ArrayList.tsx:169-176`); new-array form
(`web/app/src/components/ArrayList.tsx:472-479`); import preview header "NEPOOL ID"
(`web/app/src/components/ImportSpreadsheetModal.tsx:230`).

**What's implicit:** That the NEPOOL-GIS ID is the canonical registry identifier
for the array, that it becomes the parenthetical in the report sheet title
(`"<Array Name> (<NEPOOL-GIS ID>)"` per CLAUDE.md / `ReportsCard.tsx:203`), where
to find it (the operator's NEPOOL-GIS account / their existing roster), and that
marking it "optional" means the flagship report ships with a blank/ugly title.

**Why it matters:** It's described in the codebase as "the canonical field," yet
the UI frames it as optional and offers no sourcing guidance. A missing ID
degrades the deliverable the customer is paying for. New operators won't know
what string to paste.

**Severity:** High

**Recommended addition:** Helper text: "The array's NEPOOL-GIS registry ID (e.g.
NON12345). It appears in the report's sheet title and identifies the array to
NEPOOL — find it in your NEPOOL-GIS account or your existing tracking sheet." Drop
or soften "(optional)" given its importance to the output.

**Files involved:** `web/onboarding/src/screens/Clients.tsx:283-293`,
`web/app/src/components/ArrayList.tsx:169-176,472-479`,
`web/app/src/components/ImportSpreadsheetModal.tsx:230`

---

### Finding 3 — Pricing math never reconciles on screen

**Where it shows up:** Onboarding Welcome promises "$250 one-time setup ·
$45/array/month" (`web/onboarding/src/screens/Welcome.tsx:38-44`); checkout is
created from the Info screen with no array count yet
(`web/onboarding/src/screens/Info.tsx:34-46`); the dashboard Account summary shows
"Utility accounts" and "Bills on file" but **never an array count or a dollar
figure** (`web/app/src/components/AccountSummaryCard.tsx:127-128`).

**What's implicit:** How many arrays the operator is being billed for, where that
number comes from, when per-array billing starts (at checkout they have 0 arrays;
auto-populate adds them later), and how "utility accounts" / "bills on file"
relate to billable arrays (they don't directly — billing is per array, but arrays
aren't shown).

**Why it matters:** Money. A stamping agent reselling this can't sanity-check
their own invoice against the dashboard. The two counts shown are the two that
*don't* drive the bill. When auto-populate later adds arrays, the charge changes
with no on-screen acknowledgment of the new array count or cost.

**Severity:** High

**Recommended addition:** Show "Billable arrays: N · ~$45 × N = $X/mo" on the
Account summary, sourced from the same array count that drives Stripe, with a one-
line note that the figure updates as auto-populate adds arrays. At checkout, state
explicitly that the $45/array charge begins once arrays are detected.

**Files involved:** `web/app/src/components/AccountSummaryCard.tsx:89-134`,
`web/onboarding/src/screens/Welcome.tsx:37-44`,
`web/onboarding/src/screens/Info.tsx:34-55`

---

### Finding 4 — Auto-populate is an invisible background process

**Where it shows up:** Empty array state "Arrays appear here once GMP auto-populate
runs, or add one manually" (`web/app/src/components/ArrayList.tsx:82-86`);
AddClientModal "When this client logs into GMP through the extension, we'll add
their arrays automatically" (`web/app/src/components/AddClientModal.tsx:132-136`);
Done screen "Arrays from auto-populate clients will appear once they sign into GMP"
(`web/onboarding/src/screens/Done.tsx:68-73`).

**What's implicit:** When auto-populate runs, whether it has ever run, whether the
last GMP capture succeeded, and whether the array list the operator is looking at
is fresh or stale. The dashboard fetches client/array data once on mount
(`ClientsSection.tsx:31-47`, `ArrayList.tsx:30-46`) with no refresh affordance and
no "last synced" anywhere. Onboarding *does* surface live capture state
(`Extension.tsx:263-290`), but that signal vanishes once the operator reaches the
dashboard.

**Why it matters:** The operator can't distinguish "working, just waiting" from
"broken, extension never captured." This is exactly the second-guessing the
Mega-Vector north star says we must absorb. Likely the top post-onboarding support
question ("where are my arrays?").

**Severity:** High

**Recommended addition:** Per-client (or account-level) "Last GMP capture: <date>"
or "No captures yet — make sure the extension is active and this client has signed
into GMP" indicator, plus a manual "Refresh" on the clients list. Reuse the
capture-status concept from `Extension.tsx`.

**Files involved:** `web/app/src/components/ArrayList.tsx:82-86`,
`web/app/src/components/AddClientModal.tsx:132-136`,
`web/app/src/components/ClientsSection.tsx:31-47`,
`web/onboarding/src/screens/Done.tsx:68-73`

---

### Finding 5 — Delete-array (final) vs deactivate-client (reversible): opposite semantics, no signpost

**Where it shows up:** ClientCard "Deactivate client" is a soft-delete, kept
visible as "inactive," reactivatable, modal copy explicitly says "nothing is
deleted" (`web/app/src/components/ClientCard.tsx:41-56,166-213`). One screen
deeper, ArrayList "Delete array" is permanent: modal says "also removes its N
linked utility accounts and any bills tied to them. This can't be undone."
(`web/app/src/components/ArrayList.tsx:236-266`).

**What's implicit:** That reversibility flips between two adjacent, visually
similar destructive actions. An operator who learns "deactivating a client is
safe and undoable" will reasonably assume deleting an array is too. It is not —
it cascades to utility accounts and bills permanently.

**Why it matters:** Irreversible data loss (bills are the raw material of every
future report). The asymmetry is a trap precisely because the safer action is the
more prominent/encountered one.

**Severity:** High

**Recommended addition:** Either make array delete a soft-delete to match clients,
or harden the warning — e.g. require typing the array name, and label the button
"Permanently delete." At minimum, mirror the client modal's reassurance pattern in
reverse: lead with "This is permanent and is **not** like deactivating a client."

**Files involved:** `web/app/src/components/ArrayList.tsx:236-266`,
`web/app/src/components/ClientCard.tsx:166-213`

---

### Finding 6 — Subscription status badge has no legend

**Where it shows up:** Account summary status badge — active / trialing / comped /
past_due / canceled / pending (`web/app/src/components/AccountSummaryCard.tsx:16-35`).

**What's implicit:** What each state means for the operator. "comped" (violet) is
internal billing jargon — a paying customer would have no idea why they're
"comped." "trialing" vs "active" billing implications, "past_due" urgency, and
what "pending" blocks are all unexplained. The badge is color + capitalized word
only.

**Why it matters:** "past_due" especially carries an action (fix your card) the
badge doesn't convey — the dedicated email exists (`notify.py:285-315`) but the
dashboard badge is silent. "comped" leaks an internal concept to customers.

**Severity:** Medium

**Recommended addition:** Tooltip or one-line caption per state, and for
`past_due` a visible "Update payment → Manage billing" prompt next to the badge.
Rename or hide "comped" from the customer's view (e.g. "Complimentary").

**Files involved:** `web/app/src/components/AccountSummaryCard.tsx:16-35`

---

### Finding 7 — "Utility accounts" and "Bills on file" counts are unexplained

**Where it shows up:** Account summary fields (`web/app/src/components/AccountSummaryCard.tsx:127-128`).

**What's implicit:** What a "utility account" is vs an array vs a client, and what
"bills on file" implies about report readiness (e.g. need ~6 quarters for a full
report). These are presented as bare numbers with no context for whether they're
"enough."

**Why it matters:** They're the only quantitative feedback on the main screen, yet
they don't map to anything the operator manages (clients/arrays) or pays for
(arrays). Low actionability, mild confusion, and they crowd out the array count
that actually matters (see Finding 3).

**Severity:** Medium

**Recommended addition:** Add a hint such as "Bills on file: 14 (a full report
needs ~18 — the last 6 quarters)" and a short tooltip defining "utility account"
as the GMP account that bills an array.

**Files involved:** `web/app/src/components/AccountSummaryCard.tsx:104-133`

---

### Finding 8 — Two overlapping "copy me" mechanisms (cc_on_reports vs send_mode = to_me)

**Where it shows up:** ReportsCard toggle "Send me a copy of every report"
(`web/app/src/components/ReportsCard.tsx:155-168`, `cc_on_reports`) and
EmailCustomizationCard send-mode "To me only (I forward)"
(`web/app/src/components/EmailCustomizationCard.tsx:16-19,184-219`, `send_mode`).
Resolution lives in `api/delivery.py:73,91-94,149-154`.

**What's implicit:** That these two settings interact and partly contradict. With
`send_mode = to_me`, clients and client CCs are **not** contacted at all
(`delivery.py:91-94`) — but the operator set CC emails on clients expecting them to
fire. Meanwhile `cc_on_reports` is a separate "also BCC me" path. Nothing on
either screen references the other.

**Why it matters:** An operator can believe their client is receiving reports
while `to_me` is silently intercepting everything, or double-configure both and
not know which wins. Client-level CC emails being silently skipped under `to_me`
is a real surprise.

**Severity:** Medium

**Recommended addition:** Cross-reference the two settings in their helper text,
and under `to_me` explicitly warn "Your clients (and their CC addresses) will not
be emailed — only you." Consider collapsing into one "Who receives reports" control.

**Files involved:** `web/app/src/components/ReportsCard.tsx:155-168`,
`web/app/src/components/EmailCustomizationCard.tsx:184-219`,
`api/delivery.py:73,91-99,149-154`

---

### Finding 9 — Changing report cadence has unstated timing side effects

**Where it shows up:** Cadence `<select>` on Account summary
(`web/app/src/components/AccountSummaryCard.tsx:113-126`) and the segmented
schedule control on ReportsCard (`web/app/src/components/ReportsCard.tsx:114-146`).

**What's implicit:** When the *next* report fires after a change. Switching
quarterly → weekly: does a report go out now, at the next week boundary, or stay
on the old anchor? Switching mid-quarter: is the in-flight quarter affected? The
toast just says "Reports now send weekly" with no "next send" date. (The good
news: ReportsCard already clarifies that *manual* sends don't alter the schedule,
`ReportsCard.tsx:173-176` — but the cadence change itself is unexplained.)

**Why it matters:** The operator can't predict whether changing cadence triggers an
immediate client-facing email — a real risk if they're experimenting.

**Severity:** Medium

**Recommended addition:** After a cadence change, surface "Next automatic report:
<date>" (sourced from the scheduler) and confirm whether the change takes effect
immediately or next cycle.

**Files involved:** `web/app/src/components/AccountSummaryCard.tsx:113-126`,
`web/app/src/components/ReportsCard.tsx:42-60`, `api/scheduler.py`

---

### Finding 10 — Import spreadsheet doesn't disclose AI parsing or where the file goes

**Where it shows up:** ImportSpreadsheetModal upload copy "Drop your roster... We'll
read it and let you review everything before anything is saved."
(`web/app/src/components/ImportSpreadsheetModal.tsx:156-159`, "Parsing your
spreadsheet…" at 211-216).

**What's implicit:** That an LLM extracts the fields (per Mega-Vector V4), that the
file's contents leave the browser to a parsing service, and what happens to the
uploaded file afterward. A privacy-conscious accountant uploading a full client
roster (names, account numbers) gets no statement about data handling.

**Why it matters:** Trust + privacy. Uploading a client list to "we'll read it"
without disclosing AI/third-party processing is exactly the kind of thing a
stamping agent's own compliance obligations care about.

**Severity:** Medium

**Recommended addition:** One line under the dropzone: "We use automated extraction
to read the file; it's processed to build the preview and not shared. Nothing is
saved until you confirm." Link to the privacy policy.

**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:154-209`

---

### Finding 11 — Import: "Unassigned" silently merges orphan rows into one client

**Where it shows up:** Import preview groups by operator; the count shown is
`new Set(operator_name || "Unassigned")` (`web/app/src/components/ImportSpreadsheetModal.tsx:108-111`),
surfaced in the commit button "...under N clients"
(`web/app/src/components/ImportSpreadsheetModal.tsx:305`).

**What's implicit:** That every row missing an operator name is bucketed together
into a single client literally named "Unassigned," and that the client count in
the button is derived from distinct operator names (so blanks collapse to one).

**Why it matters:** An operator importing a messy sheet can unknowingly create a
junk "Unassigned" client holding unrelated arrays, then wonder why reports group
oddly. The button's "N clients" number won't match their mental model.

**Severity:** Medium

**Recommended addition:** Flag blank-operator rows in the preview ("will be grouped
under 'Unassigned'") and/or block commit until they're named, rather than silently
bucketing.

**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:108-111,253-257,305`

---

### Finding 12 — "HTML supported" assumes the operator writes HTML; typos render verbatim to clients

**Where it shows up:** Email body textarea helper "HTML supported. Use
{{client_name}}..." (`web/app/src/components/EmailCustomizationCard.tsx:178-181`);
merge logic leaves unknown/typo'd tags verbatim by design
(`api/email_templates.py:50-67`).

**What's implicit:** (a) That a stamping agent — "accountants at heart" per Mega-
Vector — is expected to author HTML, and that raw `<` or `&` typed as text can
break rendering. (b) That a misspelled merge tag like `{{Client_name}}` will appear
*literally* in the client's email instead of being substituted or flagged. The
preview helps, but only if the operator runs it.

**Why it matters:** A broken or `{{typo}}`-laden email goes out under the operator's
professional name to *their* client — the exact reputational surface V2 exists to
protect.

**Severity:** Medium

**Recommended addition:** Note "Tip: a misspelled tag like {{Client_name}} will
appear exactly as typed — use the buttons/list above." Consider validating tags
against the known set on save and warning on unknowns; and nudge "Preview before
saving" more strongly.

**Files involved:** `web/app/src/components/EmailCustomizationCard.tsx:163-181`,
`api/email_templates.py:23-67`

---

### Finding 13 — Activation code never says "keep this secret"

**Where it shows up:** Onboarding Extension screen (`web/onboarding/src/screens/Extension.tsx:197-222`),
dashboard ActivationCodeCard (`web/app/src/components/ActivationCodeCard.tsx`),
welcome email (`api/notify.py:133-148`).

**What's implicit:** That the activation code (`tenant_key`) is a bearer credential
— anyone holding it can post captures to the operator's account
(`Extension.tsx:44-46` comment confirms it authenticates `/v1/sync`). The UI frames
it purely as a setup convenience ("links the extension to your account") with no
sensitivity guidance.

**Why it matters:** Security. An operator might paste it into a shared doc or
screenshot it in a support thread, not realizing it's a secret. Low likelihood,
real blast radius.

**Severity:** Medium

**Recommended addition:** One line: "Treat this like a password — anyone with it
can send data to your account. Don't share it." Offer a regenerate option for if
it leaks.

**Files involved:** `web/app/src/components/ActivationCodeCard.tsx:12-46`,
`web/onboarding/src/screens/Extension.tsx:197-222`, `api/notify.py:133-148`

---

### Finding 14 — GMP login field: whose login, and does it *do* anything?

**Where it shows up:** "GMP login (email or username)" in onboarding Clients
(`web/onboarding/src/screens/Clients.tsx:247-258`), AddClientModal
(`web/app/src/components/AddClientModal.tsx:123-137`), and ClientCard
(`web/app/src/components/ClientCard.tsx:132-152`).

**What's implicit:** Whether this is the *client's* GMP login or the operator's,
and whether entering it triggers anything (it does not log in or scrape — capture
happens when *someone* signs into GMP with the extension active; this field appears
to be a matching/identity hint). The helper text varies by screen: AddClientModal
says "When this client logs into GMP through the extension..." (good), onboarding
just says "Use whichever you log into GMP with" (ambiguous about *whose* login),
and ClientCard offers no helper at all.

**Why it matters:** Operators may think typing the login here authorizes Solar
Operator to fetch bills (it doesn't), or may enter their own credentials. Sets a
false expectation about how capture is triggered — feeds the same "why no arrays?"
confusion as Finding 4.

**Severity:** Medium

**Recommended addition:** Standardize one explanation across all three screens:
"The email/username this client uses to sign into GMP. We use it to match incoming
bill captures to this client — we never log in for them; capture happens when they
sign into GMP with the extension installed."

**Files involved:** `web/onboarding/src/screens/Clients.tsx:247-258`,
`web/app/src/components/AddClientModal.tsx:123-137`,
`web/app/src/components/ClientCard.tsx:132-152`

---

### Finding 15 — "Merge sub-meters by hand in the dashboard" points at a feature that isn't labeled as one

**Where it shows up:** Onboarding sub-meter warning "those will come in as separate
arrays. You'll need to merge them by hand in the dashboard after onboarding."
(`web/onboarding/src/screens/Clients.tsx:187-198`). The actual mechanism is linking
multiple utility accounts to one array (`web/app/src/components/ArrayList.tsx:271-346`,
"+ Link a utility account").

**What's implicit:** That "merge by hand" == delete the duplicate auto-created
arrays and link their GMP accounts under one array via "Link a utility account."
There is no "merge" button or guide; the operator must infer the entire workflow
from a one-line onboarding warning seen days earlier.

**Why it matters:** This is the Starlake case the business explicitly cares about
(3 sub-meters → 1 array). The instruction names an action the UI doesn't provide
by that name, so the operator is stranded. Trust + support load.

**Severity:** High

**Recommended addition:** Either add a real "merge arrays" affordance, or rewrite
the warning to the actual steps ("In the dashboard, open one array, use 'Link a
utility account' to attach the other GMP accounts, then delete the duplicate
arrays") and link to a short guide.

**Files involved:** `web/onboarding/src/screens/Clients.tsx:187-198`,
`web/app/src/components/ArrayList.tsx:271-346`

---

### Finding 16 — "Reset to defaults" doesn't say what it resets (and quietly keeps send mode)

**Where it shows up:** EmailCustomizationCard "Reset to defaults" link
(`web/app/src/components/EmailCustomizationCard.tsx:102-119,248-255`).

**What's implicit:** That it clears exactly four fields (from-email, from-name,
subject, body) but deliberately **preserves** send_mode
(`EmailCustomizationCard.tsx:102-119`). The operator can't tell which settings will
revert and which survive.

**Why it matters:** An operator wanting a clean slate (including send_mode) won't
get one and won't know why. Low blast radius but quietly surprising.

**Severity:** Polish

**Recommended addition:** Label or confirm: "Reset subject, body, and sender to the
Solar Operator defaults? Your send mode stays as-is."

**Files involved:** `web/app/src/components/EmailCustomizationCard.tsx:102-119,248-255`

---

### Finding 17 — "Last report sent" is account-wide but reads as per-everything

**Where it shows up:** Account summary "Last report sent"
(`web/app/src/components/AccountSummaryCard.tsx:129-133`) and ReportsCard "Last
sent: <date>" (`web/app/src/components/ReportsCard.tsx:148-153`), both from
`tenant.last_delivery_at`, which `api/delivery.py:156-164` stamps on *any* client
delivery.

**What's implicit:** That this single date reflects the most recent delivery to
*any one* client, not that every client received a report on that date. With
multiple clients on different states (inactive, no email on file), per-client
reality can differ.

**Why it matters:** An operator may assume "Last report sent: June 1" means all
clients are current, when one client with no contact email was silently skipped
(`delivery.py:97-99`).

**Severity:** Medium

**Recommended addition:** Per-client "last sent" on each ClientCard, and/or relabel
the account figure "Most recent delivery" with a note that it reflects the latest
of any client.

**Files involved:** `web/app/src/components/AccountSummaryCard.tsx:129-133`,
`web/app/src/components/ReportsCard.tsx:148-153`, `api/delivery.py:97-99,156-164`

---

### Finding 18 — Silent fallback: custom "send from" can be overridden without telling the operator

**Where it shows up:** Helper text "Custom domains must be verified, or we fall
back to the default automatically" (`web/app/src/components/EmailCustomizationCard.tsx:139-142`);
the actual runtime fallback retries from the platform address and only *logs* it
(`api/notify.py:100-109`).

**What's implicit:** That a report the operator believes went out "as them" may
have actually been sent from `admin@solaroperator.org` (with their address as
Reply-To). The dashboard never reports that this happened on any given send — it's
log-only, server-side.

**Why it matters:** The whole point of "send as me" (V2, "trust-builder #1") is the
operator's name on the email. Silently falling back undermines exactly that, and
the operator has no on-screen signal it occurred.

**Severity:** Medium

**Recommended addition:** Surface verification status of the custom domain in the
UI (verified / not verified → "we'll fall back"), and ideally a per-send indicator
or notification when a fallback actually happened.

**Files involved:** `web/app/src/components/EmailCustomizationCard.tsx:131-142`,
`api/notify.py:79-109`

---

### Finding 19 — Extension capture dot has no persistent legend

**Where it shows up:** Onboarding Extension status dot — amber pulse = waiting,
green = received (`web/onboarding/src/screens/Extension.tsx:263-290`).

**What's implicit:** The color→meaning mapping. It's reasonably mitigated by the
adjacent `aria-live` text ("We're waiting for your first GMP capture…" /
"Capture received…"), so this is mild — but the same dot pattern doesn't recur in
the dashboard with any label (see Finding 4).

**Why it matters:** Minor on its own; the text rescue makes it mostly fine. Noted
for consistency since capture status disappears entirely post-onboarding.

**Severity:** Polish

**Recommended addition:** Keep the text label pattern wherever the dot appears, and
reuse it in the dashboard capture indicator proposed in Finding 4.

**Files involved:** `web/onboarding/src/screens/Extension.tsx:263-290`

---

### Finding 20 — No in-app statement of where data lives or who can see it

**Where it shows up:** Privacy/TOS exist only as a collapsible on the onboarding
Welcome screen (`web/onboarding/src/screens/Welcome.tsx:60-92`). The dashboard has
no data/privacy surface; the footer is just "support@solaroperator.org"
(`web/app/src/screens/DashboardLayout.tsx:74-76`).

**What's implicit:** Where GMP bills and client data are stored, who at Solar
Operator can access them, and who to ask. Once past onboarding the operator has no
in-app answer to "is my clients' data safe here?"

**Why it matters:** Trust, especially for an agent who is themselves a custodian of
their clients' data. The competition is a human consultant the operator already
trusts; "where does my data go" being unanswerable in-app is a soft churn risk.

**Severity:** Polish

**Recommended addition:** A small "Privacy & data" link in the dashboard footer or
Account tab pointing to a plain-language page (storage location, access, deletion,
contact).

**Files involved:** `web/app/src/screens/DashboardLayout.tsx:74-76`,
`web/onboarding/src/screens/Welcome.tsx:60-92`

---

### Finding 21 — "[copy]" subject prefix on CC/copy emails is unexplained to recipients and operator

**Where it shows up:** CC recipients and the "copy me" path get a `[copy]` subject
prefix (`api/delivery.py:139-154`); the operator-facing toggle copy says only
"You'll receive an identical email to whatever each client gets"
(`web/app/src/components/ReportsCard.tsx:164-167`).

**What's implicit:** That the operator's copy (and any client CC) arrives with a
`[copy]` subject prefix that the primary recipient's email lacks — so the operator
QA-ing their own inbox sees a different subject than the client received, and
client CC addresses get `[copy]` too.

**Why it matters:** Minor confusion during QA ("why does my subject say [copy]?")
and a slightly odd artifact landing in a client's CC inbox under the operator's
professional name.

**Severity:** Polish

**Recommended addition:** Mention in the toggle helper that copies arrive with a
"[copy]" subject prefix; reconsider applying that prefix to client-facing CC
addresses.

**Files involved:** `api/delivery.py:139-154`,
`web/app/src/components/ReportsCard.tsx:155-168`

---

## Notes on what's already good (to avoid regressing it)
- Onboarding Extension screen explains the activation code's *purpose* and the
  log-into-GMP step well, with a live capture indicator and a troubleshooting
  modal (`Extension.tsx:197-261,326-377`).
- Client *deactivation* copy is exemplary — explicitly reassures "nothing is
  deleted" (`ClientCard.tsx:209-213`). The fix for Finding 5 is to make array
  delete as clear, not to dilute this.
- Manual "Send a report now" already clarifies it doesn't change the schedule
  (`ReportsCard.tsx:173-176`) and confirms the audience in its modal.
- Email merge-tag help is present on both subject and body
  (`EmailCustomizationCard.tsx:160,178-181`); the gap is typo behavior (Finding 12),
  not the tag list itself.
</content>
</invoke>
