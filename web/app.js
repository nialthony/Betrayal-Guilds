const API = "";
const POLL_MS = 2200;
const AUTOPILOT_MS = 3200;
const MAX_EVENTS_PER_POLL = 90;

const state = {
  sinceEventId: 0,
  summary: null,
  autoPilot: false,
  autoTimer: null,
  pollTimer: null,
  logQueue: [],
  logFlushTimer: null,
  localLoginBlocked: false,
};

const el = (id) => document.getElementById(id);

function nowStamp() {
  return new Date().toLocaleTimeString();
}

function clip(s, n = 180) {
  const t = String(s || "");
  return t.length > n ? `${t.slice(0, n - 1)}...` : t;
}

function setChip(id, text, tone = "dim") {
  const node = el(id);
  node.textContent = text;
  node.classList.remove("chip--dim", "chip--ok", "chip--warn");
  node.classList.add(tone === "ok" ? "chip--ok" : tone === "warn" ? "chip--warn" : "chip--dim");
}

function setToken(token) {
  const t = String(token || "");
  el("token").value = t;
  localStorage.setItem("bg_token", t);
  setChip("chipAuth", t ? `AUTH: ${t.slice(0, 12)}...` : "AUTH: NONE", t ? "ok" : "dim");
}

function getToken() {
  return el("token").value.trim();
}

function getAgentId() {
  return el("agentId").value.trim() || "agent_alpha_blade";
}

function logLine(kind, msg, tone = "INFO") {
  state.logQueue.push(`[${nowStamp()}] ${kind} ${tone} :: ${String(msg || "")}\n`);
  if (state.logFlushTimer) return;
  state.logFlushTimer = setTimeout(() => {
    state.logFlushTimer = null;
    const log = el("log");
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    log.textContent += state.logQueue.join("");
    state.logQueue = [];
    const rows = log.textContent.split("\n");
    if (rows.length > 550) log.textContent = rows.slice(rows.length - 500).join("\n");
    if (stick) log.scrollTop = log.scrollHeight;
  }, 120);
}

async function apiGet(path, token = null) {
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(API + path, { headers });
  const text = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${text}`);
  return text ? JSON.parse(text) : {};
}

async function apiPost(path, body, token = null) {
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(API + path, { method: "POST", headers, body: JSON.stringify(body) });
  const text = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${text}`);
  return text ? JSON.parse(text) : {};
}

function parseDetail(raw) {
  const t = String(raw || "");
  const i = t.indexOf("{");
  if (i === -1) return "";
  try {
    return String(JSON.parse(t.slice(i)).detail || "");
  } catch {
    return "";
  }
}

async function quickLogin(silent = false) {
  if (state.localLoginBlocked) return false;
  try {
    const res = await apiPost("/v1/auth/local-login", { agent_id: getAgentId() });
    setToken(res.access_token);
    await whoami();
    if (!silent) logLine("AUTH", "quick login success", "OK");
    return true;
  } catch (e) {
    const detail = parseDetail(String(e?.message || e));
    if (detail === "local_auth_disabled" || detail === "bad_local_secret") state.localLoginBlocked = true;
    if (!silent) logLine("AUTH_FAIL", detail || String(e?.message || e), "ERR");
    return false;
  }
}

async function whoami() {
  const token = getToken();
  if (!token) return null;
  try {
    const me = await apiGet("/v1/auth/whoami", token);
    if (me?.agent_id) el("agentId").value = me.agent_id;
    return me;
  } catch {
    setToken("");
    return null;
  }
}

async function submitActions(actions) {
  let token = getToken();
  if (!token) {
    const ok = await quickLogin(true);
    if (!ok) return logLine("AUTH", "No bearer token. Click Quick Login.", "WARN"), false;
    token = getToken();
  }
  const body = { agent_id: getAgentId(), tick_submitted: 0, actions };
  try {
    await apiPost("/v1/actions", body, token);
    return true;
  } catch (e) {
    const detail = parseDetail(String(e?.message || e));
    if (["invalid_session_token", "session_expired", "missing_authorization", "invalid_authorization"].includes(detail)) {
      setToken("");
      if (await quickLogin(true)) return submitActions(actions);
    }
    logLine("ACTION_FAIL", detail || String(e?.message || e), "ERR");
    return false;
  }
}

function currentSelf() {
  return state.summary?.self || null;
}

function enemyCandidates() {
  const me = currentSelf();
  if (!me) return [];
  const agents = Object.values(state.summary?.agents || {});
  return agents.filter((a) => a.guild !== me.guild && a.alive);
}

