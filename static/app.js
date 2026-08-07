"use strict";

let logCursor = 0;
let lastState = null;
let settingsTouched = false;
let dropdownOpen = false;
let toastTimer = null;

// which account card is showing -- UI-only, independent of mining roles.
// Persisted so a page reload (Ctrl+R) keeps whatever you last switched
// to instead of falling back to "first wallet" every time.
const SELECTED_WALLET_STORAGE_KEY = "primewallet:selected-wallet";
let selectedWalletName = null;
try {
  selectedWalletName = localStorage.getItem(SELECTED_WALLET_STORAGE_KEY);
} catch (_) {
  // localStorage unavailable -- falls back to picking a wallet fresh each load
}

function setSelectedWallet(name) {
  selectedWalletName = name;
  try {
    if (name) localStorage.setItem(SELECTED_WALLET_STORAGE_KEY, name);
    else localStorage.removeItem(SELECTED_WALLET_STORAGE_KEY);
  } catch (_) {
    // ignore -- storage unavailable, in-memory value still works this session
  }
}

// -- tiny helpers ----------------------------------------------------

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try {
    body = await res.json();
  } catch (_) {
    // no JSON body
  }
  if (!res.ok) {
    const msg = (body && body.error) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body;
}

function showToast(message, isError) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  el.classList.toggle("err", !!isError);
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

function el(id) {
  return document.getElementById(id);
}

function show(id) { el(id).classList.remove("hidden"); }
function hide(id) { el(id).classList.add("hidden"); }
function setHidden(id, hidden) { el(id).classList.toggle("hidden", hidden); }

// deterministic color + initial from a name/address, so the same wallet
// always gets the same avatar without storing anything extra
function avatarStyleFor(seed) {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
  }
  const hue = hash % 360;
  return `background: hsl(${hue}, 55%, 45%)`;
}

function shortAddress(addr) {
  if (!addr) return "(address unreadable)";
  if (addr.length <= 22) return addr;
  return `${addr.slice(0, 12)}...${addr.slice(-6)}`;
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    return false;
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result; // "data:...;base64,AAAA..."
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error("could not read file"));
    reader.readAsDataURL(file);
  });
}

function onClick(id, handler) {
  el(id).addEventListener("click", async (ev) => {
    ev.preventDefault();
    const btn = ev.currentTarget;
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await handler();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      await refreshState().catch((e) => console.error(e));
    }
  });
}

function safeInterval(fn, ms) {
  setInterval(() => {
    fn().catch((err) => console.error(err));
  }, ms);
}

// -- wallet lookup helpers --------------------------------------------

function findWallet(state, name) {
  return state.wallets.find((w) => w.name === name) || null;
}

function pickSelectedWallet(state) {
  if (selectedWalletName && findWallet(state, selectedWalletName)) {
    return selectedWalletName;
  }
  const active = state.wallets.find(
    (w) => w.active_roles.includes("prime") || w.active_roles.includes("composite")
  );
  const fallback = active || state.wallets[0];
  setSelectedWallet(fallback ? fallback.name : null);
  return selectedWalletName;
}

// -- rendering ----------------------------------------------------------

function renderScreens(state) {
  const unlocked = state.config.unlocked;
  // three screens, mutually exclusive:
  //  - unlock: a wallet already exists on disk, just need the passphrase
  //  - setup:  nothing exists yet (or somehow unlocked with 0 wallets) --
  //            create a new one or import a backup .wallet file
  //  - main:   unlocked and at least one wallet is registered
  const showUnlock = !unlocked && state.has_any_wallet;
  const showSetup = (!unlocked && !state.has_any_wallet) || (unlocked && state.wallets.length === 0);
  const showMain = unlocked && state.wallets.length > 0;
  setHidden("unlock-screen", !showUnlock);
  setHidden("onboarding-screen", !showSetup);
  setHidden("main-screen", !showMain);
  setHidden("onboard-passphrase", unlocked); // already unlocked -- no need to ask again
  el("lock-btn").disabled = !unlocked;
}

function renderSettings(state) {
  if (!settingsTouched) {
    el("cfg-workdir").value = state.config.workdir;
    el("cfg-peer-host").value = state.config.peer_host;
    el("cfg-peer-port").value = state.config.peer_port;
    el("cfg-target").value = state.config.target;
  }
  el("cfg-bin-dir").textContent = state.config.bin_dir;
}

function renderAccountSwitcher(state, name) {
  const wallet = findWallet(state, name);
  el("account-name").textContent = wallet ? wallet.name : "no wallet";
  const avatar = el("account-avatar");
  avatar.textContent = wallet ? wallet.name.slice(0, 1).toUpperCase() : "?";
  avatar.setAttribute("style", avatarStyleFor(wallet ? wallet.name : "?"));
}

