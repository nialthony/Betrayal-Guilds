const API_BASE = "";
const POLL_MS = 2800;
const AUTOPILOT_MS = 3400;
const MAX_RENDERED_EVENTS = 220;
const MAX_EVENTS_PER_POLL = 40;

const appState = {
  summary: null,
  sinceEventId: 0,
  autopilot: false,
  autopilotTimer: null,
  pollTimer: null,
  feedPaused: false,
  localLoginBlocked: false,
};

const $ = (id) => document.getElementById(id);

function ts() {
  return new Date().toLocaleTimeString();
}

function clip(s, max = 190) {
  const text = String(s || "");
  return text.length > max ? `${text.slice(0, max - 1)}...` : text;
}

function setChip(id, text, tone = "") {
  const node = $(id);
  node.textContent = text;
  node.classList.remove("ok", "warn");
  if (tone) node.classList.add(tone);
}

function setToken(token) {
  const t = String(token || "");
  $("tokenInput").value = t;
  localStorage.setItem("bg_token", t);
  if (!t) {
    setChip("chipAuth", "AUTH: NONE");
    return;
  }
  setChip("chipAuth", `AUTH: ${t.slice(0, 12)}...`, "ok");
}

function token() {
  return $("tokenInput").value.trim();
}

function agentId() {
  return $("agentIdInput").value.trim() || "agent_alpha_blade";
}

function appendEvent(text) {
  if (appState.feedPaused) return;
  const feed = $("eventFeed");
  const stick = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 28;
  const li = document.createElement("li");
  li.textContent = text;
  feed.appendChild(li);
  while (feed.children.length > MAX_RENDERED_EVENTS) feed.removeChild(feed.firstChild);
  if (stick) feed.scrollTop = feed.scrollHeight;
}

function parseErrorDetail(raw) {
  const t = String(raw || "");
  const i = t.indexOf("{");
  if (i < 0) return "";
  try {
    const obj = JSON.parse(t.slice(i));
    return String(obj.detail || "");
  } catch {
    return "";
  }
}

async function apiGet(path, authToken = null) {
  const headers = { Accept: "application/json" };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  const r = await fetch(API_BASE + path, { headers });
  const text = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${text}`);
  return text ? JSON.parse(text) : {};
}

async function apiPost(path, body, authToken = null, extraHeaders = {}) {
  const headers = { "Content-Type": "application/json", Accept: "application/json", ...extraHeaders };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  const r = await fetch(API_BASE + path, { method: "POST", headers, body: JSON.stringify(body || {}) });
  const text = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${text}`);
  return text ? JSON.parse(text) : {};
}

async function quickLogin(silent = false) {
  if (appState.localLoginBlocked) return false;
  try {
    const res = await apiPost("/v1/auth/local-login", { agent_id: agentId() });
    setToken(res.access_token);
    if (!silent) appendEvent(`[${ts()}] AUTH OK :: quick login success`);
    return true;
  } catch (e) {
    const detail = parseErrorDetail(e?.message || e);
    if (detail === "local_auth_disabled" || detail === "bad_local_secret") appState.localLoginBlocked = true;
    if (!silent) appendEvent(`[${ts()}] AUTH FAIL :: ${detail || e.message}`);
    return false;
  }
}

async function whoami(logResult = true) {
  const t = token();
  if (!t) return null;
  try {
    const me = await apiGet("/v1/auth/whoami", t);
    if (me?.agent_id) $("agentIdInput").value = me.agent_id;
    if (logResult) appendEvent(`[${ts()}] WHOAMI :: ${JSON.stringify(me)}`);
    return me;
  } catch (e) {
    setToken("");
    if (logResult) appendEvent(`[${ts()}] WHOAMI FAIL :: ${e.message}`);
    return null;
  }
}

function selfRow() {
  return appState.summary?.self || null;
}

function enemies() {
  const me = selfRow();
  if (!me) return [];
  return Object.values(appState.summary?.agents || {}).filter((row) => row.guild !== me.guild && row.alive);
}

function allies() {
  const me = selfRow();
  if (!me) return [];
  return Object.values(appState.summary?.agents || {}).filter((row) => row.guild === me.guild && row.agent_id !== me.agent_id);
}

function pickTarget() {
  const manual = $("targetInput").value.trim();
  if (manual) return manual;
  const candidate = enemies().sort((a, b) => a.hp - b.hp)[0];
  return candidate?.agent_id || "";
}

async function submitActions(actions, retry = true) {
  let t = token();
  if (!t) {
    const ok = await quickLogin(true);
    if (!ok) {
      appendEvent(`[${ts()}] ACTION BLOCKED :: no bearer token`);
      return false;
    }
    t = token();
  }

  try {
    await apiPost("/v1/actions", { agent_id: agentId(), tick_submitted: 0, actions }, t);
    return true;
  } catch (e) {
    const detail = parseErrorDetail(e?.message || e);
    if (retry && ["invalid_session_token", "session_expired", "missing_authorization", "invalid_authorization"].includes(detail)) {
      setToken("");
      return submitActions(actions, false);
    }
    appendEvent(`[${ts()}] ACTION FAIL :: ${detail || e.message}`);
    return false;
  }
}