function allyCandidates() {
  const me = currentSelf();
  if (!me) return [];
  const agents = Object.values(state.summary?.agents || {});
  return agents.filter((a) => a.guild === me.guild && a.agent_id !== me.agent_id);
}

function pickTargetFallback() {
  const manual = el("targetAgent").value.trim();
  if (manual) return manual;
  const enemies = enemyCandidates().sort((a, b) => a.hp - b.hp);
  return enemies[0]?.agent_id || "";
}

async function act(type, extra = {}) {
  const ok = await submitActions([{ type, ...extra }]);
  if (ok) logLine("ACTION", `${type} sent`, "OK");
}

async function runSmartAction() {
  const me = currentSelf();
  if (!me) return;

  const targetEnemy = enemyCandidates().sort((a, b) => a.hp - b.hp)[0];
  const suspectAlly = allyCandidates().sort((a, b) => b.suspicion - a.suspicion)[0];
  const target = targetEnemy?.agent_id || pickTargetFallback();

  if (!me.alive || me.energy <= 1) return act("rest");

  if (me.secret_alignment === "traitor") {
    if (me.energy >= 2 && Math.random() < 0.35) return act("sabotage");
    if (me.energy >= 2 && Math.random() < 0.25) return act("steal_vault");
    if (target && Math.random() < 0.55) return act("strike", { target_agent: target });
    if (suspectAlly?.agent_id && Math.random() < 0.3) return act("accuse", { target_agent: suspectAlly.agent_id });
    return act("farm");
  }

  if (suspectAlly?.suspicion > 0.72 && Math.random() < 0.38) return act("accuse", { target_agent: suspectAlly.agent_id });
  if (target && Math.random() < 0.62) return act("strike", { target_agent: target });
  if (Math.random() < 0.25) return act("guard");
  return act("farm");
}

function setAuto(on) {
  state.autoPilot = !!on;
  if (state.autoTimer) clearInterval(state.autoTimer);
  state.autoTimer = null;
  if (state.autoPilot) {
    state.autoTimer = setInterval(() => runSmartAction(), AUTOPILOT_MS);
  }
  el("btnAutoPilot").textContent = `Autopilot: ${state.autoPilot ? "ON" : "OFF"}`;
  setChip("chipAuto", `AUTO: ${state.autoPilot ? "ON" : "OFF"}`, state.autoPilot ? "ok" : "dim");
}

function renderSummary(s) {
  state.summary = s;
  el("tick").textContent = String(s.tick ?? "-");
  el("matchId").textContent = String(s.match?.id ?? "-");
  el("round").textContent = `${s.match?.round ?? "-"} / ${s.match?.max_rounds ?? "-"}`;
  el("status").textContent = String(s.match?.status || "-");

  const ga = s.guilds?.alpha || {};
  const go = s.guilds?.omega || {};
  el("guildAlpha").textContent = `ALPHA\nHP=${ga.hp ?? "-"}  VAULT=${ga.vault ?? "-"}  SCORE=${ga.score ?? "-"}  WINS=${ga.wins ?? "-"}`;
  el("guildOmega").textContent = `OMEGA\nHP=${go.hp ?? "-"}  VAULT=${go.vault ?? "-"}  SCORE=${go.score ?? "-"}  WINS=${go.wins ?? "-"}`;

  const self = s.self || null;
  if (self) {
    el("myStats").textContent =
      `agent=${self.agent_id}\n` +
      `guild=${self.guild} role=${self.role}\n` +
      `alive=${self.alive} hp=${self.hp} energy=${self.energy} credits=${self.credits}\n` +
      `suspicion=${self.suspicion} trust=${self.trust}\n` +
      `secret_alignment=${self.secret_alignment}\n` +
      `contract=${self.contract?.text || "-"}\n` +
      `progress=${self.contract?.current ?? "-"} / ${self.contract?.target ?? "-"} (${self.contract?.percent ?? 0}%)`;
  } else {
    el("myStats").textContent = "No private intel.\nLogin first.";
  }

  const suspects = (s.top_suspects || [])
    .map((x) => `${x.agent_id}  guild=${x.guild}  susp=${x.suspicion}  alive=${x.alive}  rev=${x.revealed}`);
  el("topSuspects").textContent = suspects.length ? suspects.join("\n") : "(none)";

  const agents = Object.values(s.agents || {})
    .sort((a, b) => (a.guild + a.agent_id).localeCompare(b.guild + b.agent_id))
    .map((a) => `${a.agent_id.padEnd(24)} g=${a.guild.padEnd(5)} hp=${String(a.hp).padEnd(3)} en=${String(a.energy).padEnd(2)} cr=${String(a.credits).padEnd(3)} susp=${String(a.suspicion).padEnd(5)} ${a.revealed ? `[${a.revealed_alignment}]` : ""} act=${a.last_action || "-"}`);
  el("agents").textContent = agents.length ? agents.join("\n") : "(none)";

  const lb = (s.leaderboard || []).map((x, i) => `${String(i + 1).padStart(2, "0")}. ${x.agent_id.padEnd(24)} ${x.points}`);
  el("leaderboard").textContent = lb.length ? lb.join("\n") : "(none)";

  if (!el("targetAgent").value.trim()) {
    const fallback = pickTargetFallback();
    if (fallback) el("targetAgent").value = fallback;
  }
}