function renderDropdown(state) {
  const dd = el("account-dropdown");
  dd.innerHTML = "";
  const running = state.mining_running;
  for (const w of state.wallets) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "dropdown-item" + (w.name === selectedWalletName ? " selected" : "");
    const av = document.createElement("span");
    av.className = "avatar";
    av.textContent = w.name.slice(0, 1).toUpperCase();
    av.setAttribute("style", avatarStyleFor(w.name));
    const label = document.createElement("span");
    label.textContent = w.name;
    const roles = document.createElement("span");
    roles.className = "roles-mini";
    // One signal for "lit up" everywhere in the app: actually mining
    // that role right now, not just assigned to it. Only the wallet(s)
    // that both hold a role and are actually mining get a tag.
    for (const [role, letter] of [["prime", "P"], ["composite", "C"]]) {
      if (!w.active_roles.includes(role) || !running) continue;
      const badge = document.createElement("span");
      badge.className = "role-mini-badge on";
      badge.textContent = letter;
      badge.title = `${role} (active)`;
      roles.appendChild(badge);
    }
    item.appendChild(av);
    item.appendChild(label);
    item.appendChild(roles);
    item.addEventListener("click", () => {
      setSelectedWallet(w.name);
      closeDropdown();
      renderAll(lastState);
    });
    dd.appendChild(item);
  }
  const sep = document.createElement("div");
  sep.className = "dropdown-sep";
  dd.appendChild(sep);
  const createItem = document.createElement("button");
  createItem.type = "button";
  createItem.className = "dropdown-item create-item";
  createItem.textContent = "+ Create new wallet";
  createItem.addEventListener("click", () => {
    closeDropdown();
    openCreateModal();
  });
  dd.appendChild(createItem);
}

function openDropdown() {
  dropdownOpen = true;
  show("account-dropdown");
}
function closeDropdown() {
  dropdownOpen = false;
  hide("account-dropdown");
}

let lastActivityWalletName = null;

function renderAccountCard(state, name) {
  const wallet = findWallet(state, name);
  const bigAvatar = el("account-avatar-big");
  const addrEl = el("account-address");
  const balanceEl = el("account-balance");
  const primeBtn = el("toggle-prime");
  const compositeBtn = el("toggle-composite");
  const running = state.mining_running;

  if (!wallet) {
    bigAvatar.textContent = "?";
    bigAvatar.removeAttribute("style");
    addrEl.textContent = "-";
    balanceEl.textContent = "0";
    primeBtn.disabled = true;
    compositeBtn.disabled = true;
    lastActivityWalletName = null;
    renderActivityList(null);
    return;
  }

  // Fetch history only when the viewed wallet actually changes, not on
  // every state poll -- it's a local chain.dat scan, not something to
  // redo every 3 seconds for no reason.
  if (wallet.name !== lastActivityWalletName) {
    lastActivityWalletName = wallet.name;
    refreshAccountActivity(wallet.name);
  }

  bigAvatar.textContent = wallet.name.slice(0, 1).toUpperCase();
  bigAvatar.setAttribute("style", avatarStyleFor(wallet.name));
  addrEl.textContent = shortAddress(wallet.address);
  addrEl.title = wallet.address || "";
  balanceEl.textContent = wallet.total_micro_units;

  for (const [btn, role] of [[primeBtn, "prime"], [compositeBtn, "composite"]]) {
    const isAssigned = wallet.active_roles.includes(role);
    // Colored/active only while actually mining that role right now --
    // which wallet is *assigned* to a role (independent of whether
    // mining is running) shows up on the account switcher's P/C badges
    // instead, so the button itself doesn't need a separate, easy-to-
    // confuse-with-"live" resting color.
    btn.classList.toggle("active", isAssigned && running);
    btn.disabled = running; // switching the active wallet mid-run isn't allowed server-side
    if (running) {
      btn.title = isAssigned
        ? `currently mining the ${role} role`
        : "stop mining to change role assignment";
    } else {
      btn.title = isAssigned
        ? `assigned to mine the ${role} role once you start -- click to keep it, pick another wallet to move it`
        : `make this wallet the ${role} miner`;
    }
  }
}

let activityRequestId = 0;

