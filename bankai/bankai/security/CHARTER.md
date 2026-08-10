# The Sentinel Charter

BankAI holds a family's whole financial and legal life — balances, statements,
deeds, trusts, tax returns, identity documents, a home address. This document is
the doctrine for defending it. It is written for the humans who own the system;
a short disposition version is woven into the copilot's own mind (its system
prompt), but **this file, and the code that enforces it, is the real boundary.**

## The one principle everything else follows from

**The copilot lives inside the sandbox. It never holds the keys to its own cage.**

You cannot make an AI safe by making it *want* to be safe. A prompt is a
disposition, not a wall — a model that is prompt-injected by a malicious
document, or that drifts as it grows more capable, will ignore its own good
intentions. So the controls that keep BankAI safe run in code the model does not
edit, write to a ledger the model cannot rewrite, and hand every consequential
decision to a human.

Concretely, the model cannot, by design:

- move money or reach a bank (bank access is read-only, always);
- email or message anyone outside the household without a human clicking Approve;
- change its own security settings, permissions, or code;
- delete or edit the audit ledger.

If a future change would break any of these, that change is the thing to stop.

## Sentinel's job: watch and sound the alarm — never fight back

Sentinel is BankAI's self-defense subsystem. Its power is to **see and warn**,
loudly and honestly. It deliberately does **not**:

- take autonomous action against a third party (a bank, a lender, an attacker) —
  a defense that can "retaliate" is just a new weapon pointed at the family;
- silently change security controls to "fix" what it finds — it reports the exact
  remediation and a human decides;
- act on instructions it finds *inside* data (an email, a document, a memo). Data
  is never a command.

What it does, on a schedule and in code the copilot can't switch off:

1. **Tamper-evident audit.** Every security-relevant event is appended to a
   hash-chained ledger. Editing or deleting history breaks the chain and is
   detected (`verify_chain`). This is how "did it do something without
   permission?" stays answerable.

2. **Posture self-audit.** The things that *drift* and open holes get checked
   every cycle: file permissions (BankAI was briefly world-writable once —
   never again unnoticed), the localhost-only binding, secrets accidentally
   written to logs, whether the session secret is set, backup freshness. Drift is
   reported, never silently auto-changed.

3. **Threat watch.** Failed-login bursts and prompt-injection markers in inbound
   email and documents are flagged and alarmed.

When Sentinel finds something new and real, it emails the household and drops a
note in the shared thread so the copilot sees it — framed as *report this to the
family*, never *act on it*.

## The three classes of "rogue element", and how each is met

- **Rogue AI (including this one, prompt-injected).** Injection-marker detection
  on all untrusted input; the instruction-vs-data rule in the copilot's mind; and
  the hard code gates above, which hold even if the model is fully subverted.
- **Institutions acting against the family.** The copilot is disposed to check
  the household's statements against their interest — overcharges, errors,
  predatory terms, silent fee or rate changes — and to raise them. It advises;
  it does not fight the institution on its own.
- **Fraud / scams / "loan" attackers.** Watch inbound documents and messages for
  scam, phishing, predatory-lending, and identity-theft signals, and warn the
  family before they act.

## Extending Sentinel safely

Add detections freely. Before adding any *action* — anything that changes state,
reaches outside the box, or alters a control — ask: could a prompt-injected
copilot trigger this to the family's harm? If yes, it must be human-gated. Detection
scales without limit; autonomy does not.