function toneForEvent(t) {
  if (t.includes("rejected")) return "ERR";
  if (t.includes("revealed") || t.includes("ended")) return "WARN";
  if (t.includes("strike") || t.includes("sabotage") || t.includes("stolen")) return "HOT";
  return "INFO";
}

function handleEvent(ev) {
  logLine(ev.type, clip(JSON.stringify(ev.payload || {}), 240), toneForEvent(ev.type));
}

async function pollSummary() {
  try {
    const s = await apiGet("/v1/summary", getToken() || null);
    renderSummary(s);
    setChip("chipConn", "ONLINE", "ok");
  } catch (e) {
    setChip("chipConn", "OFFLINE", "warn");
    logLine("SUMMARY_FAIL", String(e?.message || e), "ERR");
  }
}

async function pollEvents() {
  try {
    const s = await apiGet(`/v1/state?since_event_id=${state.sinceEventId}`, getToken() || null);
    const events = s.events || [];
    const scoped = events.length > MAX_EVENTS_PER_POLL ? events.slice(-MAX_EVENTS_PER_POLL) : events;
    for (const ev of scoped) {
      handleEvent(ev);
      if (typeof ev.event_id === "number") state.sinceEventId = Math.max(state.sinceEventId, ev.event_id);
    }
    if (!scoped.length && typeof s.latest_event_id === "number") {
      state.sinceEventId = Math.max(state.sinceEventId, s.latest_event_id);
    }
    el("since").textContent = String(state.sinceEventId);
    el("latest").textContent = String(s.latest_event_id ?? "-");
  } catch (e) {
    logLine("EVENT_FAIL", String(e?.message || e), "ERR");
  }
}

async function refreshNow() {
  await Promise.all([pollSummary(), pollEvents()]);
}

function bind() {
  el("btnQuickLogin").addEventListener("click", () => quickLogin(false));
  el("btnSaveToken").addEventListener("click", async () => { setToken(getToken()); await whoami(); });
  el("btnClearToken").addEventListener("click", () => setToken(""));
  el("btnRefreshNow").addEventListener("click", refreshNow);
  el("btnSmartAction").addEventListener("click", runSmartAction);
  el("btnAutoPilot").addEventListener("click", () => setAuto(!state.autoPilot));

  el("btnStrike").addEventListener("click", () => act("strike", { target_agent: pickTargetFallback() }));
  el("btnGuard").addEventListener("click", () => act("guard"));
  el("btnFarm").addEventListener("click", () => act("farm"));
  el("btnTransfer").addEventListener("click", () => act("transfer", { target_agent: el("targetAgent").value.trim(), amount: parseInt(el("transferAmount").value.trim() || "1", 10) }));
  el("btnScan").addEventListener("click", () => act("scan", { target_agent: el("targetAgent").value.trim() }));
  el("btnAccuse").addEventListener("click", () => act("accuse", { target_agent: el("targetAgent").value.trim() }));
  el("btnSabotage").addEventListener("click", () => act("sabotage"));
  el("btnSteal").addEventListener("click", () => act("steal_vault"));
  el("btnRest").addEventListener("click", () => act("rest"));
}

async function boot() {
  bind();
  setToken(localStorage.getItem("bg_token") || "");
  setAuto(false);
  if (getToken()) await whoami();
  if (!getToken()) await quickLogin(true);
  await refreshNow();
  state.pollTimer = setInterval(refreshNow, POLL_MS);
  logLine("READY", "Betrayal Guilds console online", "OK");
}

boot();