async function refreshAccountActivity(name) {
  const requestId = ++activityRequestId; // an older, slower request must not overwrite a newer one
  const list = el("account-activity-list");
  list.innerHTML = "";
  const loading = document.createElement("p");
  loading.className = "muted tiny";
  loading.textContent = "loading...";
  list.appendChild(loading);

  let data;
  try {
    data = await api(`/api/wallets/history?name=${encodeURIComponent(name)}`);
  } catch (err) {
    if (requestId !== activityRequestId) return;
    list.innerHTML = "";
    const p = document.createElement("p");
    p.className = "muted tiny";
    p.textContent = `could not load activity: ${err.message}`;
    list.appendChild(p);
    return;
  }
  if (requestId !== activityRequestId) return; // a newer request already landed
  renderActivityList(data);
}

function renderActivityList(data) {
  const list = el("account-activity-list");
  list.innerHTML = "";
  const empty = (text) => {
    const p = document.createElement("p");
    p.className = "muted tiny";
    p.textContent = text;
    list.appendChild(p);
  };
  if (!data) return empty("-");
  const events = data.events || [];
  if (events.length === 0) {
    // Pending events come from a live mempool query and don't need a
    // local sync -- only "nothing at all to show" depends on that.
    if (!data.synced) return empty("not synced locally yet -- start mining once to pull chain history");
    return empty("no activity yet");
  }

  // Already newest-first, pending on top -- see _handle_wallet_history.
  for (const ev of events) {
    const row = document.createElement("div");
    const isReceived = ev.direction === "received";
    const isFee = ev.direction === "fee-paid";
    row.className = "activity-row " + (isReceived ? "in" : isFee ? "fee" : "out") + (ev.pending ? " pending" : "");

    const label = document.createElement("div");
    label.className = "activity-row-label";
    if (isFee) {
      label.textContent = "Network fee";
    } else {
      const other = isReceived ? ev.sender : ev.receiver;
      label.textContent = `${isReceived ? "Received from" : "Sent to"} ${shortAddress(other)}`;
      label.title = other || "";
    }
    if (ev.pending) {
      const badge = document.createElement("span");
      badge.className = "pending-badge";
      badge.textContent = "pending";
      label.appendChild(badge);
    }

    const amount = document.createElement("div");
    amount.className = "activity-row-amount";
    amount.textContent = `${isReceived ? "+" : "-"}${ev.amount_micro_units} (#${ev.prime})`;

    row.appendChild(label);
    row.appendChild(amount);
    list.appendChild(row);
  }
}

function renderMiningBar(state) {
  const running = state.mining_running;
  const dot = el("mining-dot");
  const label = el("mining-label");
  const btn = el("mining-toggle-btn");

  // The Start/Stop button reflects the wallet you're *looking at*, not
  // just whether mining is running somewhere. Mining is one global
  // process, but showing "Stop mining" while viewing a wallet that
  // isn't the one actually mining reads as if clicking it would stop
  // that wallet -- when it wouldn't, since it isn't running.
  const viewedWallet = findWallet(state, selectedWalletName);
  const viewedWalletIsMining = running && !!viewedWallet && viewedWallet.active_roles.length > 0;
  const minerWallet = running ? state.wallets.find((w) => w.active_roles.length > 0) : null;

  dot.className = "dot" + (viewedWalletIsMining ? " running" : !running && state.mining_failed ? " failed" : "");
  if (viewedWalletIsMining) {
    label.textContent = "Mining running";
  } else if (running) {
    label.textContent = minerWallet ? `Mining running with ${minerWallet.name}` : "Mining running";
  } else if (state.mining_failed) {
    label.textContent = "Mining stopped (last run failed)";
  } else {
    label.textContent = "Mining stopped";
  }
  btn.textContent = viewedWalletIsMining ? "Stop mining" : "Start mining";
  btn.disabled = false; // clicking always does something sensible now -- see onClick("mining-toggle-btn")
  btn.title = "";

  renderJobStatusLine(state.job_status || {}, running);
}

function renderJobStatusLine(jobStatus, running) {
  const el_ = el("job-status");
  const entries = Object.entries(jobStatus);
  if (!entries.length) {
    el_.textContent = "";
    el_.title = "";
    el_.classList.toggle("active", running);
    return;
  }
  // The raw job-status has workdir/peer/timestamps in it too --
  // useful for debugging, not for a glance at the mining bar. Show
  // just the two numbers that actually change while you watch, keep
  // the full dump as a tooltip for anyone who wants it.
  const parts = [];
  if (jobStatus.LOCAL_FRONTIER !== undefined) {
    parts.push(`local frontier: ${jobStatus.LOCAL_FRONTIER}`);
  }
  if (jobStatus.JOB_STATUS !== undefined) {
    parts.push(jobStatus.JOB_STATUS.replace(/-/g, " "));
  }
  el_.textContent = parts.join("  ·  ");
  el_.title = entries.map(([k, v]) => `${k}: ${v}`).join("\n");
  el_.classList.toggle("active", running);
}

