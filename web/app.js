const API = ""; // same origin

const ENTRY_GATE_ABI = [
  "function entryFeeWei() view returns (uint256)",
  "function ttlSeconds() view returns (uint256)",
  "function payEntry(bytes32 agentId, bytes32 sessionKey) payable",
  "event EntryPaid(bytes32 indexed agentId, bytes32 indexed sessionKey, address indexed payer, uint256 amountWei, uint256 expiresAt)"
];

const MAX_LOG_LINES = 600; // tweak

function randomBytes32Hex() {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return "0x" + Array.from(b).map(x => x.toString(16).padStart(2, "0")).join("");
}

function agentIdToBytes32(agentStr) {
  // bytes32 = keccak256(utf8(agentStr))
  return ethers.keccak256(ethers.toUtf8Bytes(agentStr));
}

let sinceEventId = 0;
let pollTimer = null;

const el = (id) => document.getElementById(id);

const MONAD_TESTNET = {
  chainId: "0x279f", // 10143
  chainName: "Monad Testnet",
  nativeCurrency: { name: "MON", symbol: "MON", decimals: 18 },
  rpcUrls: ["https://testnet-rpc.monad.xyz"],
  blockExplorerUrls: ["https://testnet.monadexplorer.com/"],
};

function hasWallet() {
  return typeof window.ethereum !== "undefined";
}

async function connectWallet() {
  if (!hasWallet()) {
    logLine("WALLET", "No injected wallet found. Install MetaMask.", "bad");
    return;
  }
  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    const addr = accounts?.[0] || "";
    el("walletAddr").value = addr;
    logLine("WALLET", `connected ${addr.slice(0, 10)}…`, "ok");
    await refreshWalletChain();
  } catch (e) {
    logLine("WALLET_FAIL", String(e?.message || e), "bad");
  }
}

async function refreshWalletChain() {
  if (!hasWallet()) return;
  try {
    const chainId = await window.ethereum.request({ method: "eth_chainId" });
    el("walletChain").value = chainId;
    const isMonad = chainId?.toLowerCase() === MONAD_TESTNET.chainId.toLowerCase();
    logLine("CHAIN", isMonad ? "on Monad testnet" : `on ${chainId} (not Monad)`, isMonad ? "ok" : "warn");
  } catch (e) {}
}

async function switchToMonad() {
  if (!hasWallet()) {
    logLine("WALLET", "No injected wallet found. Install MetaMask.", "bad");
    return;
  }
  try {
    await window.ethereum.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: MONAD_TESTNET.chainId }],
    });
    logLine("CHAIN", "switched to Monad testnet", "ok");
    await refreshWalletChain();
  } catch (e) {
    // 4902 = unknown chain, so add it
    const code = e?.code;
    if (code === 4902) {
      try {
        await window.ethereum.request({
          method: "wallet_addEthereumChain",
          params: [MONAD_TESTNET],
        });
        logLine("CHAIN", "Monad testnet added", "ok");
        await switchToMonad();
      } catch (e2) {
        logLine("CHAIN_FAIL", String(e2?.message || e2), "bad");
      }
    } else {
      logLine("CHAIN_FAIL", String(e?.message || e), "bad");
    }
  }
}

async function getSigner() {
  if (!hasWallet()) throw new Error("No injected wallet found (MetaMask).");
  const provider = new ethers.BrowserProvider(window.ethereum);
  return await provider.getSigner();
}

async function loadWorldConfig() {
  try {
    const info = await apiGet("/v1/world");
    if (info.entry_contract) el("entryContract").value = info.entry_contract;
    if (info.entry_fee_mon != null) el("entryFeeMon").value = String(info.entry_fee_mon);
    logLine("WORLD", `contract=${(info.entry_contract||"").slice(0,10)}… fee=${info.entry_fee_mon} MON`, "ok");
  } catch (e) {
    // fallback defaults if API not available
    if (!el("entryFeeMon").value) el("entryFeeMon").value = "0.001";
  }
}

