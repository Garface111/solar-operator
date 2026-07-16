import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "tailwindcss";
import autoprefixer from "autoprefixer";
import type { Plugin as PostcssPlugin, Rule } from "postcss";

// ─── THE FOLD: "Generation reports" embed build ──────────────────────────────
// Builds src/embed.tsx into a self-contained IIFE (dist-embed/embed.js) plus
// ONE stylesheet (dist-embed/embed.css) whose every selector is scoped under
// #so-genrep, so the module can mount inside Array Operator's vanilla SPA
// without Tailwind preflight (or anything else) leaking into the host page —
// and with enough specificity that the host's element-level rules mostly stay
// out of the module. Run with: npm run build:embed

const PREFIX = "#so-genrep";

/** Scope every selector under #so-genrep. `html`/`body`/`:root`/`#root`
 *  collapse to the prefix itself (their declarations belong to the embed
 *  container, not the host page). Keyframe step selectors are left alone. */
function prefixSelectors(): PostcssPlugin {
  const scopeOne = (sel: string): string => {
    const s = sel.trim();
    if (!s || s.startsWith(PREFIX)) return s;
    // Selectors that mean "the page" now mean "the embed container".
    const PAGE = /^(html|body|:root|#root)$/;
    if (PAGE.test(s)) return PREFIX;
    // "html .x" / "body .x" → "#so-genrep .x"
    const lead = s.match(/^(html|body|:root|#root)([\s>+~].*)$/);
    if (lead) return PREFIX + lead[2];
    return `${PREFIX} ${s}`;
  };
  return {
    postcssPlugin: "so-genrep-prefix",
    Rule(rule: Rule) {
      const parent = rule.parent as { type?: string; name?: string } | undefined;
      if (parent?.type === "atrule" && /keyframes$/i.test(parent.name ?? "")) return;
      rule.selectors = [...new Set(rule.selectors.map(scopeOne))];
    },
  };
}

export default defineConfig({
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  css: {
    postcss: {
      // Order matters: Tailwind expands its directives first, then everything
      // (preflight included) is scoped under the embed root.
      plugins: [tailwindcss(), autoprefixer(), prefixSelectors()],
    },
  },
  build: {
    outDir: "dist-embed",
    emptyOutDir: true,
    cssCodeSplit: false,
    lib: {
      entry: "src/embed.tsx",
      name: "NepoolGenReportsBundle",
      formats: ["iife"],
      fileName: () => "embed.js",
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        assetFileNames: (info) =>
          info.name?.endsWith(".css") ? "embed.css" : "assets/[name][extname]",
      },
    },
  },
});
