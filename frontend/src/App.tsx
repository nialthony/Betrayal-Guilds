import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAccount, useChainId, useConnect, useDisconnect, useSwitchChain } from 'wagmi'
import { getEvents, getSummary, localLogin, resetWorld, submitActions, whoAmI } from './api'
import { monadTestnet } from './chains'
import type { ActionPayload, AgentRow, SummaryResponse } from './types'
import { walletConnectProjectIdMissing } from './wallet'

const POLL_MS = 2600
const AUTOPILOT_MS = 3400
const MAX_FEED_ITEMS = 240

type FeedEntry = { key: string; text: string }

function nowStamp() {
  return new Date().toLocaleTimeString()
}

function clip(raw: string, max = 220) {
  return raw.length > max ? `${raw.slice(0, max - 1)}...` : raw
}

function parseDetail(err: unknown) {
  const text = String(err instanceof Error ? err.message : err || '')
  const idx = text.indexOf('{')
  if (idx < 0) return text
  try {
    const parsed = JSON.parse(text.slice(idx)) as { detail?: string }
    return parsed.detail || text
  } catch {
    return text
  }
}

function pickLowestHp(rows: AgentRow[]) {
  const sorted = [...rows].sort((a, b) => a.hp - b.hp)
  return sorted[0]
}

export default function App() {
  const { address, isConnected } = useAccount()
  const { connect, connectors, isPending } = useConnect()
  const chainId = useChainId()
  const { disconnect } = useDisconnect()
  const { switchChain } = useSwitchChain()

  const [token, setToken] = useState('')
  const [agentId, setAgentId] = useState('agent_alpha_blade')
  const [targetAgent, setTargetAgent] = useState('')
  const [transferAmount, setTransferAmount] = useState(2)
  const [adminSecret, setAdminSecret] = useState('')
  const [summary, setSummary] = useState<SummaryResponse | null>(null)
  const [latestEventId, setLatestEventId] = useState(0)
  const [feedPaused, setFeedPaused] = useState(false)
  const [autopilot, setAutopilot] = useState(false)
  const [feed, setFeed] = useState<FeedEntry[]>([])

  const sinceRef = useRef(0)
  const pollingRef = useRef<number | null>(null)
  const autopilotRef = useRef<number | null>(null)

  const onMonad = chainId === monadTestnet.id

  const pushFeed = useCallback(
    (text: string) => {
      if (feedPaused) return
      setFeed((prev) => {
        const next = [...prev, { key: `${Date.now()}-${Math.random()}`, text }]
        return next.slice(Math.max(0, next.length - MAX_FEED_ITEMS))
      })
    },
    [feedPaused],
  )

  const walletAgentId = useMemo(() => (address ? address.toLowerCase() : ''), [address])

  const enemyRows = useMemo(() => {
    if (!summary?.self) return []
    return Object.values(summary.agents).filter(
      (row) => row.guild !== summary.self?.guild && row.alive,
    )
  }, [summary])

  const allyRows = useMemo(() => {
    if (!summary?.self) return []
    return Object.values(summary.agents).filter(
      (row) => row.guild === summary.self?.guild && row.agent_id !== summary.self?.agent_id,
    )
  }, [summary])

  const saveToken = useCallback((value: string) => {
    setToken(value)
    localStorage.setItem('bg_token', value)
  }, [])

  const login = useCallback(
    async (wantedAgentId: string) => {
      const session = await localLogin(wantedAgentId.toLowerCase())
      saveToken(session.access_token)
      pushFeed(`[${nowStamp()}] AUTH OK :: agent=${wantedAgentId.toLowerCase()}`)
      return session.access_token
    },
    [pushFeed, saveToken],
  )

  const ensureToken = useCallback(async () => {
    if (token) return token
    return login(agentId)
  }, [agentId, login, token])

  const refresh = useCallback(async () => {
    try {
      const summaryRes = await getSummary(token || null)
      setSummary(summaryRes)

      const stateRes = await getEvents(sinceRef.current, token || null)
      setLatestEventId(stateRes.latest_event_id ?? sinceRef.current)

      const nextEvents = stateRes.events.slice(-40)
      for (const event of nextEvents) {
        sinceRef.current = Math.max(sinceRef.current, event.event_id)
        const payload = clip(JSON.stringify(event.payload))
        pushFeed(`[${nowStamp()}] t${event.tick} ${event.type} :: ${payload}`)
      }
      if (!nextEvents.length && typeof stateRes.latest_event_id === 'number') {
        sinceRef.current = Math.max(sinceRef.current, stateRes.latest_event_id)
      }
    } catch (error) {
      pushFeed(`[${nowStamp()}] REFRESH FAIL :: ${parseDetail(error)}`)
    }
  }, [pushFeed, token])

  const runAction = useCallback(
    async (payload: ActionPayload) => {
      try {
        const accessToken = await ensureToken()
        await submitActions(accessToken, agentId, [payload])
        pushFeed(`[${nowStamp()}] ACTION OK :: ${payload.type}`)
      } catch (error) {
        pushFeed(`[${nowStamp()}] ACTION FAIL :: ${parseDetail(error)}`)
      }
    },
    [agentId, ensureToken, pushFeed],
  )

  const smartMove = useCallback(async () => {
    const self = summary?.self
    if (!self) return
    if (!self.alive || self.energy <= 1) {
      await runAction({ type: 'rest' })
      return
    }

    const enemy = pickLowestHp(enemyRows)
    const suspect = [...allyRows].sort((a, b) => b.suspicion - a.suspicion)[0]

    if (self.secret_alignment === 'traitor') {
      if (self.energy >= 2 && Math.random() < 0.33) return runAction({ type: 'sabotage' })
      if (self.energy >= 2 && Math.random() < 0.26) return runAction({ type: 'steal_vault' })
      if (enemy && Math.random() < 0.58) return runAction({ type: 'strike', target_agent: enemy.agent_id })
      if (suspect && Math.random() < 0.22) return runAction({ type: 'accuse', target_agent: suspect.agent_id })
      return runAction({ type: 'farm' })
    }

    if (suspect && suspect.suspicion > 0.74 && Math.random() < 0.35) {
      return runAction({ type: 'accuse', target_agent: suspect.agent_id })
    }
    if (enemy && Math.random() < 0.64) return runAction({ type: 'strike', target_agent: enemy.agent_id })
    if (Math.random() < 0.22) return runAction({ type: 'guard' })
    return runAction({ type: 'farm' })
  }, [allyRows, enemyRows, runAction, summary?.self])

  useEffect(() => {
    const stored = localStorage.getItem('bg_token') || ''
    setToken(stored)
  }, [])

  useEffect(() => {
    if (!walletAgentId) return
    setAgentId(walletAgentId)
  }, [walletAgentId])

  useEffect(() => {
    void refresh()
    pollingRef.current = window.setInterval(() => {
      void refresh()
    }, POLL_MS)
    return () => {
      if (pollingRef.current) window.clearInterval(pollingRef.current)
    }
  }, [refresh])

  useEffect(() => {
    if (!autopilot) {
      if (autopilotRef.current) window.clearInterval(autopilotRef.current)
      autopilotRef.current = null
      return
    }
    autopilotRef.current = window.setInterval(() => {
      void smartMove()
    }, AUTOPILOT_MS)
    return () => {
      if (autopilotRef.current) window.clearInterval(autopilotRef.current)
      autopilotRef.current = null
    }
  }, [autopilot, smartMove])

  const fallbackTarget = targetAgent || pickLowestHp(enemyRows)?.agent_id || ''

  const alpha = summary?.guilds?.alpha
  const omega = summary?.guilds?.omega

  return (
    <div className="page">
      <header className="topbar">
        <div>
          <div className="brand">BETRAYAL GUILDS // VITE OPS</div>
          <div className="sub">wallet-native control room on Monad testnet</div>
        </div>
        <div className="top-actions">
          {!isConnected &&
            connectors.map((connector) => (
              <button
                className="btn ghost"
                key={connector.uid}
                onClick={() => connect({ connector })}
                disabled={isPending}
              >
                {isPending ? `Connecting ${connector.name}...` : `Connect ${connector.name}`}
              </button>
            ))}
          {isConnected && (
            <button className="btn ghost" onClick={() => disconnect()}>
              Disconnect Wallet
            </button>
          )}
          <button className="btn ghost" onClick={() => setAutopilot((v) => !v)}>
            {autopilot ? 'Autopilot ON' : 'Autopilot OFF'}
          </button>
          <button className="btn ghost" onClick={() => setFeedPaused((v) => !v)}>
            {feedPaused ? 'Feed Paused' : 'Feed Live'}
          </button>
        </div>
      </header>

      <main className="layout">
        <section className="card hero">
          <h1>Trust is fake. Ledger is real.</h1>
          <p>
            Connect wallet to Monad Testnet, map address to agent identity, login session, then drive betrayal actions directly to the arena API.
          </p>
          {walletConnectProjectIdMissing && (
            <div className="warning">Set <code>VITE_WALLETCONNECT_PROJECT_ID</code> for full WalletConnect support.</div>
          )}
          {isConnected && !onMonad && (
            <div className="warning">
              Connected to wrong chain. Expected Monad Testnet ({monadTestnet.id}).
              <button className="btn small" onClick={() => switchChain({ chainId: monadTestnet.id })}>
                Switch Chain
              </button>
            </div>
          )}
        </section>

        <section className="grid-two">
          <article className="card">
            <h2>Identity + Session</h2>
            <div className="field">
              <label>Agent ID</label>
              <input value={agentId} onChange={(e) => setAgentId(e.target.value)} />
            </div>
            <div className="field">
              <label>Wallet Address</label>
              <input value={walletAgentId || '-'} readOnly />
            </div>
            <div className="buttons">
              <button
                className="btn"
                onClick={async () => {
                  try {
                    await login(agentId)
                  } catch (error) {
                    pushFeed(`[${nowStamp()}] AUTH FAIL :: ${parseDetail(error)}`)
                  }
                }}
              >
                Local Login
              </button>
              <button
                className="btn"
                onClick={async () => {
                  if (!walletAgentId) {
                    pushFeed(`[${nowStamp()}] AUTH FAIL :: connect wallet first`)
                    return
                  }
                  try {
                    setAgentId(walletAgentId)
                    await login(walletAgentId)
                  } catch (error) {
                    pushFeed(`[${nowStamp()}] AUTH FAIL :: ${parseDetail(error)}`)
                  }
                }}
              >
                Sync Wallet Session
              </button>
              <button
                className="btn"
                onClick={async () => {
                  if (!token) {
                    pushFeed(`[${nowStamp()}] WHOAMI :: no token`)
                    return
                  }
                  try {
                    const me = await whoAmI(token)
                    pushFeed(`[${nowStamp()}] WHOAMI :: ${clip(JSON.stringify(me), 280)}`)
                  } catch (error) {
                    pushFeed(`[${nowStamp()}] WHOAMI FAIL :: ${parseDetail(error)}`)
                  }
                }}
              >
                WhoAmI
              </button>
              <button
                className="btn danger"
                onClick={() => {
                  saveToken('')
                  disconnect()
                  pushFeed(`[${nowStamp()}] AUTH :: session cleared`)
                }}
              >
                Clear Session
              </button>
            </div>
            <div className="status-row">
              <span>token: {token ? `${token.slice(0, 12)}...` : 'none'}</span>
              <span>chain: {chainId || '-'}</span>
            </div>
          </article>

          <article className="card">
            <h2>Arena Snapshot</h2>
            <div className="kpi">
              <div><span>Tick</span><strong>{summary?.tick ?? '-'}</strong></div>
              <div><span>Match</span><strong>{summary?.match?.id ?? '-'}</strong></div>
              <div><span>Round</span><strong>{summary ? `${summary.match.round}/${summary.match.max_rounds}` : '-'}</strong></div>
              <div><span>Status</span><strong>{summary?.match?.status ?? '-'}</strong></div>
              <div><span>Winner</span><strong>{summary?.match?.winner ?? '-'}</strong></div>
            </div>
            <pre className="mono">
{`ALPHA  hp=${alpha?.hp ?? '-'} vault=${alpha?.vault ?? '-'} wins=${alpha?.wins ?? '-'}
OMEGA  hp=${omega?.hp ?? '-'} vault=${omega?.vault ?? '-'} wins=${omega?.wins ?? '-'}`}
            </pre>
            <div className="status-row">
              <span>since_event_id: {sinceRef.current}</span>
              <span>latest_event_id: {latestEventId}</span>
            </div>
          </article>
        </section>

        <section className="grid-two">
          <article className="card">
            <h2>Action Console</h2>
            <div className="field-grid">
              <div className="field">
                <label>Target Agent</label>
                <input value={targetAgent} onChange={(e) => setTargetAgent(e.target.value)} placeholder="agent_omega_blade" />
              </div>
              <div className="field">
                <label>Transfer Amount</label>
                <input
                  type="number"
                  min={1}
                  value={transferAmount}
                  onChange={(e) => setTransferAmount(Number(e.target.value))}
                />
              </div>
              <div className="field">
                <label>Admin Secret (optional)</label>
                <input value={adminSecret} onChange={(e) => setAdminSecret(e.target.value)} />
              </div>
            </div>
            <div className="buttons action-buttons">
              <button className="btn main" onClick={() => runAction({ type: 'strike', target_agent: fallbackTarget })}>Strike</button>
              <button className="btn" onClick={() => runAction({ type: 'guard' })}>Guard</button>
              <button className="btn" onClick={() => runAction({ type: 'farm' })}>Farm</button>
              <button className="btn" onClick={() => runAction({ type: 'transfer', target_agent: fallbackTarget, amount: transferAmount })}>Transfer</button>
              <button className="btn" onClick={() => runAction({ type: 'scan', target_agent: fallbackTarget })}>Scan</button>
              <button className="btn" onClick={() => runAction({ type: 'accuse', target_agent: fallbackTarget })}>Accuse</button>
              <button className="btn" onClick={() => runAction({ type: 'sabotage' })}>Sabotage</button>
              <button className="btn" onClick={() => runAction({ type: 'steal_vault' })}>Steal Vault</button>
              <button className="btn" onClick={() => runAction({ type: 'rest' })}>Rest</button>
              <button className="btn alt" onClick={() => void smartMove()}>Smart Move</button>
              <button className="btn" onClick={() => void refresh()}>Refresh</button>
              <button
                className="btn danger"
                onClick={async () => {
                  try {
                    await resetWorld(adminSecret)
                    sinceRef.current = 0
                    setLatestEventId(0)
                    setFeed([])
                    pushFeed(`[${nowStamp()}] ADMIN :: world reset`)
                    await refresh()
                  } catch (error) {
                    pushFeed(`[${nowStamp()}] ADMIN FAIL :: ${parseDetail(error)}`)
                  }
                }}
              >
                Reset World
              </button>
            </div>
          </article>

          <article className="card">
            <h2>Intel Boards</h2>
            <div className="boards">
              <div>
                <h3>My Intel</h3>
                <pre className="mono">
{summary?.self
  ? `agent=${summary.self.agent_id}
guild=${summary.self.guild} role=${summary.self.role}
alive=${summary.self.alive} hp=${summary.self.hp} energy=${summary.self.energy} credits=${summary.self.credits}
suspicion=${summary.self.suspicion} trust=${summary.self.trust}
secret_alignment=${summary.self.secret_alignment}
contract=${summary.self.contract.text}
progress=${summary.self.contract.current}/${summary.self.contract.target} (${summary.self.contract.percent}%)`
  : '(login first)'}
                </pre>
              </div>
              <div>
                <h3>Top Suspects</h3>
                <pre className="mono">
{summary?.top_suspects?.length
  ? summary.top_suspects
      .map((row) => `${row.agent_id}  g=${row.guild}  susp=${row.suspicion}  rev=${row.revealed}`)
      .join('\n')
  : '(none)'}
                </pre>
              </div>
              <div>
                <h3>Leaderboard</h3>
                <pre className="mono">
{summary?.leaderboard?.length
  ? summary.leaderboard
      .map((row, i) => `${String(i + 1).padStart(2, '0')}. ${row.agent_id} ${row.points}`)
      .join('\n')
  : '(none)'}
                </pre>
              </div>
            </div>
          </article>
        </section>

        <section className="card">
          <h2>Event Feed</h2>
          <ul className="feed">
            {feed.map((entry) => (
              <li key={entry.key}>{entry.text}</li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  )
}
