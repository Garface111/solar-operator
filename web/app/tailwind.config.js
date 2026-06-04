/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
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
        // Solar green accent — full palette so primary-50..950 all resolve.
        // Darkened Jun 2026: the old emerald-500 base (#10b981) read too light/
        // vibrant. primary-500 is now emerald-700, with 600/700 shifted to
        // emerald-800/900 so hover/active states (bg-primary-600/700) stay
        // legibly darker than the base CTA.
        primary: {
          50: "#ecfdf5",
          100: "#d1fae5",
          200: "#a7f3d0",
          300: "#6ee7b7",
          400: "#34d399",
          500: "#047857",
          600: "#065f46",
          700: "#064e3b",
          800: "#065f46",
          900: "#064e3b",
          950: "#022c22",
        },
      },
      borderRadius: {
        xl: "0.75rem",
      },
    },
  },
  plugins: [],
};
