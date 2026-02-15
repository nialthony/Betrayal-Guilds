export type ActionType =
  | 'strike'
  | 'guard'
  | 'farm'
  | 'transfer'
  | 'scan'
  | 'accuse'
  | 'sabotage'
  | 'steal_vault'
  | 'rest'

export type ActionPayload = {
  type: ActionType
  target_agent?: string
  amount?: number
}

export type AgentRow = {
  agent_id: string
  guild: 'alpha' | 'omega' | string
  role: string
  alive: boolean
  hp: number
  energy: number
  credits: number
  suspicion: number
  trust: number
  revealed: boolean
  revealed_alignment: string | null
  last_action: string
}

export type SummaryResponse = {
  tick: number
  match: {
    id: number
    round: number
    max_rounds: number
    status: string
    winner: string | null
  }
  guilds: {
    alpha: { hp: number; vault: number; score: number; wins: number }
    omega: { hp: number; vault: number; score: number; wins: number }
  }
  agents: Record<string, AgentRow>
  top_suspects: AgentRow[]
  leaderboard: Array<{ agent_id: string; points: number }>
  self?: {
    agent_id: string
    guild: string
    role: string
    alive: boolean
    hp: number
    energy: number
    credits: number
    suspicion: number
    trust: number
    secret_alignment: string
    contract: {
      text: string
      current: number
      target: number
      percent: number
    }
  }
}

export type StateResponse = {
  latest_event_id: number
  events: Array<{
    event_id: number
    tick: number
    type: string
    payload: Record<string, unknown>
  }>
}
