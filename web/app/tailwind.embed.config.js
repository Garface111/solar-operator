/** @type {import("tailwindcss").Config}
 *
 * THE FOLD - Phase 3: Sky palette for the EMBED build only.
 *
 * Used exclusively by vite.embed.config.ts (npm run build:embed); the
 * standalone SPA keeps tailwind.config.js and must render exactly as today.
 * Same content globs, same class names -- the THEME VALUES are remapped so
 * every existing primary-* / cream* / wood-* class resolves to Array
 * Operator Sky values instead of the NEPOOL emerald/cream/ochre.
 *
 * Sky ramp anchors are the REAL theme-sky.css literals (array-operator
 * public/theme-sky.css section 1), not invented hexes:
 *   #2196F3 --sky-primary/--good | #1E90E8 --good2/--sky-top |
 *   #1976D2 --sky-primary-deep | #1565C0 --green-deep | #BEE3FA --sky-horizon |
 *   #D9E7FB --sky-pastel-blue | #EAF4FD --bg | rgba(20,60,120,x) hairlines.
 *
 * PRESERVED SEMANTICS: literal emerald-* (live/delivered), amber-*
 * (warn/pending) and red-* (failed/bounced) classes are deliberately NOT
 * remapped -- status colors keep their meaning; only the brand skin changes.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // AO self-hosts Plus Jakarta Sans (document-global @font-face in
        // theme-sky.css) -- the embed inherits it by family name.
        sans: [
          "Plus Jakarta Sans",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
      },
      colors: {
        // Brand: emerald -> Sky action blue. Monotonic light->dark;
        // 50/100/200/500/600/700/800 are exact theme-sky.css values.
        primary: {
          50: "#EAF4FD",
          100: "#D9E7FB",
          200: "#BEE3FA",
          300: "#7CC0F5",
          400: "#42A5F5",
          500: "#2196F3",
          600: "#1E90E8",
          700: "#1976D2",
          800: "#1565C0",
          900: "#0D47A1",
          950: "#082F66",
        },
        // Cream -> glass-friendly azure white. The page GROUND itself goes
        // transparent in embed-sky.css (the .rb2 safe-zone sheet shows
        // through); this hex serves bg-cream/40 zebra rows and small fills.
        cream: {
          DEFAULT: "#F2F7FD",
          border: "rgba(20,60,120,.13)", // the Sky hairline (theme-sky-reports --line)
        },
        // Wood (warm ochre accents) -> restrained slate-blue. Keeps the
        // semantic tier distinct from the saturated primary blue.
        wood: {
          50: "#F4F8FC",
          100: "#E6EEF7",
          200: "#C9DAEB",
          300: "#A2BCD8",
          400: "#7391B2",
          500: "#51708F",
          600: "#3A5470",
          border: "rgba(20,60,120,.16)", // theme-sky-reports --so-wood-bd
        },
      },
      borderRadius: {
        // Sky radius scale (theme-sky.css: --sky-r-btn 12px, --sky-r-card 22px).
        // xl nudges toward the card feel; 2xl = the Sky card radius proper.
        xl: "14px",
        "2xl": "22px",
      },
    },
  },
  plugins: [],
};
