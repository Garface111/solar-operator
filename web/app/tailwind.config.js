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
        // Re-lightened Jun 6'26 (Ford): "all dark green is ugly — use the
        // light array-row green sitewide". 500 → emerald-500 #10b981
        // (living-leaf), 600/700/800/900 stay one step darker each for
        // hover/active legibility but NEVER reach the old forest-green.
        primary: {
          50: "#ecfdf5",
          100: "#d1fae5",
          200: "#a7f3d0",
          300: "#6ee7b7",
          400: "#34d399",
          500: "#10b981",
          600: "#059669",
          700: "#047857",
          800: "#065f46",
          900: "#064e3b",
          950: "#022c22",
        },
        // Cream — warm off-white for page surface and nav bars.
        cream: {
          DEFAULT: "#faf8f5",
          border: "#e8e2d9",
        },
        // Wood — warm ochre for accent borders and status badges.
        wood: {
          50: "#fdf8f2",
          100: "#faedd8",
          200: "#f3d5a8",
          300: "#e6b470",
          400: "#d4914a",
          500: "#b56d2c",
          600: "#8c4e1c",
          border: "#e6d4bd",
        },
      },
      borderRadius: {
        xl: "0.75rem",
      },
    },
  },
  plugins: [],
};
