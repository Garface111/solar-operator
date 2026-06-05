# Solar Operator — Mega-Vector Strategy (Jun 3 2026)

## North Star
**From a working tool to a trusted service.** The product currently *works*. It does not yet *feel cared-for*. Every customer ask reduces to: the operator had to figure something out, second-guess, or do grunt work the software should have absorbed.

Competition isn't other SaaS. It's the human consultant who answered the phone, knew the operator's name, and reassured them their report went out clean. We have to feel like that human — but faster and cheaper.

## Operating Principle
**Every screen anticipates what the operator is about to wonder.** If they could plausibly ask "wait, what?", we lost.

## Customer Model (CORRECTED)
- **Tenant** (the customer who pays) = a NEPOOL-GIS reporting consultant / stamping agent who serves multiple solar operators
- **Client** (managed by tenant) = the actual solar operator like Bruce Genereaux, who runs the arrays
- **Array** (owned by client) = a physical solar installation
- **UtilityAccount** (linked to array) = the GMP/etc account that bills it

Pricing: $250 setup + $15/array/month. The tenant's value prop: replace the manual quarterly grind across all their operator clients.

## Visual Identity Shift
**Old:** Stripe/Linear minimalism (correct but soulless)
**New:** Solarpunk-pastoral — Studio Ghibli airships, lavender/canola fields, warm cream whites (#F4F1E4), painterly soft edges. Stays professional via typography discipline + clean information hierarchy. Reference image at `docs/solarpunk-reference.jpg`.

**Approved palette additions:**
- Cream white `#F4F1E4` (instead of pure `#FFFFFF`)
- Warm wood accent `#8B6F47`
- Soft lavender field `#8B7BB8`
- Canola yellow `#E8C547`
- Keep emerald `#10b981` as primary CTA color
- Sky gradient `#3FA8D8` → `#1E6FA8` for hero backgrounds

Typography: keep Inter (don't reach for a rounded humanist that loses professionalism). Use weight + spacing for warmth instead.

## Priority Vectors (each is a feature request expressing the same underlying complaint)

### V1 — Quarterly default
Operators report quarterly because NEPOOL wants quarterly. Monthly default is engineer-default, not operator-default. **5-min fix, do first.**

### V2 — Email preview, customization, "send as me", "send only to me"
Tenant wants control over the communication that goes out under their professional name. Trust-builder #1.
- Preview email template before any send
- Customize subject + body (with merge tags: `{{client_name}}`, `{{quarter}}`, etc.)
- Toggle: "Send from my email address" (configurable from address per tenant)
- Toggle: "Send to me only (not the client) — I'll forward" (for tenants who want full control)

### V3 — GMP activation guidance
Modal/walkthrough on the Extension screen: "Now log into your GMP account in any tab. We'll detect your bills automatically. You don't need to do anything else." Plus a "Test connection" affordance so the tenant doesn't sit there wondering.

### V4 — AI spreadsheet ingest for NEPOOL IDs
Tenant drops their existing tracking spreadsheet (a master roster of operators, arrays, NEPOOL-GIS IDs). LLM extracts (operator name → Client, array name → Array, NEPOOL ID → Array.nepool_gis_id, GMP account # → UtilityAccount). Tenant confirms a preview table. One-click commit. **Collapses setup from 2 hours to 5 minutes.**

### V5 — Solarpunk landing
Reference image: `docs/solarpunk-reference.jpg`. Studio Ghibli pastoral airships, lavender/canola fields, painterly. SVG illustration in hero, drifting airships, layered parallax. Stays B2B-credible via typography + content discipline.

### V6 — End-to-end journey map
Mermaid flowchart in `docs/`. Every transition between screens, every email trigger, every error path. Audit for smoothness before more features land.

## What This Re-Orders
1. **Quarterly default** — first, unblocks everything else
2. **Email preview + customization + send-as + send-to-me** — biggest trust win
3. **GMP activation modal + test connection** — kills the #1 onboarding bounce point
4. **AI spreadsheet ingest** — 10x onboarding speed for the actual buyer
5. **Solarpunk landing** — brand soul
6. **Journey map audit** — system-level discipline before piling more features

## Anti-Goals
- Don't reach for "fun" at the cost of professionalism. Stamping agents are accountants at heart. Solarpunk via *imagery and warmth*, not *emoji and bouncing animations*.
- Don't add settings nobody will touch. Every toggle is a maintenance debt.
- Don't AI-pile. The spreadsheet ingest must be ONE clean feature, not a chatbot, not a copilot.
