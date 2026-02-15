/// <reference types="vite/client" />
export {}

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_WALLETCONNECT_PROJECT_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare global {
  interface Window {
    __BG_CONFIG__?: {
      walletConnectProjectId?: string
    }
  }
}
