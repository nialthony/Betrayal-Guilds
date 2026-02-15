import { createConfig, http } from 'wagmi'
import { injected, walletConnect } from 'wagmi/connectors'
import { monadTestnet } from './chains'

const fallbackProjectId = 'YOUR_WALLETCONNECT_PROJECT_ID'
const walletConnectProjectId = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID || fallbackProjectId

export const walletConnectProjectIdMissing = walletConnectProjectId === fallbackProjectId

const connectors = walletConnectProjectIdMissing
  ? [injected()]
  : [
      injected(),
      walletConnect({
        projectId: walletConnectProjectId,
        showQrModal: true,
      }),
    ]

export const wagmiConfig = createConfig({
  chains: [monadTestnet],
  connectors,
  transports: {
    [monadTestnet.id]: http(monadTestnet.rpcUrls.default.http[0]),
  },
})