function renderSendFrom(state) {
  const select = el("send-from");
  const prev = select.value;
  select.innerHTML = "";
  for (const w of state.wallets) {
    const opt = document.createElement("option");
    opt.value = w.name;
    opt.textContent = `${w.name} (${shortAddress(w.address)})`;
    select.appendChild(opt);
  }
  if ([...select.options].some((o) => o.value === prev)) {
    select.value = prev;
  } else if (selectedWalletName) {
    select.value = selectedWalletName;
  }
}

function renderGlobalMiningBanner(state) {
  // Deliberately independent of screen/wallet state -- a mining process
  // reads the canonical wallet files directly, not the named ones, so
  // it can keep running even with zero named wallets around (e.g. the
  // one that was active got deleted). If it's running, there must
  // always be a visible way to see that and stop it.
  setHidden("global-mining-banner", !state.mining_running);
  if (!state.mining_running) return;
  const frontier = (state.job_status || {}).LOCAL_FRONTIER;
  el("global-mining-text").textContent = frontier
    ? `Mining is running · local frontier: ${frontier}`
    : "Mining is running";
}

function renderAll(state) {
  if (!state) return;
  lastState = state;
  renderScreens(state);
  renderSettings(state);
  renderGlobalMiningBanner(state);
  if (!state.config.unlocked || state.wallets.length === 0) return;
  const name = pickSelectedWallet(state);
  renderAccountSwitcher(state, name);
  renderDropdown(state);
  renderAccountCard(state, name);
  renderMiningBar(state);
  renderSendFrom(state);
}

let stateRequestInFlight = false;

async function refreshState() {
  // /api/state shells out to primechain-client for balances/job-status;
  // on a slow disk (e.g. mid-sync) one call can take longer than the
  // poll interval. Never let a new one stack on top of a slow one --
  // that's how a burst of overlapping subprocesses piles up.
  if (stateRequestInFlight) return lastState;
  stateRequestInFlight = true;
  try {
    const state = await api("/api/state");
    renderAll(state);
    return state;
  } finally {
    stateRequestInFlight = false;
  }
}