async function doAction(type, extra = {}) {
  const ok = await submitActions([{ type, ...extra }]);
  if (ok) appendEvent(`[${ts()}] ACTION OK :: ${type}`);
}

async function smartMove() {
  const me = selfRow();
  if (!me) return;
  if (!me.alive || me.energy <= 1) return doAction("rest");

  const enemy = enemies().sort((a, b) => a.hp - b.hp)[0];
  const allySusp = allies().sort((a, b) => b.suspicion - a.suspicion)[0];

  if (me.secret_alignment === "traitor") {
    if (me.energy >= 2 && Math.random() < 0.34) return doAction("sabotage");
    if (me.energy >= 2 && Math.random() < 0.27) return doAction("steal_vault");
    if (enemy && Math.random() < 0.58) return doAction("strike", { target_agent: enemy.agent_id });
    if (allySusp && Math.random() < 0.24) return doAction("accuse", { target_agent: allySusp.agent_id });
    return doAction("farm");
  }

  if (allySusp?.suspicion > 0.74 && Math.random() < 0.36) return doAction("accuse", { target_agent: allySusp.agent_id });
  if (enemy && Math.random() < 0.64) return doAction("strike", { target_agent: enemy.agent_id });
  if (Math.random() < 0.24) return doAction("guard");
  return doAction("farm");
}

function setAutopilot(on) {
  appState.autopilot = !!on;
  if (appState.autopilotTimer) clearInterval(appState.autopilotTimer);
  appState.autopilotTimer = null;
  if (appState.autopilot) appState.autopilotTimer = setInterval(() => smartMove(), AUTOPILOT_MS);
  $("btnAutoPilot").textContent = `Autopilot ${appState.autopilot ? "ON" : "OFF"}`;
  setChip("chipAuto", `AUTO: ${appState.autopilot ? "ON" : "OFF"}`, appState.autopilot ? "ok" : "");
}

function renderSummary(s) {
  appState.summary = s;
  $("kpiTick").textContent = String(s.tick ?? "-");
  $("kpiMatch").textContent = String(s.match?.id ?? "-");
  $("kpiRound").textContent = `${s.match?.round ?? "-"} / ${s.match?.max_rounds ?? "-"}`;
  $("kpiStatus").textContent = String(s.match?.status || "-");
  $("kpiWinner").textContent = String(s.match?.winner || "-");

  const alpha = s.guilds?.alpha || {};
  const omega = s.guilds?.omega || {};
  $("guildAlpha").textContent = `ALPHA\nHP=${alpha.hp ?? "-"}  VAULT=${alpha.vault ?? "-"}\nSCORE=${alpha.score ?? "-"}  WINS=${alpha.wins ?? "-"}`;
  $("guildOmega").textContent = `OMEGA\nHP=${omega.hp ?? "-"}  VAULT=${omega.vault ?? "-"}\nSCORE=${omega.score ?? "-"}  WINS=${omega.wins ?? "-"}`;

  if (s.self) {
    $("myIntel").textContent =
      `agent=${s.self.agent_id}\n` +
      `guild=${s.self.guild} role=${s.self.role}\n` +
      `alive=${s.self.alive} hp=${s.self.hp} energy=${s.self.energy} credits=${s.self.credits}\n` +
      `suspicion=${s.self.suspicion} trust=${s.self.trust}\n` +
      `secret_alignment=${s.self.secret_alignment}\n` +
      `contract=${s.self.contract?.text || "-"}\n` +
      `progress=${s.self.contract?.current ?? "-"} / ${s.self.contract?.target ?? "-"} (${s.self.contract?.percent ?? 0}%)`;
  } else {
    $("myIntel").textContent = "(login first)";
  }

  const suspects = (s.top_suspects || []).map(
    (row) => `${row.agent_id}\nguild=${row.guild} susp=${row.suspicion} rev=${row.revealed}`
  );
  $("suspectBoard").textContent = suspects.length ? suspects.join("\n\n") : "(none)";

  const agents = Object.values(s.agents || {})
    .sort((a, b) => (a.guild + a.agent_id).localeCompare(b.guild + b.agent_id))
    .map((row) => {
      const rev = row.revealed ? `[${row.revealed_alignment}]` : "";
      return `${row.agent_id.padEnd(24)} g=${row.guild.padEnd(5)} hp=${String(row.hp).padEnd(3)} en=${String(row.energy).padEnd(2)} cr=${String(row.credits).padEnd(3)} susp=${String(row.suspicion).padEnd(5)} ${rev} act=${row.last_action || "-"}`;
    });
  $("agentBoard").textContent = agents.length ? agents.join("\n") : "(none)";

  const leaders = (s.leaderboard || []).map((row, i) => `${String(i + 1).padStart(2, "0")}. ${row.agent_id.padEnd(24)} ${row.points}`);
  $("leaderboardBoard").textContent = leaders.length ? leaders.join("\n") : "(none)";

  if (!$("targetInput").value.trim()) {
    const pick = pickTarget();
    if (pick) $("targetInput").value = pick;
  }
}

