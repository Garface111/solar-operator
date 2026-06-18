# Liquid-Fill Inverter Card — Integration Spec

**For:** whoever is building the per-inverter fleet card UI (the "Inverter N / sparkline / OUTPUT NOW % / All good / SolarEdge" cards).
**From:** the design exploration on the liquid-energy visualization.
**Goal:** add a bubbling green "liquid energy" fill to each inverter card WITHOUT changing your layout, and WITHOUT washing out any text or the sparkline.

Reference mockup: `sketches/liquid-cards/HYBRID-liquid-plus-sparkline.html` (open in a browser to see it live).

---

## TL;DR — what changes on the card

1. Add ONE liquid layer as the card's bottom background (z-index 1).
2. Wrap your existing card content (name, sparkline, OUTPUT NOW %, of-max, status pill, vendor tag) in a "frosted plate" that sits ABOVE the liquid (z-index 3). This is what guarantees text/graph never wash out.
3. The liquid HEIGHT = capacity factor = `current_output_w / max_output_w` (the same number behind your OUTPUT NOW %). **The liquid replaces the thin horizontal progress bar** — it shows the same data with far more presence.
4. Keep everything else exactly as you have it. Sparkline stays. Numbers stay. Pills stay.

The card must be `position: relative; overflow: hidden` so the liquid clips to the rounded corners.

---

## The ONE data field you need

You already compute the OUTPUT NOW percentage. The liquid needs the same 0..1 fraction:

```ts
// cf = capacity factor, 0..1. Same value your "55%" comes from.
const cf = Math.min(1, Math.max(0, currentW / maxW));   // maxW = nameplate or session-peak
```

That's it. No new backend field — `inverter_fleet.py` already exposes per-inverter live power + nameplate_kw.

---

## Drop-in React component

Paste this next to your card component. It renders ONLY the liquid layer — you keep your own plate/content.

```tsx
// LiquidFill.tsx — the bubbling green energy layer. Renders behind card content.
import { useMemo } from "react";

interface LiquidFillProps {
  /** 0..1 capacity factor (current output / max). Drives fill height. */
  fraction: number;
  /** Visual state. "sleep" = night/sun-down: calm indigo resting pool, no bubbles. */
  state?: "ok" | "low" | "clip" | "fault" | "sleep";
}

export function LiquidFill({ fraction, state = "ok" }: LiquidFillProps) {
  // When sleeping, ignore the (zero) fraction and show a low resting pool.
  const sleeping = state === "sleep";
  const pct = sleeping ? 14 : Math.min(100, Math.max(0, fraction * 100));
  // bubble count scales with fill; none when faulted/sleeping/idle. Memoized so
  // bubbles don't re-randomize on every poll re-render (would cause flicker).
  const bubbles = useMemo(() => {
    if (state === "fault" || sleeping || pct <= 0) return [];
    const n = Math.min(6, 3 + Math.round(fraction * 4));
    return Array.from({ length: n }, () => ({
      size: 4 + Math.random() * 7,
      left: 8 + Math.random() * 84,
      dur: 3 + Math.random() * 2.4,
      delay: Math.random() * 3,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, Math.round(pct / 5)]); // re-roll only on ~5% buckets, not every watt

  return (
    <div
      className={`liquid liquid--${state}`}
      style={{ height: `${pct}%` }}
      aria-hidden="true"
    >
      <div className="liquid__bubbles">
        {bubbles.map((b, i) => (
          <span
            key={i}
            style={{
              width: b.size, height: b.size, left: `${b.left}%`,
              animationDuration: `${b.dur}s`, animationDelay: `${b.delay}s`,
            }}
          />
        ))}
      </div>
    </div>
  );
}
```

### Use it in your card

```tsx
<div className="inverter-card">          {/* position:relative; overflow:hidden */}
  <LiquidFill fraction={cf} state={status} />
  <div className="inverter-card__plate">  {/* z-index 3 — your existing content */}
    {/* name, nameplate, sparkline, OUTPUT NOW %, of-max, status pill, vendor tag
        — UNCHANGED. Just make sure this wrapper has the frosted bg below. */}
  </div>
</div>
```

---

