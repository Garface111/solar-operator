/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL for the OCICBB Mind backend. Defaults to http://localhost:8001. */
  readonly VITE_MIND_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
