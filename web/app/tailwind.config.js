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
        // R1 lock Jun 6'26: primary-500 = #34d399 (one step lighter than the
        // previous #10b981 — the leaf green). 600/700/800/900 cascade one
        // step darker each so hover/active states stay legible. The whole
        // palette stops short of the original forest-green.
        primary: {
          50: "#ecfdf5",
          100: "#d1fae5",
          200: "#a7f3d0",
          300: "#6ee7b7",
          400: "#10b981",
          500: "#34d399",
          600: "#10b981",
          700: "#059669",
          800: "#047857",
          900: "#065f46",
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