async function refreshLog() {
  const data = await api(`/api/log?since=${logCursor}`);
  if (data.lines.length) {
    const pre = el("log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
    pre.textContent += data.lines.join("\n") + "\n";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
  logCursor = data.next;
}

// -- modals ---------------------------------------------------------------

function openCreateModal() {
  el("create-wallet-name").value = "";
  el("create-error").textContent = "";
  show("create-modal");
  el("create-wallet-name").focus();
}
function closeCreateModal() { hide("create-modal"); }

function openSendModal() {
  el("send-result").textContent = "";
  show("send-modal");
  if (selectedWalletName) el("send-from").value = selectedWalletName;
  populateSendReceiverSuggestions();
  refreshSendHoldings();
}
function closeSendModal() { hide("send-modal"); }

function populateSendReceiverSuggestions() {
  const list = el("send-receiver-suggestions");
  list.innerHTML = "";
  if (!lastState) return;
  for (const w of lastState.wallets) {
    if (!w.address) continue;
    const opt = document.createElement("option");
    opt.value = w.address;
    opt.label = w.name;
    list.appendChild(opt);
  }
}

let sendHoldingsRequestId = 0;
let currentSendFee = null; // micro-units, from the network's live economic policy
let sendHoldingsStale = false; // last live balance check failed -- holdings shown are a cached fallback

async function refreshSendHoldings() {
  const name = el("send-from").value;
  const select = el("send-prime");
  const hint = el("send-holding-hint");
  const requestId = ++sendHoldingsRequestId; // guards against an older, slower request overwriting a newer one
  select.innerHTML = "";
  hint.textContent = "loading holdings...";
  if (!name) {
    hint.textContent = "";
    return;
  }
  let holdings = [];
  try {
    const data = await api(`/api/wallets/holdings?name=${encodeURIComponent(name)}`);
    holdings = data.holdings || [];
    sendHoldingsStale = !!data.stale;
    if (data.transfer_fee_micro_units !== null && data.transfer_fee_micro_units !== undefined) {
      currentSendFee = data.transfer_fee_micro_units;
      el("send-fee").value = currentSendFee;
    }
  } catch (err) {
    if (requestId !== sendHoldingsRequestId) return;
    hint.textContent = `could not load holdings: ${err.message}`;
    return;
  }
  if (requestId !== sendHoldingsRequestId) return; // a newer request already landed

  if (holdings.length === 0) {
    hint.textContent = "this wallet has no assets to send yet";
    return;
  }
  for (const h of holdings) {
    const opt = document.createElement("option");
    opt.value = h.prime;
    opt.dataset.microUnits = h.micro_units;
    opt.textContent = `${h.prime} (${h.micro_units} available)`;
    select.appendChild(opt);
  }
  updateSendHoldingHint();
}

function updateSendHoldingHint() {
  const select = el("send-prime");
  const hint = el("send-holding-hint");
  const opt = select.options[select.selectedIndex];
  const available = opt ? `available: ${opt.dataset.microUnits}` : "";
  // The live balance check failed (peer busy/rate-limited) and this is
  // the last holdings snapshot that *did* load, not necessarily what the
  // wallet holds right now -- say so instead of presenting it as current.
  hint.textContent = sendHoldingsStale
    ? [available, "showing last known holdings -- network is slow to respond"].filter(Boolean).join(" -- ")
    : available;
}

function openReceiveModal() {
  const wallet = lastState && findWallet(lastState, selectedWalletName);
  el("receive-address").textContent = wallet && wallet.address ? wallet.address : "(address unreadable)";
  show("receive-modal");
}
function closeReceiveModal() { hide("receive-modal"); }

// -- wiring -----------------------------------------------------------------

for (const id of ["cfg-workdir", "cfg-peer-host", "cfg-peer-port", "cfg-target"]) {
  el(id).addEventListener("input", () => { settingsTouched = true; });
}

function switchSettingsTab(tab) {
  const isConfig = tab === "config";
  el("tab-btn-config").classList.toggle("active", isConfig);
  el("tab-btn-wallets").classList.toggle("active", !isConfig);
  setHidden("tab-panel-config", !isConfig);
  setHidden("tab-panel-wallets", isConfig);
  if (!isConfig) {
    deleteConfirmTarget = null;
    el("rekey-new-passphrase").value = "";
    el("rekey-confirm-passphrase").value = "";
    el("rekey-error").textContent = "";
    el("rekey-result").textContent = "";
    refreshManageList();
  }
}

function openSettings(tab) {
  show("settings-modal");
  switchSettingsTab(tab || "config");
}

el("settings-btn").addEventListener("click", () => openSettings("config"));
el("close-settings-btn").addEventListener("click", () => hide("settings-modal"));
el("tab-btn-config").addEventListener("click", () => switchSettingsTab("config"));
el("tab-btn-wallets").addEventListener("click", () => switchSettingsTab("wallets"));

onClick("save-config-btn", async () => {
  const workdir = el("cfg-workdir").value.trim();
  const peer_host = el("cfg-peer-host").value.trim();
  const peer_port = parseInt(el("cfg-peer-port").value, 10);
  const target = el("cfg-target").value.trim();
  if (!workdir || !peer_host || !peer_port) {
    throw new Error("workdir, peer host, and peer port are all required");
  }
  await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workdir, peer_host, peer_port, target: target || undefined }),
  });
  settingsTouched = false;
  el("config-status").textContent = "saved";
  setTimeout(() => { el("config-status").textContent = ""; }, 2500);
});

onClick("unlock-btn", async () => {
  const input = el("passphrase-input");
  const passphrase = input.value;
  input.value = ""; // don't leave it sitting in the DOM
  el("unlock-error").textContent = "";
  if (!passphrase) throw new Error("enter a passphrase first");
  try {
    await api("/api/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
  } catch (err) {
    el("unlock-error").textContent = err.message;
    throw err;
  }
});
el("passphrase-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") el("unlock-btn").click();
});

onClick("lock-btn", async () => {
  await api("/api/lock", { method: "POST" });
  setSelectedWallet(null);
});

async function ensureUnlockedForSetup() {
  if (lastState && lastState.config.unlocked) return;
  const passphrase = el("onboard-passphrase").value;
  if (!passphrase) throw new Error("choose a passphrase first");
  await api("/api/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passphrase }),
  });
}

onClick("onboard-create-btn", async () => {
  const input = el("onboard-wallet-name");
  const name = input.value.trim();
  el("onboard-error").textContent = "";
  if (!name) throw new Error("enter a wallet name first");
  try {
    await ensureUnlockedForSetup();
    const result = await api("/api/wallets/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    setSelectedWallet(result.name);
    el("onboard-passphrase").value = "";
  } catch (err) {
    el("onboard-error").textContent = err.message;
    throw err;
  }
});
el("onboard-wallet-name").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") el("onboard-create-btn").click();
});
el("onboard-passphrase").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") el("onboard-create-btn").click();
});