async function payEntryOnChain() {
  try {
    if (!hasWallet()) return logLine("WALLET", "Install MetaMask first.", "bad");

    // ensure connected
    await connectWallet();

    // ensure monad testnet
    await switchToMonad();

    const signer = await getSigner();
    const addr = el("entryContract").value.trim();
    if (!addr || !addr.startsWith("0x") || addr.length !== 42) {
      return logLine("PAY", "Invalid Entry Contract address.", "bad");
    }

    // sessionKey
    let sessionKey = el("sessionKey").value.trim();
    if (!sessionKey || sessionKey.length !== 66) {
      sessionKey = randomBytes32Hex();
      el("sessionKey").value = sessionKey;
      logLine("SESSION_KEY", "generated new session key", "ok");
    }

    const agentStr = getAgentId();
    const agentIdHash = agentIdToBytes32(agentStr);

    const contract = new ethers.Contract(addr, ENTRY_GATE_ABI, signer);

    // fee: prefer contract.entryFeeWei(), fallback to UI
    let feeWei;
    try {
      feeWei = await contract.entryFeeWei();
      // also update UI fee MON
      el("entryFeeMon").value = ethers.formatEther(feeWei);
    } catch {
      const feeMon = (el("entryFeeMon").value.trim() || "0.001");
      feeWei = ethers.parseEther(feeMon);
    }

    logLine("PAY", `sending tx payEntry(agent=${agentStr}, fee=${ethers.formatEther(feeWei)} MON)…`, "warn");

    const tx = await contract.payEntry(agentIdHash, sessionKey, { value: feeWei });
    logLine("TX_SENT", tx.hash, "ok");

    // auto-fill tx hash for verify
    el("txHash").value = tx.hash;

    // Optional: wait for confirmation
    const receipt = await tx.wait();
    logLine("TX_MINED", `block=${receipt.blockNumber}`, "ok");

    logLine("NEXT", "Click VERIFY ENTRY to mint session token.", "ok");
  } catch (e) {
    logLine("PAY_FAIL", String(e?.message || e), "bad");
  }
}

function setPill(id, text, tone="dim") {
  const p = el(id);
  p.textContent = text;
  p.classList.remove("pill--dim");
  if (tone === "dim") p.classList.add("pill--dim");
}

function nowStamp() {
  const d = new Date();
  return d.toLocaleTimeString();
}

function clip(s, n=120) {
  if (!s) return "";
  return s.length > n ? s.slice(0,n-1) + "…" : s;
}

function logLine(type, msg, tone="t") {
  const line = document.createElement("div");
  line.className = "line";
  line.innerHTML = `<span class="t">[${nowStamp()}]</span> <span class="ty">${type}</span> <span class="${tone}">${msg}</span>`;
  const log = el("log");
  log.appendChild(line);

  // ✅ prune old lines
  while (log.children.length > MAX_LOG_LINES) {
    log.removeChild(log.firstChild);
  }

  log.scrollTop = log.scrollHeight;
}

function getToken() {
  return el("token").value.trim();
}

function setToken(tok) {
  el("token").value = tok || "";
  localStorage.setItem("rumor_token", tok || "");
  setPill("pill-auth", tok ? `AUTH: OK (${tok.slice(0,12)}…)` : "AUTH: NONE", tok ? "ok" : "dim");
}

function getAgentId() {
  return el("agentId").value.trim() || "agent_conspiracist";
}

