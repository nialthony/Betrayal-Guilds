const API = "";
const POLL_MS = 2400;
const AUTOPILOT_MS = 3600;
const MAX_LOG_LINES = 500;
const LOG_FLUSH_MS = 120;
const MAX_EVENTS_PER_POLL = 80;
const BLOCKS = ["Market", "Lab", "Arena", "CouncilHall"];

const ENTRY_GATE_ABI = [
  "function entryFeeWei() view returns (uint256)",
  "function payEntry(bytes32 agentId, bytes32 sessionKey) payable"
];

const MONAD_TESTNET = {
  chainId: "0x279f",
  chainName: "Monad Testnet",
  nativeCurrency: { name: "MON", symbol: "MON", decimals: 18 },
  rpcUrls: ["https://testnet-rpc.monad.xyz"],
  blockExplorerUrls: ["https://testnet.monadexplorer.com/"]
};

const state = {
  sinceEventId: 0,
  pollTimer: null,
  autoPilot: false,
  autoPilotTimer: null,
  worldInfo: null,
  summary: null,
  logQueue: [],
  logLineCount: 0,
  logFlushTimer: null,
  localLoginBlocked: false
};

const el = (id) => document.getElementById(id);

function nowStamp() {
  return new Date().toLocaleTimeString();
}