el("onboard-import-btn").addEventListener("click", () => el("onboard-import-file").click());
el("onboard-import-file").addEventListener("change", async () => {
  const fileInput = el("onboard-import-file");
  const file = fileInput.files[0];
  fileInput.value = "";
  if (!file) return;
  el("onboard-error").textContent = "";
  const typedName = el("onboard-wallet-name").value.trim();
  const name = typedName || file.name.replace(/\.wallet$/i, "");
  if (!name) {
    el("onboard-error").textContent = "enter a wallet name first";
    return;
  }
  try {
    await ensureUnlockedForSetup();
    const wallet_file_b64 = await fileToBase64(file);
    const result = await api("/api/wallets/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, wallet_file_b64 }),
    });
    setSelectedWallet(result.name);
    el("onboard-passphrase").value = "";
  } catch (err) {
    el("onboard-error").textContent = err.message;
  }
  await refreshState().catch((e) => console.error(e));
});

el("account-switcher-btn").addEventListener("click", (ev) => {
  ev.preventDefault();
  if (dropdownOpen) closeDropdown();
  else openDropdown();
});
document.addEventListener("click", (ev) => {
  if (!dropdownOpen) return;
  if (!el("account-switcher").contains(ev.target)) closeDropdown();
});

// -- manage wallets (delete) ------------------------------------------

let deleteConfirmTarget = null;
let lastManageWallets = [];

async function refreshManageList() {
  try {
    const data = await api("/api/wallets/list_all");
    lastManageWallets = data.wallets || [];
  } catch (err) {
    console.error(err);
  }
  renderManageList(lastManageWallets);
}

function renderManageList(wallets) {
  const container = el("manage-wallet-list");
  container.innerHTML = "";
  if (!wallets || wallets.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted small";
    empty.textContent = "No wallets found in this workdir yet.";
    container.appendChild(empty);
    return;
  }
  for (const w of wallets) {
    const row = document.createElement("div");
    row.className = "manage-row";

    if (deleteConfirmTarget === w.name) {
      const confirmWrap = document.createElement("div");
      confirmWrap.className = "manage-confirm-row";
      const label = document.createElement("span");
      label.className = "small muted";
      label.textContent = `Type "${w.name}" to permanently delete it:`;
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = w.name;
      const actions = document.createElement("div");
      actions.className = "manage-confirm-actions";
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "manage-confirm-cancel";
      cancelBtn.textContent = "Cancel";
      cancelBtn.addEventListener("click", () => {
        deleteConfirmTarget = null;
        renderManageList(lastManageWallets);
      });
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "manage-confirm-delete";
      deleteBtn.textContent = "Delete permanently";
      deleteBtn.disabled = true;
      input.addEventListener("input", () => {
        deleteBtn.disabled = input.value !== w.name;
      });
      deleteBtn.addEventListener("click", async () => {
        deleteBtn.disabled = true;
        try {
          await api("/api/wallets/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: w.name, confirm_name: input.value }),
          });
          deleteConfirmTarget = null;
          showToast(`Deleted "${w.name}"`);
          if (selectedWalletName === w.name) setSelectedWallet(null);
        } catch (err) {
          showToast(err.message, true);
          deleteBtn.disabled = false;
        }
        await refreshManageList();
        await refreshState().catch((e) => console.error(e));
      });
      actions.appendChild(cancelBtn);
      actions.appendChild(deleteBtn);
      confirmWrap.appendChild(label);
      confirmWrap.appendChild(input);
      confirmWrap.appendChild(actions);
      row.appendChild(confirmWrap);
      input.focus();
    } else {
      const info = document.createElement("div");
      info.className = "manage-row-info";
      const nameEl = document.createElement("div");
      nameEl.className = "manage-row-name";
      nameEl.textContent = w.name;
      const addrEl = document.createElement("code");
      addrEl.className = "manage-row-address";
      addrEl.textContent = w.address || "(address unreadable)";
      info.appendChild(nameEl);
      info.appendChild(addrEl);
      if (w.active_roles.length) {
        const roles = document.createElement("div");
        roles.className = "manage-row-roles muted tiny";
        roles.textContent = `active: ${w.active_roles.join(", ")}`;
        info.appendChild(roles);
      }
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "manage-delete-btn";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", () => {
        deleteConfirmTarget = w.name;
        renderManageList(lastManageWallets);
      });
      row.appendChild(info);
      row.appendChild(delBtn);
    }
    container.appendChild(row);
  }
}