async function apiGet(path) {
  const r = await fetch(API + path, { headers: { "Accept":"application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function apiPost(path, body, token=null) {
  const headers = { "Content-Type":"application/json", "Accept":"application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(API + path, { method:"POST", headers, body: JSON.stringify(body) });
  const txt = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${txt}`);
  return txt ? JSON.parse(txt) : {};
}

function renderSummary(s) {
  el("tick").textContent = s.tick ?? "—";
  el("market").textContent = JSON.stringify(s.market_prices ?? s.snapshot?.market?.prices ?? {}, null, 0);

  const top = (s.top_rumors && s.top_rumors[0]) ? s.top_rumors[0] : null;
  el("topRumor").textContent = top ? `${top.belief} :: ${clip(top.claim, 80)}` : "—";

  // Agents
  const agents = s.agents || {};
  const aLines = Object.entries(agents).map(([id,a]) =>
    `${id.padEnd(18)} loc=${String(a.location).padEnd(12)} rep=${String(a.reputation).padEnd(4)} sta=${String(a.stamina).padEnd(4)} cr=${String(a.credits ?? "").padEnd(5)}`
  );
  el("agents").textContent = aLines.length ? aLines.join("\n") : "(none)";

  // Rumors
  const rLines = (s.top_rumors || []).map(r => {
    const flags = r.flags ? JSON.stringify(r.flags) : "{}";
    const eff = r.effects ? JSON.stringify(r.effects) : "{}";
    return `${r.rumor_id}  belief=${r.belief}  flags=${flags}\n  ${clip(r.claim, 140)}\n  effects=${eff}\n`;
  });
  el("rumors").textContent = rLines.length ? rLines.join("\n") : "(none)";
}

async function pollSummary() {
  try {
    const s = await apiGet("/v1/summary");
    renderSummary(s);
    setPill("pill-conn", "ONLINE", "ok");
  } catch (e) {
    setPill("pill-conn", "OFFLINE", "bad");
  }
}

async function pollEvents() {
  try {
    const s = await apiGet(`/v1/state?since_event_id=${sinceEventId}`);
    el("since").textContent = String(sinceEventId);
    el("latest").textContent = String(s.latest_event_id ?? "—");

    const events = s.events || [];
    if (events.length) {
      for (const ev of events) {
        const payload = ev.payload ? JSON.stringify(ev.payload) : "{}";
        const tone =
          ev.type === "REALITY_SHIFT" ? "hot" :
          ev.type.includes("threshold") ? "warn" :
          ev.type.includes("debunk") ? "ok" :
          ev.type.includes("caught") ? "bad" : "t";
        logLine(ev.type, clip(payload, 220), tone);
        sinceEventId = Math.max(sinceEventId, ev.event_id + 1);
      }
    } else {
      // still advance to avoid re-reading old windows if server returns latest id
      if (typeof s.latest_event_id === "number") {
        sinceEventId = Math.max(sinceEventId, s.latest_event_id);
      }
    }
    setPill("pill-conn", "ONLINE", "ok");
  } catch (e) {
    setPill("pill-conn", "OFFLINE", "bad");
  }
}

async function verifyEntry() {
  const tx = el("txHash").value.trim();
  if (!tx) return logLine("WARN", "Paste a tx hash first.", "warn");

  try {
    const res = await apiPost("/v1/auth/verify-entry", { tx_hash: tx }, null);
    setToken(res.access_token);
    logLine("AUTH", `verified tx → token ${res.access_token.slice(0,14)}… exp=${res.expires_at_unix}`, "ok");
  } catch (e) {
    logLine("AUTH_FAIL", e.message, "bad");
  }
}

async function submitActions(actions) {
  const token = getToken();
  if (!token) return logLine("AUTH", "No token. Verify entry first.", "warn");

  try {
    const body = { agent_id: getAgentId(), tick_submitted: 0, actions };
    const res = await apiPost("/v1/actions", body, token);
    logLine("ACTION_OK", JSON.stringify(res), "ok");
  } catch (e) {
    logLine("ACTION_FAIL", e.message, "bad");
  }
}

function bindUI() {
  // restore token
  const saved = localStorage.getItem("rumor_token") || "";
  if (saved) setToken(saved);

  el("btnVerify").addEventListener("click", verifyEntry);
  el("btnConnect").addEventListener("click", connectWallet);
  el("btnSwitchMonad").addEventListener("click", switchToMonad);

  if (hasWallet()) {
  window.ethereum.on?.("accountsChanged", (accs) => {
    el("walletAddr").value = accs?.[0] || "";
    logLine("WALLET", "accounts changed", "warn");
  });
  window.ethereum.on?.("chainChanged", () => {
    refreshWalletChain();
    logLine("CHAIN", "chain changed", "warn");
  });
  refreshWalletChain();
}

  el("btnGenSessionKey").addEventListener("click", () => {
	el("sessionKey").value = randomBytes32Hex();
	logLine("SESSION_KEY", "new session key generated", "ok");
  });

  el("btnPayEntry").addEventListener("click", payEntryOnChain);

  el("btnSaveToken").addEventListener("click", () => {
    setToken(getToken());
    logLine("AUTH", "token saved to localStorage", "ok");
  });

  el("btnClearToken").addEventListener("click", () => {
    setToken("");
    logLine("AUTH", "token cleared", "warn");
  });

  document.querySelectorAll("[data-move]").forEach(btn => {
    btn.addEventListener("click", () => {
      submitActions([{ type: "move", to: btn.getAttribute("data-move") }]);
    });
  });

  el("btnSpread").addEventListener("click", () => {
    const rumorId = el("spreadRumorId").value.trim();
    const effort = parseInt(el("spreadEffort").value.trim() || "3", 10);
    if (!rumorId) return logLine("WARN", "Enter rumor id to spread.", "warn");
    submitActions([{ type: "spread_rumor", rumor_id: rumorId, effort }]);
  });

  el("btnDebunk").addEventListener("click", () => {
    const rumorId = el("debunkRumorId").value.trim();
    if (!rumorId) return logLine("WARN", "Enter rumor id to debunk.", "warn");
    submitActions([{ type: "debunk_rumor", rumor_id: rumorId }]);
  });
}

async function boot() {
  bindUI();
  logLine("BOOT", "terminal online. polling summary + events…", "ok");
  await pollSummary();
  await pollEvents();
  await loadWorldConfig();

  pollTimer = setInterval(async () => {
    await pollSummary();
    await pollEvents();
  }, 1500);
}

boot();