async function pollSummary() {
  try {
    const s = await apiGet("/v1/summary", token() || null);
    renderSummary(s);
    setChip("chipConnection", "ONLINE", "ok");
  } catch (e) {
    setChip("chipConnection", "OFFLINE", "warn");
    appendEvent(`[${ts()}] SUMMARY FAIL :: ${e.message}`);
  }
}

async function pollEvents() {
  try {
    const s = await apiGet(`/v1/state?since_event_id=${appState.sinceEventId}`, token() || null);
    const allEvents = s.events || [];
    const events = allEvents.length > MAX_EVENTS_PER_POLL ? allEvents.slice(-MAX_EVENTS_PER_POLL) : allEvents;
    for (const ev of events) {
      appState.sinceEventId = Math.max(appState.sinceEventId, Number(ev.event_id || 0));
      const payload = clip(JSON.stringify(ev.payload || {}), 230);
      appendEvent(`[${ts()}] t${ev.tick} ${ev.type} :: ${payload}`);
    }
    if (!events.length && typeof s.latest_event_id === "number") {
      appState.sinceEventId = Math.max(appState.sinceEventId, s.latest_event_id);
    }
    $("sinceEvent").textContent = String(appState.sinceEventId);
    $("latestEvent").textContent = String(s.latest_event_id ?? "-");
  } catch (e) {
    appendEvent(`[${ts()}] EVENT FAIL :: ${e.message}`);
  }
}

async function refreshNow() {
  await Promise.all([pollSummary(), pollEvents()]);
}

function bind() {
  $("btnQuickLogin").addEventListener("click", () => quickLogin(false));
  $("btnSaveToken").addEventListener("click", async () => {
    setToken(token());
    await whoami(false);
  });
  $("btnClearToken").addEventListener("click", () => setToken(""));
  $("btnRefresh").addEventListener("click", refreshNow);
  $("btnWhoAmI").addEventListener("click", () => whoami(true));
  $("btnSmartMove").addEventListener("click", smartMove);
  $("btnAutoPilot").addEventListener("click", () => setAutopilot(!appState.autopilot));
  $("btnPauseFeed").addEventListener("click", () => {
    appState.feedPaused = !appState.feedPaused;
    $("feedState").textContent = appState.feedPaused ? "PAUSED" : "LIVE";
    $("btnPauseFeed").textContent = appState.feedPaused ? "Resume Feed" : "Pause Feed";
  });
  $("btnResetWorld").addEventListener("click", async () => {
    try {
      const secret = $("adminSecretInput").value.trim();
      const headers = secret ? { "x-admin-secret": secret } : {};
      await apiPost("/v1/admin/reset-world", {}, null, headers);
      appState.sinceEventId = 0;
      $("eventFeed").innerHTML = "";
      appendEvent(`[${ts()}] ADMIN :: world reset`);
      await refreshNow();
    } catch (e) {
      appendEvent(`[${ts()}] RESET FAIL :: ${e.message}`);
    }
  });

  $("btnStrike").addEventListener("click", () => doAction("strike", { target_agent: pickTarget() }));
  $("btnGuard").addEventListener("click", () => doAction("guard"));
  $("btnFarm").addEventListener("click", () => doAction("farm"));
  $("btnTransfer").addEventListener("click", () => doAction("transfer", { target_agent: $("targetInput").value.trim(), amount: parseInt($("amountInput").value.trim() || "1", 10) }));
  $("btnScan").addEventListener("click", () => doAction("scan", { target_agent: $("targetInput").value.trim() }));
  $("btnAccuse").addEventListener("click", () => doAction("accuse", { target_agent: $("targetInput").value.trim() }));
  $("btnSabotage").addEventListener("click", () => doAction("sabotage"));
  $("btnSteal").addEventListener("click", () => doAction("steal_vault"));
  $("btnRest").addEventListener("click", () => doAction("rest"));
}

async function boot() {
  bind();
  setToken(localStorage.getItem("bg_token") || "");
  setAutopilot(false);
  if (token()) await whoami(false);
  if (!token()) await quickLogin(true);
  await refreshNow();
  appState.pollTimer = setInterval(refreshNow, POLL_MS);
  appendEvent(`[${ts()}] READY :: Betrayal Guilds Ops Room online`);
}

boot();