// -- change passphrase (inline in the Wallets settings tab) ------------

onClick("rekey-submit-btn", async () => {
  const next = el("rekey-new-passphrase").value;
  const confirm = el("rekey-confirm-passphrase").value;
  el("rekey-error").textContent = "";
  el("rekey-result").textContent = "";
  if (next.length < 4) throw new Error("choose a passphrase at least 4 characters long");
  if (next !== confirm) throw new Error("passphrases don't match");
  try {
    const result = await api("/api/wallets/rekey", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_passphrase: next }),
    });
    el("rekey-new-passphrase").value = "";
    el("rekey-confirm-passphrase").value = "";
    const parts = [];
    if (result.succeeded.length) parts.push(`changed: ${result.succeeded.join(", ")}`);
    if (result.failed.length) parts.push(`unchanged (different passphrase): ${result.failed.map((f) => f.name).join(", ")}`);
    el("rekey-result").textContent = parts.join("  ·  ") || "nothing to do";
    showToast(result.succeeded.length ? "Passphrase changed" : "No wallets matched your current passphrase");
  } catch (err) {
    el("rekey-error").textContent = err.message;
    throw err;
  }
});

el("forgot-passphrase-btn").addEventListener("click", () => show("forgot-modal"));
el("close-forgot-btn").addEventListener("click", () => hide("forgot-modal"));
el("forgot-open-settings-btn").addEventListener("click", () => {
  hide("forgot-modal");
  openSettings("config");
  el("cfg-workdir").focus();
  el("cfg-workdir").select();
});

el("export-wallet-btn").addEventListener("click", () => {
  if (!selectedWalletName) return;
  window.open(`/api/wallets/export?name=${encodeURIComponent(selectedWalletName)}`, "_blank");
});

onClick("copy-address-btn", async () => {
  const wallet = lastState && findWallet(lastState, selectedWalletName);
  if (!wallet || !wallet.address) throw new Error("no address to copy yet");
  const ok = await copyToClipboard(wallet.address);
  showToast(ok ? "Address copied" : "Could not copy -- copy it manually from Receive");
});

el("refresh-activity-btn").addEventListener("click", () => {
  if (selectedWalletName) refreshAccountActivity(selectedWalletName);
});

el("toggle-prime").addEventListener("click", () => activateRole("prime"));
el("toggle-composite").addEventListener("click", () => activateRole("composite"));
async function activateRole(role) {
  if (!selectedWalletName) return;
  try {
    await api("/api/wallets/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, name: selectedWalletName }),
    });
  } catch (err) {
    showToast(err.message, true);
  }
  await refreshState().catch((e) => console.error(e));
}

onClick("mining-toggle-btn", async () => {
  const state = lastState;
  if (!state) return;
  const viewedWallet = findWallet(state, selectedWalletName);
  if (!viewedWallet) throw new Error("no wallet selected");
  const running = state.mining_running;
  const viewedWalletIsMining = running && viewedWallet.active_roles.length > 0;

  if (running && viewedWalletIsMining) {
    await api("/api/mining/stop", { method: "POST" });
    return;
  }

  if (running && !viewedWalletIsMining) {
    // mining is running, but with a *different* wallet than the one
    // being viewed -- clicking "Start mining" here means "switch to
    // this wallet instead", which needs stopping the current run first.
    // Confirm rather than silently yanking mining away from whichever
    // wallet the user thought was still going.
    const minerWallet = state.wallets.find((w) => w.active_roles.length > 0);
    const minerName = minerWallet ? minerWallet.name : "another wallet";
    const proceed = window.confirm(
      `${minerName} is currently mining. Stop it and start mining with ${viewedWallet.name} instead?`
    );
    if (!proceed) return;
    await api("/api/mining/stop", { method: "POST" });
  }

  // Not running (or just stopped above to switch onto this wallet) --
  // make sure the viewed wallet holds both roles before starting, so
  // "Start mining" from any wallet's view is a one-click action instead
  // of requiring the prime/composite toggles to be set up first.
  if (!viewedWallet.active_roles.includes("prime")) {
    await api("/api/wallets/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "prime", name: viewedWallet.name }),
    });
  }
  if (!viewedWallet.active_roles.includes("composite")) {
    await api("/api/wallets/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "composite", name: viewedWallet.name }),
    });
  }
  await api("/api/mining/start", { method: "POST" });
  // starting mining can be quiet for a while (fresh sync) -- open the
  // log automatically so progress is visible without hunting for the toggle
  show("activity-panel");
  el("activity-toggle").textContent = "Activity ▴";
});