## Night / "Sleeping" state — the calm zero

**The problem it solves:** with fill = output ÷ max, every inverter reads ~empty at dawn/dusk and dead-empty overnight. An empty tank looks *alarming* — but the inverter isn't broken, the sun is down. So zero-at-night must read **calm and intentional**, while zero-from-a-fault stays **alarming**. Same number, opposite mood.

**Visual treatment (see the Night toggle in the mockup):**
- Liquid → still, dim **indigo resting pool** (~14% height), no bubbles, slow "breathing" opacity pulse. The motion-as-alive signal goes quiet — stillness = rest.
- A 🌙 moon cue in the card corner + a soft "zzz".
- OUTPUT NOW % becomes a quiet em-dash "—" instead of a stark `0%`. Label reads `OUTPUT NOW · ASLEEP`.
- Status pill → **Sleeping** in lavender (NOT red/amber — it's not a problem).
- Sparkline stays but dims, so the day's shape is still visible.
- Whole board can shift to a deeper bg + the array summary's alert box flips to `STATUS / Resting / N inverters asleep · back at sunrise`.

### ⚠️ Trigger: use SUN POSITION, not zero-output

Do **not** trigger "Sleeping" from `output === 0`. A noon fault that knocks every inverter to zero would then get mislabeled "Sleeping" — hiding a real outage. Gate it on the sun being down:

```ts
// Prefer a real sunrise/sunset for the array's lat/long (e.g. SunCalc, or a
// backend-provided `is_daylight` flag). Cheap fallback if you have neither:
function isNight(now: Date, lat: number): boolean {
  // crude civil-daylight approximation; replace with SunCalc when available
  const h = now.getHours();
  return h < 5 || h >= 21; // TODO: compute from lat/long + date
}

// Decide the card state:
const state =
  isNight(new Date(), array.lat) && currentW === 0 ? "sleep"
  : faultFlag ? "fault"
  : cf >= 0.98 ? "clip"
  : cf < 0.35  ? "low"
  : "ok";
```

The condition is `night AND zero` — so an inverter still trickling at dusk shows a real low fill, and a daytime zero correctly falls through to `fault`/`low`, never `sleep`. Ask the backend agent whether `inverter_fleet.py` can expose an `is_daylight` (or sunrise/sunset) per array — it has the lat/long and is the right place to compute it once rather than in every card.

### Sleeping CSS

```css
/* calm: still, dim indigo resting pool. no bubbles. slow breathing glow. */
.liquid--sleep {
  background: linear-gradient(180deg, rgba(90,110,190,.30), rgba(50,62,120,.55));
  animation: liquid-breathe 5.5s ease-in-out infinite;
}
.liquid--sleep::before {
  animation: none;
  background: radial-gradient(ellipse 50% 100% at 50% 100%, rgba(150,170,255,.35), transparent 70%);
}
.liquid--sleep::after { display: none; }
@keyframes liquid-breathe { 0%,100% { opacity:.72; } 50% { opacity:1; } }

/* on the plate, when the card is sleeping (add a `.is-sleeping` class to the card): */
.is-sleeping .pct    { color:#9aa8e0; }      /* lavender, smaller — see mockup */
.is-sleeping .status { background:rgba(120,135,210,.14); color:#aab4e8;
                       border-color:rgba(120,135,210,.28); }
.is-sleeping .sparkline polyline { stroke:#5a6790; opacity:.6; }
```

Moon + zzz are just two small absolutely-positioned spans (`z-index:4`) inside the card; see the mockup markup. Respect `prefers-reduced-motion` — drop the breathing animation for users who ask for less motion (the indigo + moon still read as "asleep" without it).

---

## CSS (Tailwind-friendly notes below)

```css
.inverter-card { position: relative; overflow: hidden; /* your existing card */ }

/* liquid layer — full card height, behind the plate */
.liquid {
  position: absolute; left: 0; right: 0; bottom: 0; z-index: 1; width: 100%;
  background: linear-gradient(180deg, rgba(46,213,115,.42), rgba(24,165,86,.8));
  transition: height 1s cubic-bezier(.4,0,.2,1);   /* smooth rise on poll updates */
}
/* the moving surface meniscus */
.liquid::before, .liquid::after {
  content: ""; position: absolute; top: -12px; left: -50%; width: 200%; height: 24px;
  background: radial-gradient(ellipse 50% 100% at 50% 100%, rgba(120,255,170,.65), transparent 70%);
  animation: liquid-wave 5s linear infinite;
}
.liquid::after { animation-duration: 7s; animation-direction: reverse; opacity: .5; }
@keyframes liquid-wave { from { transform: translateX(0); } to { transform: translateX(25%); } }

/* bubbles */
.liquid__bubbles span {
  position: absolute; bottom: 0; border-radius: 50%;
  background: radial-gradient(circle at 35% 35%, rgba(190,255,210,.9), rgba(120,255,170,.2));
  animation: liquid-rise linear infinite;
}
@keyframes liquid-rise {
  0%   { transform: translateY(0) scale(.6); opacity: 0; }
  15%  { opacity: .85; }
  100% { transform: translateY(-230px) scale(1); opacity: 0; }  /* ~card height */
}

/* state variants */
.liquid--clip { background: linear-gradient(180deg, rgba(90,200,255,.4), rgba(40,150,220,.8)); }
.liquid--fault { background: linear-gradient(180deg, rgba(255,170,60,.42), rgba(200,120,20,.8)); }
.liquid--fault::before, .liquid--fault::after { animation: none; }   /* faults sit still */

/* THE PLATE — this is what protects your text + sparkline */
.inverter-card__plate {
  position: absolute; z-index: 3; inset: 10px;
  background: rgba(11,16,23,.8);          /* opaque enough that liquid never bleeds through glyphs */
  backdrop-filter: blur(7px);
  border: 1px solid rgba(255,255,255,.06); border-radius: 10px;
  padding: 12px 13px 11px;
  display: flex; flex-direction: column;
}
```

**Tailwind equivalent for the plate:** `absolute inset-2.5 z-[3] rounded-[10px] border border-white/5 bg-[#0b1017]/80 backdrop-blur-md p-3 flex flex-col`. The liquid layer is easier to keep as the raw CSS above (keyframes + pseudo-elements don't translate to utilities).

---

## Hard requirements (the failure modes we already hit — don't skip)

1. **Plate opacity ≥ 0.8.** We tried a transparent plate so the liquid showed through behind the text — it re-introduced the see-through text-wash bug. Keep the plate background at `rgba(...,.8)` or higher. The liquid reading comes from the colored RIM around the plate, plus the fill peeking at the bottom edge, not from showing through the text.
2. **`overflow: hidden` on the card** or the liquid spills past the rounded corners.
3. **Memoize bubble positions** (see component) — if you re-roll random positions on every 60s poll, bubbles teleport. Re-roll only when the fill bucket changes.
4. **`aria-hidden` on the liquid** — it's decorative; screen readers should read your real % text, not the animation.
5. **Color is NOT the only signal.** Keep your numeric % and status pill. ~8% of men can't distinguish the green/amber fill — height + text carry the real meaning.

---

## Performance at scale (40+ inverters on one screen)

The mockup gives each card its own CSS animations, which is fine up to ~15-20 cards. For a large array owner:

- **One shared animation clock:** CSS keyframes already share the browser's compositor timeline, so pure-CSS bubbles are cheap (GPU-composited transforms/opacity — no JS per frame). This is why the spec uses CSS animation, NOT requestAnimationFrame per card.
- **Freeze offscreen cards:** add `content-visibility: auto` to `.inverter-card` so off-screen cards stop animating/painting. One line, big win.
- **Cap bubbles at 6/card** (the component already does). Don't raise it.
- If you ever see jank, drop `backdrop-filter: blur()` first — it's the most expensive property here. A flat `rgba(11,16,23,.85)` with no blur looks nearly identical and costs nothing.

---

## What "fill" means — confirm with Ford

The spec uses **fill = current output ÷ max** (capacity factor). That means a healthy inverter reads near-empty at dawn/dusk and a small inverter pinned at its cap reads "fuller" than a big one at half. If you'd rather the fill mean "healthy vs expected-for-this-hour," that needs an expected-output model — flag it, don't silently change the metaphor.
