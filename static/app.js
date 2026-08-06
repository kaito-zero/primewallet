"use strict";

let logCursor = 0;
let lastState = null;
let selectedWalletName = null; // which account card is showing -- UI-only, independent of mining roles
let settingsTouched = false;
let dropdownOpen = false;
let toastTimer = null;

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
  selectedWalletName = fallback ? fallback.name : null;
  return selectedWalletName;
}

// -- rendering ----------------------------------------------------------

function renderScreens(state) {
  const unlocked = state.config.unlocked;
  setHidden("unlock-screen", unlocked);
  setHidden("onboarding-screen", !unlocked || state.wallets.length > 0);
  setHidden("main-screen", !unlocked || state.wallets.length === 0);
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
    for (const role of ["prime", "composite"]) {
      const dot = document.createElement("span");
      dot.className = "role-mini-dot" + (w.active_roles.includes(role) ? " on" : "");
      dot.title = `${role}${w.active_roles.includes(role) ? " (active)" : ""}`;
      roles.appendChild(dot);
    }
    item.appendChild(av);
    item.appendChild(label);
    item.appendChild(roles);
    item.addEventListener("click", () => {
      selectedWalletName = w.name;
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
    return;
  }

  bigAvatar.textContent = wallet.name.slice(0, 1).toUpperCase();
  bigAvatar.setAttribute("style", avatarStyleFor(wallet.name));
  addrEl.textContent = shortAddress(wallet.address);
  addrEl.title = wallet.address || "";
  balanceEl.textContent = wallet.total_micro_units;

  for (const [btn, role] of [[primeBtn, "prime"], [compositeBtn, "composite"]]) {
    btn.classList.toggle("active", wallet.active_roles.includes(role));
    btn.disabled = running; // switching the active wallet mid-run isn't allowed server-side
    btn.title = running
      ? "stop mining to change role assignment"
      : wallet.active_roles.includes(role)
        ? `this wallet currently mines the ${role} role -- click to keep it, pick another wallet to move it`
        : `make this wallet the ${role} miner`;
  }
}

function renderMiningBar(state) {
  const running = state.mining_running;
  const dot = el("mining-dot");
  const label = el("mining-label");
  const btn = el("mining-toggle-btn");

  dot.className = "dot" + (running ? " running" : state.mining_failed ? " failed" : "");
  if (running) {
    label.textContent = "Mining running";
  } else if (state.mining_failed) {
    label.textContent = "Mining stopped (last run failed)";
  } else {
    label.textContent = "Mining stopped";
  }
  btn.textContent = running ? "Stop mining" : "Start mining";

  const hasPair =
    state.wallets.some((w) => w.active_roles.includes("prime")) &&
    state.wallets.some((w) => w.active_roles.includes("composite"));
  btn.disabled = !running && !hasPair;
  btn.title = !running && !hasPair
    ? "activate a wallet for both prime and composite mining first"
    : "";

  const entries = Object.entries(state.job_status || {});
  el("job-status").textContent = entries.length
    ? entries.map(([k, v]) => `${k}: ${v}`).join("  ·  ")
    : "";
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

function renderAll(state) {
  if (!state) return;
  lastState = state;
  renderScreens(state);
  renderSettings(state);
  if (!state.config.unlocked || state.wallets.length === 0) return;
  const name = pickSelectedWallet(state);
  renderAccountSwitcher(state, name);
  renderDropdown(state);
  renderAccountCard(state, name);
  renderMiningBar(state);
  renderSendFrom(state);
}

async function refreshState() {
  const state = await api("/api/state");
  renderAll(state);
  return state;
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
}
function closeSendModal() { hide("send-modal"); }

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

el("settings-btn").addEventListener("click", () => {
  el("settings-panel").classList.toggle("hidden");
});

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
  selectedWalletName = null;
});

onClick("onboard-create-btn", async () => {
  const input = el("onboard-wallet-name");
  const name = input.value.trim();
  el("onboard-error").textContent = "";
  if (!name) throw new Error("enter a wallet name first");
  try {
    const result = await api("/api/wallets/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    selectedWalletName = result.name;
  } catch (err) {
    el("onboard-error").textContent = err.message;
    throw err;
  }
});
el("onboard-wallet-name").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") el("onboard-create-btn").click();
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

onClick("copy-address-btn", async () => {
  const wallet = lastState && findWallet(lastState, selectedWalletName);
  if (!wallet || !wallet.address) throw new Error("no address to copy yet");
  const ok = await copyToClipboard(wallet.address);
  showToast(ok ? "Address copied" : "Could not copy -- copy it manually from Receive");
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
  if (lastState && lastState.mining_running) {
    await api("/api/mining/stop", { method: "POST" });
  } else {
    await api("/api/mining/start", { method: "POST" });
  }
});

el("activity-toggle").addEventListener("click", () => {
  const hidden = el("activity-panel").classList.toggle("hidden");
  el("activity-toggle").textContent = hidden ? "Activity ▾" : "Activity ▴";
});

el("open-send-btn").addEventListener("click", openSendModal);
el("close-send-btn").addEventListener("click", closeSendModal);
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
    selectedWalletName = result.name;
    closeCreateModal();
  } catch (err) {
    el("create-error").textContent = err.message;
    throw err;
  }
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
  const result = await api("/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  el("send-result").textContent = result.result;
  showToast("Sent");
});

// close modals on backdrop click / Escape
for (const overlayId of ["send-modal", "receive-modal", "create-modal"]) {
  el(overlayId).addEventListener("click", (ev) => {
    if (ev.target.id === overlayId) el(overlayId).classList.add("hidden");
  });
}
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  for (const overlayId of ["send-modal", "receive-modal", "create-modal"]) {
    el(overlayId).classList.add("hidden");
  }
  closeDropdown();
});

refreshState().catch((err) => console.error(err));
safeInterval(refreshState, 3000);
safeInterval(refreshLog, 1000);
</content>