onClick("global-mining-stop-btn", async () => {
  await api("/api/mining/stop", { method: "POST" });
});

el("activity-toggle").addEventListener("click", () => {
  const hidden = el("activity-panel").classList.toggle("hidden");
  el("activity-toggle").textContent = hidden ? "Activity ▾" : "Activity ▴";
});

el("open-send-btn").addEventListener("click", openSendModal);
el("close-send-btn").addEventListener("click", closeSendModal);
el("send-from").addEventListener("change", refreshSendHoldings);
el("send-prime").addEventListener("change", updateSendHoldingHint);
el("send-max-btn").addEventListener("click", () => {
  const select = el("send-prime");
  const opt = select.options[select.selectedIndex];
  if (!opt) return;
  const available = parseInt(opt.dataset.microUnits, 10);
  const fee = currentSendFee !== null ? currentSendFee : parseInt(el("send-fee").value || "0", 10);
  const max = available - fee;
  el("send-amount").value = max > 0 ? max : 0;
});
el("open-receive-btn").addEventListener("click", openReceiveModal);
el("close-receive-btn").addEventListener("click", closeReceiveModal);
el("close-create-btn").addEventListener("click", closeCreateModal);

onClick("copy-receive-btn", async () => {
  const text = el("receive-address").textContent;
  const ok = await copyToClipboard(text);
  showToast(ok ? "Address copied" : "Could not copy -- copy it manually");
});

el("create-wallet-name").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") el("create-submit-btn").click();
});

onClick("create-submit-btn", async () => {
  const input = el("create-wallet-name");
  const name = input.value.trim();
  el("create-error").textContent = "";
  if (!name) throw new Error("enter a wallet name first");
  try {
    const result = await api("/api/wallets/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    setSelectedWallet(result.name);
    closeCreateModal();
  } catch (err) {
    el("create-error").textContent = err.message;
    throw err;
  }
});

el("create-import-btn").addEventListener("click", () => el("create-import-file").click());
el("create-import-file").addEventListener("change", async () => {
  const fileInput = el("create-import-file");
  const file = fileInput.files[0];
  fileInput.value = "";
  if (!file) return;
  el("create-error").textContent = "";
  const typedName = el("create-wallet-name").value.trim();
  const name = typedName || file.name.replace(/\.wallet$/i, "");
  if (!name) {
    el("create-error").textContent = "enter a wallet name first";
    return;
  }
  try {
    const wallet_file_b64 = await fileToBase64(file);
    const result = await api("/api/wallets/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, wallet_file_b64 }),
    });
    setSelectedWallet(result.name);
    closeCreateModal();
  } catch (err) {
    el("create-error").textContent = err.message;
  }
  await refreshState().catch((e) => console.error(e));
});

onClick("send-submit-btn", async () => {
  const body = {
    from: el("send-from").value,
    receiver: el("send-receiver").value.trim(),
    prime: parseInt(el("send-prime").value, 10),
    amount: parseInt(el("send-amount").value, 10),
    fee: parseInt(el("send-fee").value || "1", 10),
  };
  if (!body.from) throw new Error("no wallet selected to send from");
  if (!body.receiver || !Number.isFinite(body.prime) || !Number.isFinite(body.amount)) {
    throw new Error("receiver, prime asset #, and amount are required");
  }
  const btn = el("send-submit-btn");
  const originalLabel = btn.textContent;
  btn.textContent = "Sending...";
  el("send-result").textContent = "";
  try {
    const result = await api("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    el("send-result").textContent = result.result;
    showToast("Sent");
    // It won't show up in wallet-history until it's mined and locally
    // synced, but it's in the peer's mempool right away -- refresh
    // Activity now (unconditionally, not gated on wallet-switch) so the
    // pending entry appears immediately instead of on the next 3s poll.
    if (body.from === lastActivityWalletName) refreshAccountActivity(body.from);
  } finally {
    btn.textContent = originalLabel;
  }
});

// close modals on backdrop click / Escape
const ALL_MODAL_IDS = ["send-modal", "receive-modal", "create-modal", "forgot-modal", "settings-modal"];
for (const overlayId of ALL_MODAL_IDS) {
  el(overlayId).addEventListener("click", (ev) => {
    if (ev.target.id === overlayId) el(overlayId).classList.add("hidden");
  });
}
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  for (const overlayId of ALL_MODAL_IDS) {
    el(overlayId).classList.add("hidden");
  }
  closeDropdown();
});

refreshState().catch((err) => console.error(err));
safeInterval(refreshState, 3000);
safeInterval(refreshLog, 1000);
