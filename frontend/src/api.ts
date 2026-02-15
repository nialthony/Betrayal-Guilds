import type { ActionPayload, StateResponse, SummaryResponse } from './types'

const API_BASE = import.meta.env.VITE_API_BASE || ''

type Json = Record<string, unknown>

async function parseResponse(res: Response) {
  const text = await res.text()
  if (!res.ok) {
    throw new Error(`${res.status} ${text}`)
  }
  return text ? (JSON.parse(text) as Json) : {}
}

function authHeaders(token?: string | null) {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

export async function localLogin(agentId: string) {
  const res = await fetch(`${API_BASE}/v1/auth/local-login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ agent_id: agentId.toLowerCase() }),
  })
  return (await parseResponse(res)) as { access_token: string; expires_at_unix: number }
}

export async function whoAmI(token: string) {
  const res = await fetch(`${API_BASE}/v1/auth/whoami`, { headers: authHeaders(token) })
  return parseResponse(res)
}

export async function getSummary(token?: string | null) {
  const res = await fetch(`${API_BASE}/v1/summary`, { headers: authHeaders(token) })
  return (await parseResponse(res)) as SummaryResponse
}

export async function getEvents(sinceEventId: number, token?: string | null) {
  const res = await fetch(`${API_BASE}/v1/state?since_event_id=${sinceEventId}`, {
    headers: authHeaders(token),
  })
  return (await parseResponse(res)) as StateResponse
}

export async function submitActions(token: string, agentId: string, actions: ActionPayload[]) {
  const res = await fetch(`${API_BASE}/v1/actions`, {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      agent_id: agentId,
      tick_submitted: 0,
      actions,
    }),
  })
  return parseResponse(res)
}

export async function resetWorld(adminSecret?: string) {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  }
  if (adminSecret) headers['x-admin-secret'] = adminSecret
  const res = await fetch(`${API_BASE}/v1/admin/reset-world`, {
    method: 'POST',
    headers,
    body: JSON.stringify({}),
  })
  return parseResponse(res)
}