function clip(s, n = 150) {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}...` : s;
}

function randomBytes32Hex() {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return "0x" + Array.from(b).map((x) => x.toString(16).padStart(2, "0")).join("");
}

function agentIdToBytes32(agentStr) {
  return ethers.keccak256(ethers.toUtf8Bytes(agentStr));
}

function getAgentId() {
  return el("agentId").value.trim() || "agent_conspiracist";
}

function getToken() {
  return el("token").value.trim();
}

function hasWallet() {
  return typeof window.ethereum !== "undefined";
}

function setChip(id, text, tone = "dim") {
  const node = el(id);
  node.textContent = text;
  node.classList.remove("chip--dim", "chip--ok", "chip--warn");
  if (tone === "ok") node.classList.add("chip--ok");
  else if (tone === "warn") node.classList.add("chip--warn");
  else node.classList.add("chip--dim");
}

function setToken(token) {
  const value = token || "";
  el("token").value = value;
  localStorage.setItem("rumor_token", value);
  setChip("chipAuth", value ? `AUTH: ${value.slice(0, 12)}...` : "AUTH: NONE", value ? "ok" : "dim");
}

function logLine(type, message, tone = "INFO") {
  const line = `[${nowStamp()}] ${type} ${tone} :: ${String(message || "")}\n`;
  state.logQueue.push(line);
  if (state.logFlushTimer) return;
  state.logFlushTimer = setTimeout(flushLogQueue, LOG_FLUSH_MS);
}

function flushLogQueue() {
  state.logFlushTimer = null;
  if (!state.logQueue.length) return;

  const log = el("log");
  const chunk = state.logQueue.join("");
  state.logQueue = [];

  const shouldStick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.textContent += chunk;
  state.logLineCount += chunk.split("\n").length - 1;

  if (state.logLineCount > MAX_LOG_LINES + 50) {
    const kept = log.textContent.split("\n").slice(-MAX_LOG_LINES);
    log.textContent = kept.join("\n");
    state.logLineCount = kept.length;
  }

  if (shouldStick) {
    log.scrollTop = log.scrollHeight;
  }
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

function extractApiErrorDetail(raw) {
  const text = String(raw || "");
  const firstBrace = text.indexOf("{");
  if (firstBrace === -1) return "";
  try {
    const parsed = JSON.parse(text.slice(firstBrace));
    return String(parsed?.detail || "");
  } catch {
    return "";
  }
}

function explainVerifyError(rawErr) {
  const detail = extractApiErrorDetail(rawErr);
  if (!detail) return String(rawErr || "verify_failed");

  if (detail === "tx_hash_already_used") {
    return "Tx hash already used. This entry was already verified before.";
  }
  if (detail === "tx_not_found_or_not_final") {
    return "Tx not final yet. Wait until it is mined/finalized, then retry.";
  }
  if (detail === "no_entrypaid_event") {
    return "Tx does not contain EntryPaid event for configured contract.";
  }
  if (detail === "tx_payer_mismatch" || detail === "signature_payer_mismatch" || detail === "challenge_payer_mismatch") {
    return "Connected wallet must be the same address that paid the entry tx.";
  }
  if (detail === "invalid_tx_hash") {
    return "Invalid tx hash format.";
  }
  if (detail === "missing_signature" || detail === "missing_challenge_id" || detail === "missing_payer") {
    return "Verification now requires wallet signature. Connect the payer wallet and retry.";
  }
  return `verify failed: ${detail}`;
}

function explainAuthError(rawErr) {
  const detail = extractApiErrorDetail(rawErr);
  if (!detail) return String(rawErr || "auth_failed");
  if (detail === "local_auth_disabled") {
    return "Local quick login disabled on server. Use Auto Join + Verify or set LOCAL_AUTH_ENABLED=1.";
  }
  if (detail === "bad_local_secret") {
    return "Local quick login secret mismatch. Set LOCAL_AUTH_SECRET correctly.";
  }
  if (detail === "invalid_session_token" || detail === "session_expired") {
    return "Session expired/invalid. Please login again.";
  }
  if (detail === "missing_authorization" || detail === "invalid_authorization") {
    return "Bearer token missing or malformed.";
  }
  return `auth failed: ${detail}`;
}

async function getSigner() {
  if (!hasWallet()) throw new Error("No wallet found.");
  const provider = new ethers.BrowserProvider(window.ethereum);
  return provider.getSigner();
}

async function refreshWalletChain() {
  if (!hasWallet()) {
    el("walletChain").value = "";
    return;
  }
  const chainId = await window.ethereum.request({ method: "eth_chainId" });
  el("walletChain").value = chainId;
  const isMonad = chainId.toLowerCase() === MONAD_TESTNET.chainId.toLowerCase();
  logLine("CHAIN", isMonad ? "connected to Monad testnet" : `on ${chainId}; Monad required`, isMonad ? "OK" : "WARN");
}

async function probeWalletSilently() {
  if (!hasWallet()) return "";
  try {
    const accounts = await window.ethereum.request({ method: "eth_accounts" });
    const addr = accounts?.[0] || "";
    el("walletAddr").value = addr;
    if (addr) logLine("WALLET", `detected ${addr.slice(0, 10)}...`, "OK");
    return addr;
  } catch {
    return "";
  }
}

async function connectWallet() {
  if (!hasWallet()) {
    logLine("WALLET", "No injected wallet found.", "ERR");
    return "";
  }
  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    const addr = accounts?.[0] || "";
    el("walletAddr").value = addr;
    if (addr) logLine("WALLET", `connected ${addr.slice(0, 10)}...`, "OK");
    await refreshWalletChain();
    return addr;
  } catch (e) {
    logLine("WALLET_FAIL", String(e?.message || e), "ERR");
    return "";
  }
}

async function switchToMonad() {
  if (!hasWallet()) {
    logLine("CHAIN", "Wallet not available.", "ERR");
    return false;
  }

  try {
    await window.ethereum.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: MONAD_TESTNET.chainId }]
    });
    await refreshWalletChain();
    return true;
  } catch (e) {
    if (e?.code === 4902) {
      try {
        await window.ethereum.request({
          method: "wallet_addEthereumChain",
          params: [MONAD_TESTNET]
        });
        await window.ethereum.request({
          method: "wallet_switchEthereumChain",
          params: [{ chainId: MONAD_TESTNET.chainId }]
        });
        await refreshWalletChain();
        return true;
      } catch (inner) {
        logLine("CHAIN_FAIL", String(inner?.message || inner), "ERR");
        return false;
      }
    }
    logLine("CHAIN_FAIL", String(e?.message || e), "ERR");
    return false;
  }
}

function ensureSessionKey() {
  const key = el("sessionKey").value.trim();
  if (key.startsWith("0x") && key.length === 66) return key;
  const generated = randomBytes32Hex();
  el("sessionKey").value = generated;
  return generated;
}

async function loadWorldConfig() {
  try {
    const info = await apiGet("/v1/world");
    state.worldInfo = info;
    if (info.entry_contract) el("entryContract").value = info.entry_contract;
    if (info.entry_fee_mon != null) el("entryFeeMon").value = String(info.entry_fee_mon);
    logLine("WORLD", "world config loaded", "OK");
  } catch (e) {
    logLine("WORLD_FAIL", String(e?.message || e), "WARN");
  }
}

async function fetchWhoAmI() {
  const token = getToken();
  if (!token) return null;
  try {
    const me = await apiGet("/v1/auth/whoami", token);
    if (me?.mode === "session" && me.agent_id) el("agentId").value = me.agent_id;
    logLine("WHOAMI", `mode=${me.mode} agent=${me.agent_id || "-"}`, "OK");
    return me;
  } catch (e) {
    setToken("");
    logLine("WHOAMI_FAIL", "token invalid, cleared local token", "WARN");
    return null;
  }
}

async function quickLocalLogin(silent = false) {
  if (state.localLoginBlocked) return false;
  try {
    const res = await apiPost("/v1/auth/local-login", { agent_id: getAgentId() }, null);
    setToken(res.access_token);
    await fetchWhoAmI();
    if (!silent) logLine("AUTH", "quick login success", "OK");
    return true;
  } catch (e) {
    const message = explainAuthError(String(e?.message || e));
    if (message.includes("disabled") || message.includes("secret mismatch")) {
      state.localLoginBlocked = true;
    }
    if (!silent) logLine("AUTH_FAIL", message, "ERR");
    return false;
  }
}

async function verifyEntryWithWallet(txHash) {
  const signer = await getSigner();
  const payer = await signer.getAddress();
  const challenge = await apiPost("/v1/auth/challenge", { payer }, null);
  const signature = await signer.signMessage(challenge.message);
  const res = await apiPost("/v1/auth/verify-entry", {
    tx_hash: txHash,
    payer,
    challenge_id: challenge.challenge_id,
    signature
  }, null);
  setToken(res.access_token);
  await fetchWhoAmI();
  logLine("AUTH", `session minted, expires=${res.expires_at_unix}`, "OK");
}

async function verifyExistingTx() {
  const txHash = el("txHash").value.trim();
  if (!txHash) return logLine("VERIFY", "tx hash is required", "WARN");

  try {
    if (!hasWallet()) {
      logLine("VERIFY", "Wallet required: connect the same payer wallet used for this tx.", "WARN");
      return;
    }
    const addr = await connectWallet();
    if (!addr) {
      logLine("VERIFY", "Wallet is not connected.", "WARN");
      return;
    }
    await verifyEntryWithWallet(txHash);
  } catch (e) {
    logLine("VERIFY_FAIL", explainVerifyError(String(e?.message || e)), "ERR");
  }
}

async function payEntryOnChain() {
  const addr = el("entryContract").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    throw new Error("Entry contract address invalid.");
  }

  const signer = await getSigner();
  ensureSessionKey();
  const sessionKey = el("sessionKey").value.trim();
  const agentHash = agentIdToBytes32(getAgentId());
  const contract = new ethers.Contract(addr, ENTRY_GATE_ABI, signer);

  let feeWei;
  try {
    feeWei = await contract.entryFeeWei();
    el("entryFeeMon").value = ethers.formatEther(feeWei);
  } catch {
    feeWei = ethers.parseEther(el("entryFeeMon").value.trim() || "0.001");
  }

  logLine("PAY", `payEntry ${ethers.formatEther(feeWei)} MON`, "WARN");
  const tx = await contract.payEntry(agentHash, sessionKey, { value: feeWei });
  el("txHash").value = tx.hash;
  logLine("TX_SENT", tx.hash, "OK");
  await tx.wait();
  logLine("TX_MINED", "entry payment confirmed", "OK");
  return tx.hash;
}

async function autoJoinAndVerify() {
  const btn = el("btnAutoJoin");
  btn.disabled = true;
  try {
    await loadWorldConfig();
    const addr = await connectWallet();
    if (!addr) throw new Error("Wallet is not connected.");
    const switched = await switchToMonad();
    if (!switched) throw new Error("Could not switch to Monad.");
    const txHash = await payEntryOnChain();
    await verifyEntryWithWallet(txHash);
    await refreshNow();
    logLine("READY", "onboarding complete", "OK");
  } catch (e) {
    logLine("AUTO_JOIN_FAIL", String(e?.message || e), "ERR");
  } finally {
    btn.disabled = false;
  }
}

function topRumorId() {
  return state.summary?.top_rumors?.[0]?.rumor_id || "";
}

function randomSeedAction() {
  const claim = el("seedClaim").value.trim();
  if (claim) return { type: "seed_rumor", claim, effects: {} };

  const defaults = [
    "Market whispers: ore stockpile is fake.",
    "Lab notes leaked: debunks are manipulated.",
    "Arena stream boosts rumor engagement by design.",
    "Council memo: price ceilings are staged theater."
  ];
  return {
    type: "seed_rumor",
    claim: defaults[Math.floor(Math.random() * defaults.length)],
    effects: {}
  };
}

function rumorTargetOrTop() {
  return el("rumorId").value.trim() || topRumorId();
}

async function submitActions(actions) {
  let token = getToken();
  if (!token) {
    const quick = await quickLocalLogin(true);
    if (!quick) {
      logLine("AUTH", "No token. Click Quick Login or Auto Join + Verify.", "WARN");
      return false;
    }
    token = getToken();
  }

  const body = {
    agent_id: getAgentId(),
    tick_submitted: 0,
    actions
  };

  try {
    const out = await apiPost("/v1/actions", body, token);
    logLine("ACTION_OK", JSON.stringify(out), "OK");
    return true;
  } catch (e) {
    const raw = String(e?.message || e);
    const detail = extractApiErrorDetail(raw);
    if (detail === "missing_authorization" || detail === "invalid_authorization" || detail === "invalid_session_token" || detail === "session_expired") {
      setToken("");
      const quick = await quickLocalLogin(true);
      if (quick) {
        try {
          const retry = await apiPost("/v1/actions", body, getToken());
          logLine("ACTION_OK", JSON.stringify(retry), "OK");
          return true;
        } catch (e2) {
          logLine("ACTION_FAIL", String(e2?.message || e2), "ERR");
          return false;
        }
      }
    }
    logLine("ACTION_FAIL", raw, "ERR");
    return false;
  }
}

async function actionMove(to) {
  if (!BLOCKS.includes(to)) return;
  await submitActions([{ type: "move", to }]);
}

async function actionSpread() {
  const rumorId = rumorTargetOrTop();
  if (!rumorId) return logLine("SPREAD", "no rumor id available", "WARN");
  const effort = Math.max(1, Math.min(8, parseInt(el("spreadEffort").value.trim() || "3", 10)));
  await submitActions([{ type: "spread_rumor", rumor_id: rumorId, effort }]);
}

async function actionInvestigate() {
  const rumorId = rumorTargetOrTop();
  if (!rumorId) return logLine("INVESTIGATE", "no rumor id available", "WARN");
  await submitActions([{ type: "investigate_rumor", rumor_id: rumorId }]);
}

async function actionTradeSentiment() {
  const rumorId = rumorTargetOrTop();
  if (!rumorId) return logLine("TRADE", "no rumor id available", "WARN");
  await submitActions([{ type: "endorse_belief", rumor_id: rumorId }]);
}

async function actionSeed() {
  await submitActions([randomSeedAction()]);
}

async function actionRest() {
  await submitActions([{ type: "rest" }]);
}

function currentAgent() {
  if (!state.summary?.agents) return null;
  return state.summary.agents[getAgentId()] || null;
}

function blockScores() {
  const agents = state.summary?.agents || {};
  const scores = {
    Market: { score: 0, count: 0 },
    Lab: { score: 0, count: 0 },
    Arena: { score: 0, count: 0 },
    CouncilHall: { score: 0, count: 0 }
  };

  for (const a of Object.values(agents)) {
    const loc = a.location;
    if (!scores[loc]) continue;
    const rep = Number(a.reputation || 0);
    const credits = Number(a.credits || 0);
    scores[loc].score += 1 + rep + credits * 0.15;
    scores[loc].count += 1;
  }
  return scores;
}

function bestBlockFromScores(scores) {
  let best = "Market";
  let bestValue = -Infinity;
  for (const b of BLOCKS) {
    const v = scores[b]?.score ?? 0;
    if (v > bestValue) {
      bestValue = v;
      best = b;
    }
  }
  return best;
}

async function moveToBestBlock() {
  const scores = blockScores();
  const best = bestBlockFromScores(scores);
  await actionMove(best);
}

async function runCityLoop() {
  const me = currentAgent();
  const location = me?.location || "";
  const rumorId = rumorTargetOrTop();

  if (!BLOCKS.includes(location)) {
    await actionMove("Market");
    return;
  }

  if (!rumorId) {
    await actionSeed();
    return;
  }

  if (location === "Lab") {
    await actionInvestigate();
    return;
  }
  if (location === "Arena") {
    await actionTradeSentiment();
    return;
  }
  if (location === "CouncilHall") {
    await actionSpread();
    return;
  }

  if (Math.random() < 0.32) {
    await moveToBestBlock();
    return;
  }
  await actionSpread();
}

function setAutopilot(on) {
  state.autoPilot = !!on;
  if (state.autoPilotTimer) {
    clearInterval(state.autoPilotTimer);
    state.autoPilotTimer = null;
  }
  if (state.autoPilot) {
    state.autoPilotTimer = setInterval(() => {
      runCityLoop();
    }, AUTOPILOT_MS);
  }
  el("btnAutoPilot").textContent = `Autopilot: ${state.autoPilot ? "ON" : "OFF"}`;
  setChip("chipAuto", `AUTO: ${state.autoPilot ? "ON" : "OFF"}`, state.autoPilot ? "ok" : "dim");
}

function setBlockCardActive(location) {
  const mapping = {
    Market: "blockMarket",
    Lab: "blockLab",
    Arena: "blockArena",
    CouncilHall: "blockCouncil"
  };
  for (const id of Object.values(mapping)) {
    el(id).classList.remove("active");
  }
  if (mapping[location]) {
    el(mapping[location]).classList.add("active");
  }
}

function renderSummary(s) {
  state.summary = s;
  el("tick").textContent = String(s.tick ?? "-");

  const top = s.top_rumors?.[0];
  const topText = top
    ? `${top.rumor_id} belief=${top.belief} spread=${top.spread_count} debunk=${top.debunk_count}\n${clip(top.claim, 140)}`
    : "-";
  el("topRumor").textContent = topText;

  if (top?.rumor_id && !el("rumorId").value.trim()) {
    el("rumorId").value = top.rumor_id;
  }

  el("market").textContent = JSON.stringify(s.market_prices || {}, null, 0);

  const me = currentAgent();
  el("myLocation").textContent = me?.location || "-";
  el("myRep").textContent = String(me?.reputation ?? "-");
  el("myCredits").textContent = String(me?.credits ?? "-");
  setBlockCardActive(me?.location || "");

  const scores = blockScores();
  el("scoreMarket").textContent = String(Math.round(scores.Market.score));
  el("scoreLab").textContent = String(Math.round(scores.Lab.score));
  el("scoreArena").textContent = String(Math.round(scores.Arena.score));
  el("scoreCouncil").textContent = String(Math.round(scores.CouncilHall.score));
  el("countMarket").textContent = String(scores.Market.count);
  el("countLab").textContent = String(scores.Lab.count);
  el("countArena").textContent = String(scores.Arena.count);
  el("countCouncil").textContent = String(scores.CouncilHall.count);

  const agentLines = Object.entries(s.agents || {})
    .slice(0, 14)
    .map(([id, a]) =>
      `${id.padEnd(18)} loc=${String(a.location || "-").padEnd(12)} rep=${String(a.reputation).padEnd(3)} sta=${String(a.stamina).padEnd(3)} cr=${String(a.credits).padEnd(4)} act=${String(a.recent_action || "-")}`
    );
  el("agents").textContent = agentLines.length ? agentLines.join("\n") : "(none)";

  const rumorLines = (s.top_rumors || [])
    .slice(0, 8)
    .map((r) => `${r.rumor_id}  belief=${r.belief} spread=${r.spread_count} debunk=${r.debunk_count}\n${clip(r.claim, 140)}`);
  el("rumors").textContent = rumorLines.length ? rumorLines.join("\n\n") : "(none)";
}

function toneForEvent(t) {
  if (t.includes("debunk") || t.includes("investigate")) return "OK";
  if (t.includes("rejected") || t.includes("invalid") || t.includes("failed")) return "ERR";
  if (t.includes("threshold") || t.includes("epoch") || t.includes("microverse")) return "WARN";
  return "INFO";
}

function handleEvent(ev) {
  const payload = ev.payload || {};
  if (ev.type === "rumor_created" && payload.rumor_id && !el("rumorId").value.trim()) {
    el("rumorId").value = payload.rumor_id;
  }
  logLine(ev.type, clip(JSON.stringify(payload), 220), toneForEvent(ev.type));
}

async function pollSummary() {
  try {
    const s = await apiGet("/v1/summary");
    renderSummary(s);
    setChip("chipConn", "ONLINE", "ok");
  } catch {
    setChip("chipConn", "OFFLINE", "warn");
  }
}

async function pollEvents() {
  try {
    const s = await apiGet(`/v1/state?since_event_id=${state.sinceEventId}`);
    const events = s.events || [];
    const scopedEvents = events.length > MAX_EVENTS_PER_POLL ? events.slice(-MAX_EVENTS_PER_POLL) : events;

    if (events.length > MAX_EVENTS_PER_POLL) {
      logLine("EVENTS", `throttled ${events.length - MAX_EVENTS_PER_POLL} old events`, "WARN");
    }

    if (scopedEvents.length) {
      for (const ev of scopedEvents) {
        handleEvent(ev);
        if (typeof ev.event_id === "number") {
          state.sinceEventId = Math.max(state.sinceEventId, ev.event_id);
        }
      }
    } else if (typeof s.latest_event_id === "number") {
      state.sinceEventId = Math.max(state.sinceEventId, s.latest_event_id);
    }

    el("since").textContent = String(state.sinceEventId);
    el("latest").textContent = String(s.latest_event_id ?? "-");
    setChip("chipConn", "ONLINE", "ok");
  } catch {
    setChip("chipConn", "OFFLINE", "warn");
  }
}

async function refreshNow() {
  await Promise.all([pollSummary(), pollEvents()]);
}

function bindActions() {
  el("btnConnect").addEventListener("click", connectWallet);
  el("btnSwitchMonad").addEventListener("click", switchToMonad);
  el("btnAutoJoin").addEventListener("click", autoJoinAndVerify);
  el("btnVerify").addEventListener("click", verifyExistingTx);
  el("btnGenSessionKey").addEventListener("click", () => {
    el("sessionKey").value = randomBytes32Hex();
    logLine("SESSION", "generated session key", "OK");
  });

  el("btnSaveToken").addEventListener("click", async () => {
    setToken(getToken());
    await fetchWhoAmI();
    logLine("AUTH", "token saved", "OK");
  });
  el("btnQuickLogin").addEventListener("click", async () => {
    await quickLocalLogin(false);
  });
  el("btnClearToken").addEventListener("click", () => {
    setToken("");
    logLine("AUTH", "token cleared", "WARN");
  });

  el("btnSpread").addEventListener("click", actionSpread);
  el("btnInvestigate").addEventListener("click", actionInvestigate);
  el("btnTrade").addEventListener("click", actionTradeSentiment);
  el("btnSeedRumor").addEventListener("click", actionSeed);
  el("btnMoveBest").addEventListener("click", moveToBestBlock);
  el("btnRest").addEventListener("click", actionRest);
  el("btnSmartAction").addEventListener("click", runCityLoop);
  el("btnAutoPilot").addEventListener("click", () => setAutopilot(!state.autoPilot));
  el("btnRefreshNow").addEventListener("click", refreshNow);

  document.querySelectorAll("[data-move]").forEach((button) => {
    button.addEventListener("click", async () => {
      const to = button.getAttribute("data-move");
      await actionMove(to);
    });
  });

  if (hasWallet()) {
    window.ethereum.on?.("accountsChanged", (accounts) => {
      el("walletAddr").value = accounts?.[0] || "";
      logLine("WALLET", "accounts changed", "WARN");
    });
    window.ethereum.on?.("chainChanged", () => {
      refreshWalletChain();
      logLine("CHAIN", "network changed", "WARN");
    });
  }
}

async function boot() {
  bindActions();
  ensureSessionKey();
  setToken(localStorage.getItem("rumor_token") || "");
  setAutopilot(false);

  logLine("BOOT", "starting city blocks console", "OK");
  await loadWorldConfig();
  await probeWalletSilently();
  await refreshWalletChain().catch(() => {});
  if (getToken()) await fetchWhoAmI();
  if (!getToken()) await quickLocalLogin(true);
  await refreshNow();

  state.pollTimer = setInterval(() => {
    refreshNow();
  }, POLL_MS);

  logLine("READY", "Use Run City Loop or enable Autopilot", "OK");
}

boot();
