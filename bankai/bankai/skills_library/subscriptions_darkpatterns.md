WHEN TO USE: Cancelling any subscription, membership, free trial, or auto-renewing service — especially when the company hides, delays, or refuses the cancellation.
Also use when auditing recurring charges, when a "cancelled" service bills again, or when deciding whether to escalate to a chargeback.

# Subscriptions and dark patterns

## The economics you are fighting

Subscription businesses model revenue on *involuntary retention*: the friction
between wanting to cancel and completing the cancellation. Every obstacle below
is a deliberate, measured design choice with a conversion rate attached. Treat
cancellation as an adversarial process with a paper trail, not a chore.

## Before you cancel — 5 minutes of prep

1. **Find the real billing entity.** The card descriptor is often a payment
   processor, not the brand ("DRI*BRAND", "PADDLE.NET"). Search transactions for
   the descriptor and for the brand name separately.
2. **Note the renewal date.** Cancel at least 3–5 business days before it; many
   companies process cancellations "at the end of the current period" but bill on
   a batch cycle that has already been queued.
3. **Check where the subscription actually lives.** If purchased in-app, the
   merchant of record is **Apple** (Settings → Apple Account → Subscriptions) or
   **Google Play** (Play Store → Subscriptions), and the company genuinely
   cannot cancel it. Same for Amazon Channels, Roku, and PayPal billing
   agreements (PayPal → Automatic Payments). Cancelling with the brand alone
   leaves the billing alive — this is the #1 "I cancelled and got charged" cause.
4. **Screenshot everything**: the current plan, the price, the renewal date, and
   every screen of the cancellation flow as you go. Screenshots are what win
   chargebacks.
5. **Decide what you'll accept.** Retention will offer a discount. If a 40%
   discount would keep the household happy, that is a legitimate outcome — but
   decide *before* the offer lands, because the offer is engineered to be
   accepted in the moment.

## Pattern-by-pattern tactics

**The retention maze** (10 screens, "are you sure", surveys, "your data will be
deleted"). Answer nothing you don't have to; there is no requirement to give a
reason. Take the shortest path — pick the first option on each screen that keeps
moving. Screenshot the final confirmation screen *and* wait for the confirmation
email. **No email = not cancelled.**

**Phone-only cancellation** (sign-up online, cancellation by call). This
asymmetry is precisely what ROSCA and state auto-renewal laws target, and it is
usually the weakest legal ground the company stands on. Tactics: call at opening
time; say only "I'm calling to cancel my account, effective today"; repeat the
same sentence verbatim to each counter-offer ("I understand — I'd still like to
cancel today"); do not explain, argue, or justify — explanation is the surface
the retention script attaches to. Get a **cancellation confirmation number** and
the rep's name before you hang up, then email them requesting written
confirmation. If the hold is theatrical, ask for a callback and note the time.

**Chat-only or hidden-link cancellation.** Chat is *better* than phone — the
transcript is evidence. Always click "email me a transcript" at the end. If no
transcript option exists, screenshot the full scroll.

**Negative-option / auto-renewal billing** (free trial that silently converts,
annual plan that renews without a bill). Federal law here is **ROSCA** (15
U.S.C. §8403): clear and conspicuous disclosure of material terms, express
informed consent before charging, and a **simple mechanism to stop recurring
charges**. The FTC's 2024 "click-to-cancel" Negative Option Rule — which would
have required cancellation to be at least as easy as sign-up — was **vacated by
the Eighth Circuit in 2025 on procedural grounds**, so *verify the current
federal posture before citing it as binding*. State law is the stronger, live
lever: **California's Automatic Renewal Law (Bus. & Prof. Code §17600 et seq.)**
requires conspicuous disclosure, affirmative consent, an online cancellation path
for online sign-ups, and renewal reminders for long/free-trial terms — with
amendments tightening it further; **New York GBL §527-a**, **Illinois**,
**Colorado**, **Virginia**, and a growing list have analogues. Several impose a
powerful remedy: goods or services delivered in violation are deemed an
**unconditional gift**. Name the statute of the household's state in writing —
companies route statute-citing messages to a compliance queue.

**"Pause instead of cancel."** A pause is retention, not cancellation. Accept it
only deliberately, with a calendared reminder before it un-pauses. Same for
price increases buried in a "we're updating our terms" email — audit annually.

**Zombie charges after cancellation.** Send one firm written demand (below), then
revoke authorization at the bank under **Reg E** (stop payment on a preauthorized
transfer, at least 3 business days before the scheduled date) and dispute the
charge. Do **not** rely on cancelling the card — network account-updater services
push new card numbers to merchants automatically.

## Scripts

**Phone, opening line (say exactly this, then stop talking):**
> "Hi — I'd like to cancel my account effective today. Account is under
> [name], [email/account number]. Can you process that and give me a
> cancellation confirmation number?"

**Every counter-offer, same reply:**
> "I appreciate the offer, but I'd still like to cancel today. Can you confirm
> the cancellation is processed?"

**Close:**
> "Thank you. Can you confirm: the cancellation is effective today, no further
> charges will be made, and you're sending written confirmation to [email]?
> What's your name and the confirmation number?"

**Written cancellation / demand email** (also the template for the
`propose_action` tool):

> Subject: Cancellation of account [ACCOUNT #] — written notice, effective [DATE]
>
> I am cancelling my [service] subscription, account [number], billed to
> [last 4 of card], effective immediately. This message is written notice of
> cancellation and of revocation of authorization for any further recurring
> charges to my payment method.
>
> Please confirm in writing within 5 business days: (1) the cancellation date,
> (2) that no further charges will be made, and (3) the refund of any amount
> billed after this notice.
>
> [If charged after cancelling:] I was charged $X on [date] after cancelling on
> [date] (confirmation [#], transcript attached). Please refund it.
>
> [If phone-only:] Your service does not provide an online cancellation path
> although I signed up online. I am relying on ROSCA (15 U.S.C. §8403) and
> [STATE]'s automatic renewal statute. Please treat this email as effective
> notice regardless of your usual channel.
>
> If I do not receive written confirmation within 5 business days, I will
> dispute the charges with my card issuer and file complaints with the FTC and
> my state attorney general.

Send from the account's email address, keep the sent copy, and save it to the
vault.

## Chargeback as last resort — and how to do it right

Escalate to the card issuer only after (a) a documented cancellation attempt and
(b) a written demand that went unanswered. Then:

- Dispute reason: **"cancelled recurring transaction"** or "services not
  received" — not "fraud". Calling a legitimate merchant's charge fraud can get
  your card reissued and muddies the record.
- Evidence packet: cancellation confirmation or screenshots of the flow, the
  chat transcript, the demand email with timestamps, and the statement line.
- Window: card networks generally allow **120 days** from the transaction or the
  expected service date; for a credit card also send the written **FCBA billing
  error notice** within 60 days of the statement to get the statutory protections
  (see `consumer_protection`).
- Expect consequences: the merchant will usually terminate the account and may
  bar future signups. Fine for a subscription; think twice for an airline,
  utility, or anything with an ongoing relationship.
- Then **block future charges**: ask the issuer to place a merchant block, and
  for debit/ACH, submit a written stop-payment and revocation to the bank.

## Standing audit rhythm

Run `subscription_audit` quarterly and sort by annualized cost, not monthly
price — a $14/month service is a $168/year decision. Flag: charges with no
matching login, annual renewals landing in the next 60 days, duplicate streaming
services, free trials converting, and anything whose price rose since last audit.
Bank data cannot show usage — **always ask the household before judging a
subscription idle**, propose one cancellation at a time, and let a human approve
the action.
