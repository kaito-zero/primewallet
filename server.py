#!/usr/bin/env python3
"""primewallet: a self-contained local wallet/miner UI for primechain.

Wraps the existing CLI tools (primechain-client, primechain-wallet,
primechain-send) -- it doesn't reimplement wallet crypto, signing, proof
construction, or networking. Everything it does is one of those binaries
run as a subprocess, the same way the quickstart docs already tell you
to run them by hand.

Quickstart:
    python3 server.py

Then open http://127.0.0.1:8765/ . No environment variables or flags
are required to start it -- workdir, peer, and passphrase are all set
from the browser on first run. (--bin-dir/--workdir/--peer-host/
--peer-port/--listen-port are available as overrides; see --help.)

Binds to 127.0.0.1 only. This hands out control over your wallets
(including a decryption passphrase held in memory) and your mining
process -- it must never be reachable from the network. The passphrase
is kept in this process's memory only, passed to subprocess environments
the same way the CLI expects it, and is never written to disk or logged.

Requires only the Python 3 standard library; nothing to install.
"""

import argparse
import base64
import binascii
import collections
import http.server
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

STATIC_DIR = Path(__file__).parent / "static"

# A fresh workdir syncing from genesis against a live frontier in the
# thousands has taken multiple minutes in practice; give it real room
# instead of guessing low and producing a confusing timeout mid-sync.
SYNC_TIMEOUT_SECONDS = 900
DEFAULT_CLI_TIMEOUT_SECONDS = 60
WALLET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The largest legitimate POST body is a base64-encoded wallet import
# (a wallet file is a few KB, so even generously that's under 1 MiB
# once encoded). 16 MiB leaves plenty of headroom without accepting an
# unbounded Content-Length -- not remotely exploitable since this only
# binds to 127.0.0.1, but a stray/misbehaving local process forcing an
# oversized read shouldn't be free to force memory pressure either.
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024


class CliError(Exception):
    """Raised when a primechain-client/-wallet/-send invocation fails,
    can't be started, or times out. Always safe to show to the user --
    never wraps a raw traceback."""


class WalletRegistry:
    """Named wallets live in <workdir>/wallets/named/<name>.wallet -- plain
    miner-identity wallet files, same format primechain-wallet always
    produces. Two of them are designated "active" at a time (one for the
    prime-mining role, one for composite-mining), tracked in active.json.
    run-jobs/primechain-client always read from the fixed
    wallets/prime.wallet and wallets/composite.wallet paths, so making a
    wallet "active" copies its bytes onto that canonical path -- the
    named copy stays untouched so switching back and forth never loses a
    wallet."""

    def __init__(self, workdir):
        self.workdir = workdir

    @property
    def named_dir(self):
        return self.workdir / "wallets" / "named"

    @property
    def active_file(self):
        return self.workdir / "wallets" / "active.json"

    def _read_active(self):
        try:
            return json.loads(self.active_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_active(self, active):
        self.active_file.parent.mkdir(parents=True, exist_ok=True)
        self.active_file.write_text(json.dumps(active))

    def named_wallet_path(self, name):
        if not WALLET_NAME_RE.match(name):
            raise CliError(
                "wallet name must be 1-64 characters of letters, digits, _, -"
            )
        return self.named_dir / f"{name}.wallet"

    def list_names(self):
        if not self.named_dir.is_dir():
            return []
        return sorted(p.stem for p in self.named_dir.glob("*.wallet"))

    def active_names(self):
        active = self._read_active()
        return {"prime": active.get("prime"), "composite": active.get("composite")}

    def canonical_path(self, role):
        if role not in ("prime", "composite"):
            raise CliError("role must be 'prime' or 'composite'")
        return self.workdir / "wallets" / f"{role}.wallet"

    def has_any_wallet(self):
        """Pure filesystem check, no unlock/passphrase needed -- used to
        decide whether to show "unlock" (a wallet already exists) or
        "create/import" (nothing here yet) on first load."""
        if self.named_dir.is_dir() and any(self.named_dir.glob("*.wallet")):
            return True
        return self.canonical_path("prime").exists() or self.canonical_path("composite").exists()

    def _name_conflict_message(self, name):
        """path.exists() already decided this name is taken -- but on a
        case-insensitive filesystem (WSL2's DrvFs under /mnt/c, /mnt/d,
        etc.) that can trigger for a name that was never actually used,
        just because it differs only by capitalization from one that
        was ('Kaitozero' colliding with an existing 'kaitozero'). "a
        wallet named 'Kaitozero' already exists" is a confusing thing
        to read when you've never created anything by that exact name
        -- name the wallet it actually collides with instead, when
        that's what happened. Never changes *whether* something is
        rejected, only the message -- a case-sensitive filesystem never
        reaches the "differs only by case" branch, since path.exists()
        would already be False there for a genuinely distinct name.
        """
        lowered = name.lower()
        for existing in self.list_names():
            if existing == name:
                break
            if existing.lower() == lowered:
                return (
                    f"a wallet named '{existing}' already exists -- '{name}' only "
                    "differs by capitalization, which this filesystem can't tell apart"
                )
        return f"a wallet named '{name}' already exists"

    def create(self, name, bin_dir, env):
        path = self.named_wallet_path(name)
        if path.exists():
            raise CliError(self._name_conflict_message(name))
        self.named_dir.mkdir(parents=True, exist_ok=True)
        rc, out = run_binary(bin_dir / "primechain-wallet", ["new-miner", str(path)], env)
        if rc != 0:
            raise CliError(f"could not create wallet: {out.strip()}")
        return out.strip()

    def import_wallet(self, name, data, bin_dir, env):
        """Restore a wallet from raw .wallet file bytes (e.g. a backup
        copy). Validated by reading its address back before keeping it --
        garbage input is rejected rather than sitting there silently."""
        path = self.named_wallet_path(name)
        if path.exists():
            raise CliError(self._name_conflict_message(name))
        if not data:
            raise CliError("wallet file is empty")
        self.named_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            return self.address_of(name, bin_dir, env)
        except CliError as exc:
            path.unlink(missing_ok=True)
            raise CliError(f"not a valid wallet file: {exc}") from exc

    def address_of(self, name, bin_dir, env):
        path = self.named_wallet_path(name)
        if not path.exists():
            raise CliError(f"no wallet named '{name}'")
        rc, out = run_binary(bin_dir / "primechain-wallet", ["miner-address", str(path)], env)
        if rc != 0:
            raise CliError(f"could not read address for '{name}': {out.strip()}")
        return out.strip()

    def set_active(self, role, name):
        source = self.named_wallet_path(name)
        if not source.exists():
            raise CliError(f"no wallet named '{name}'")
        canonical = self.canonical_path(role)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(source.read_bytes())
        active = self._read_active()
        active[role] = name
        self._write_active(active)

    def delete(self, name):
        """Removes only the named copy at wallets/named/<name>.wallet.
        Deliberately doesn't touch the canonical prime.wallet/
        composite.wallet files -- if this wallet was active for a role,
        that copy stays put and already-running mining is unaffected.
        What does change is active.json: any role pointing at the
        now-gone name is cleared, so state stays honest (no role can
        claim a wallet that doesn't exist to select again) instead of
        blocking the delete on "pick a replacement first" -- which is a
        dead end if this is the only wallet, or the one whose passphrase
        is the reason you're here."""
        path = self.named_wallet_path(name)
        if not path.exists():
            raise CliError(f"no wallet named '{name}'")
        path.unlink()
        active = self._read_active()
        cleared = [r for r, n in active.items() if n == name]
        if cleared:
            for role in cleared:
                active[role] = None
            self._write_active(active)

    def ensure_migrated_from_canonical(self):
        """If wallets/prime.wallet or wallets/composite.wallet exist (e.g.
        auto-created by init-workdir, or from using the plain CLI) but
        aren't registered as a named wallet yet, adopt them so nothing
        already mined against them becomes orphaned or invisible.

        Each role gets its own name (default-prime / default-composite)
        even though init-workdir happens to create both at once -- they
        are two different keypairs and must never share a named wallet
        file, or activating one for a role would silently overwrite the
        other's canonical copy with the wrong key."""
        active = self._read_active()
        changed = False
        for role in ("prime", "composite"):
            canonical = self.canonical_path(role)
            if active.get(role) or not canonical.exists():
                continue
            name = f"default-{role}"
            named = self.named_wallet_path(name)
            if not named.exists():
                self.named_dir.mkdir(parents=True, exist_ok=True)
                named.write_bytes(canonical.read_bytes())
            active[role] = name
            changed = True
        if changed:
            self._write_active(active)


class AppState:
    """All mutable, in-memory server state: current configuration, the
    wallet passphrase (never persisted), and the background mining
    sequence. One instance for the life of the process."""

    def __init__(self, bin_dir, workdir, peer_host, peer_port, target):
        self.lock = threading.RLock()
        self.bin_dir = bin_dir
        self.workdir = workdir
        self.peer_host = peer_host
        self.peer_port = peer_port
        self.target = target
        self.passphrase = None  # None until /api/unlock is called

        self.mining_thread = None
        self.mining_process = None
        self.mining_log = collections.deque(maxlen=4000)
        self.mining_failed = False

        # run-jobs doesn't exit or fail when it can't decrypt the mining
        # identity's signing key -- confirmed live: it logs "wallet
        # passphrase or authentication failed" and just keeps syncing
        # and retrying forever, so mining_failed (only set when the
        # process itself exits non-zero) never catches it. From the
        # main UI's mining bar, that looks identical to genuinely
        # mining -- green dot, "Mining running" -- with zero indication
        # it can never actually submit a winning proof. Track it
        # separately so the UI can tell the two states apart.
        self.mining_auth_broken = False

        # job-status reads chain.dat directly off disk; landing mid-write
        # by a concurrent sync-download can make the read fail to parse,
        # and primechain-client silently reports frontier=0 instead of
        # surfacing an error (confirmed in its source -- loadLocalStatus()
        # swallows a load failure into a zeroed-out status). Sync
        # progress is monotonic, so track the highest value actually seen
        # and never report a drop below it -- that's a read glitch, not
        # real backward progress. Reset whenever a fresh sync starts.
        self.frontier_floor = 0

        # get_wallets_info() shells out to primechain-client once per
        # wallet (a live GET_BALANCE query) plus once for job-status,
        # which reads the workdir's chain.dat -- while a sync is
        # actively writing to that file, the job-status call can take a
        # while. Without this, every concurrent /api/state request
        # (multiple browser tabs, rapid polling, etc.) would launch its
        # own set of subprocesses fighting over the same file. This lock
        # makes concurrent callers wait for one in-flight computation
        # instead of starting their own; the short TTL cache means
        # callers that arrive moments apart get the same recent answer
        # for free.
        self.probe_lock = threading.Lock()
        self.probe_cache = None  # (monotonic_time, (raw_wallets, job_status))
        self.probe_cache_ttl = 2.0
        self.probe_refreshing = False

        # The network's transfer fee changes rarely if ever, but the Send
        # modal needs it every time it opens (see _handle_wallet_holdings).
        # No reason to pay a live round-trip for it on every holdings
        # lookup -- cache it generously.
        self.policy_lock = threading.Lock()
        self.policy_cache = None  # (monotonic_time, {key: value})
        self.policy_cache_ttl = 20.0

        # GET_BALANCE is a live query against a public, rate-limited peer
        # -- it can time out or get rejected transiently. Rather than
        # showing an empty "no assets to send" dropdown on a hiccup, fall
        # back to the last holdings that *did* load for that wallet.
        self.holdings_lock = threading.Lock()
        self.holdings_cache = {}  # name -> (monotonic_time, holdings_list)

        # Activity (wallet-history + wallet-pending + reward-history) has
        # no live-network shortcut the way balances do -- wallet-history
        # alone replays the *entire* local chain on every call, and
        # nothing was coalescing concurrent callers. A burst of same-
        # wallet requests (a second tab, a rapid refresh-click, a
        # wallet-switch racing the periodic poll) used to fan out into
        # that many full replays in parallel, competing for CPU and
        # dragging every one of them out -- measured over a minute under
        # just 8 concurrent requests. One lock per wallet name (not a
        # single global lock) so concurrent requests for *different*
        # wallets still run in parallel; only identical requests for the
        # same wallet coalesce onto one computation, same idea as
        # probe_lock above but scoped per name instead of process-wide.
        self.history_meta_lock = threading.Lock()
        self.history_locks = {}  # name -> Lock
        self.history_cache = {}  # name -> (monotonic_time, result_dict)
        self.history_cache_ttl = 3.0

    def _history_lock_for(self, name):
        with self.history_meta_lock:
            lock = self.history_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self.history_locks[name] = lock
            return lock

    def get_wallet_activity(self, name):
        """Cached + coalesced wallet-history/wallet-pending/reward-history
        for one named wallet -- see history_cache_ttl and
        _history_lock_for above. Raises CliError on a genuine failure
        (no such wallet, wallet-history itself failing); nothing is
        cached in that case -- the lock's own context manager releases
        it on the way out regardless, so a failure here can't wedge
        future calls the way a hand-rolled "refreshing" flag could."""
        lock = self._history_lock_for(name)
        with lock:
            cached = self.history_cache.get(name)
            if cached is not None and time.monotonic() - cached[0] < self.history_cache_ttl:
                return cached[1]
            result = self._compute_wallet_activity(name)
            self.history_cache[name] = (time.monotonic(), result)
            return result

    def _compute_wallet_activity(self, name):
        wallet_path = self.wallets.named_wallet_path(name)
        if not wallet_path.exists():
            raise CliError(f"no wallet named '{name}'")
        env = self.env_without_passphrase()

        # wallet-pending (a live peer query), wallet-history (a full
        # local chain.dat replay), and reward-history (another full
        # replay, computed independently) don't depend on each other --
        # run them concurrently instead of paying all three latencies
        # back-to-back.
        chain_path = self.workdir / "data" / "chain.dat"
        history_needed = self.workdir_initialized() and chain_path.exists()
        history_result = {}
        reward_result = {}

        def fetch_history():
            rc, out = run_binary(
                self.bin_dir / "primechain-client",
                ["wallet-history", str(chain_path), str(wallet_path), "--last", "20"],
                env,
            )
            history_result["rc"] = rc
            history_result["out"] = out

        # Mining/fee rewards aren't transactions -- they're a direct
        # ledger credit computed from record ownership (see
        # SequentialNode::credit()), so wallet-history's transaction
        # scan never sees them. reward-history recomputes them the same
        # way `rewards` does, but it's workdir-scoped (whichever wallet
        # is *currently* the canonical prime/composite identity), not
        # wallet-scoped -- so it's only meaningful, and only fetched,
        # for the wallet that's actually in that seat right now. A
        # wallet that mined in the past but was later swapped out won't
        # show those old rewards; that's the same limitation `rewards`
        # already has, not something new here.
        active = self.wallets.active_names()
        rewards_needed = history_needed and name in (active.get("prime"), active.get("composite"))

        def fetch_rewards():
            rc, out = run_binary(
                self.bin_dir / "primechain-client",
                ["reward-history", str(self.workdir), "--last", "20"],
                env,
            )
            reward_result["rc"] = rc
            reward_result["out"] = out

        history_thread = None
        if history_needed:
            history_thread = threading.Thread(target=fetch_history)
            history_thread.start()
        reward_thread = None
        if rewards_needed:
            reward_thread = threading.Thread(target=fetch_rewards)
            reward_thread.start()

        pending = []
        rc_p, out_p = run_binary(
            self.bin_dir / "primechain-client",
            ["wallet-pending", self.peer_host, str(self.peer_port), str(wallet_path)],
            env,
        )
        if rc_p == 0:
            pending = parse_wallet_pending(out_p)
        # A failed pending check (peer rate-limited, timed out) shouldn't
        # block showing confirmed history -- it just means this refresh
        # can't say anything new about in-flight transactions.

        if not history_needed:
            return {"events": pending, "synced": False}
        history_thread.join()
        if history_result["rc"] != 0:
            raise CliError(f"could not read wallet history: {history_result['out'].strip()}")
        # wallet-history prints oldest-to-newest; reversing gives newest
        # first, matching the order everything else is displayed in.
        confirmed = list(reversed(parse_wallet_history(history_result["out"])))

        rewards = []
        if reward_thread is not None:
            reward_thread.join()
            if reward_result["rc"] == 0:
                rewards = parse_reward_history(reward_result["out"])
            # A failed reward check shouldn't block the rest of Activity
            # either -- same reasoning as a failed pending check.

        # Interleave rewards into the confirmed list by the record they
        # belong to, newest first, instead of bucketing them separately
        # -- a reward and the transfers in the record that earned it
        # happened at the same moment.
        confirmed_and_rewards = sorted(confirmed + rewards, key=lambda ev: ev.get("integer", 0), reverse=True)
        return {"events": pending + confirmed_and_rewards, "synced": True}

    def get_economic_policy(self, env):
        """Cached GET_ECONOMIC_POLICY -- see policy_cache_ttl above."""
        with self.policy_lock:
            if self.policy_cache is not None and time.monotonic() - self.policy_cache[0] < self.policy_cache_ttl:
                return self.policy_cache[1]
        rc, out = run_peer_query(self.bin_dir, self.peer_host, self.peer_port, ["GET_ECONOMIC_POLICY"], env)
        if rc != 0:
            # Serve a stale value rather than nothing if we have one --
            # a fee that's a few minutes old is far more useful than no
            # fee at all for a field that rarely changes.
            with self.policy_lock:
                if self.policy_cache is not None:
                    return self.policy_cache[1]
            return {}
        values = parse_economic_policy(out)
        with self.policy_lock:
            self.policy_cache = (time.monotonic(), values)
        return values

    def smooth_frontier(self, job_status):
        """Clamp a freshly-read job_status's LOCAL_FRONTIER to never
        report below the highest value seen so far this run."""
        raw = job_status.get("LOCAL_FRONTIER")
        if raw is None:
            return job_status
        try:
            value = int(raw)
        except ValueError:
            return job_status
        with self.lock:
            if value < self.frontier_floor:
                value = self.frontier_floor
            else:
                self.frontier_floor = value
        return {**job_status, "LOCAL_FRONTIER": str(value)}

    @property
    def wallets(self):
        return WalletRegistry(self.workdir)

    # -- configuration -----------------------------------------------

    def snapshot_config(self):
        with self.lock:
            return {
                "bin_dir": str(self.bin_dir),
                "workdir": str(self.workdir),
                "peer_host": self.peer_host,
                "peer_port": self.peer_port,
                "target": self.target,
                "unlocked": self.passphrase is not None,
            }

    def update_config(self, workdir=None, peer_host=None, peer_port=None, target=None):
        # Parse/validate every field *before* touching any state. Doing
        # the int(peer_port) conversion in the same pass as assigning
        # self.workdir/self.peer_host meant a bad port (or any future
        # field with a conversion that can fail) raised mid-update,
        # leaving some fields applied and others not -- a corrupt,
        # half-saved config with no indication anything was wrong beyond
        # the error message.
        new_workdir = Path(workdir).expanduser().resolve() if workdir is not None else None
        new_peer_port = None
        if peer_port is not None:
            try:
                new_peer_port = int(peer_port)
            except (TypeError, ValueError) as exc:
                raise CliError(f"peer port must be a number: {peer_port!r}") from exc

        with self.lock:
            if self.is_mining_running():
                raise CliError("stop mining before changing configuration")
            if new_workdir is not None:
                self.workdir = new_workdir
            if peer_host is not None:
                self.peer_host = peer_host
            if new_peer_port is not None:
                self.peer_port = new_peer_port
            if target is not None:
                self.target = str(target)

    def workdir_initialized(self):
        return (self.workdir / "client.conf").exists()

    def ensure_workdir(self, env):
        """Idempotent: init-workdir only if not already done. Callers must
        already hold an unlocked env (init-workdir creates default wallet
        files if none exist yet, which needs the passphrase)."""
        with self.lock:
            if self.workdir_initialized():
                return
            self.workdir.mkdir(parents=True, exist_ok=True)
            rc, out = run_binary(
                self.bin_dir / "primechain-client",
                ["init-workdir", str(self.workdir), self.peer_host, str(self.peer_port)],
                env,
                timeout=DEFAULT_CLI_TIMEOUT_SECONDS,
            )
            if rc != 0:
                raise CliError(f"init-workdir failed: {out.strip()}")
            self.wallets.ensure_migrated_from_canonical()

    def unlock(self, passphrase):
        if not passphrase:
            raise CliError("passphrase must not be empty")
        with self.lock:
            self.passphrase = passphrase

    def lock_passphrase(self):
        with self.lock:
            if self.is_mining_running():
                raise CliError("stop mining before locking the passphrase")
            self.passphrase = None

    def require_unlocked(self):
        with self.lock:
            if self.passphrase is None:
                raise CliError("wallet is locked; call /api/unlock first")
            env = dict(os.environ)
            env["PRIMECHAIN_WALLET_PASSPHRASE"] = self.passphrase
            return env

    def env_without_passphrase(self):
        """For calls that don't need decryption (balances, job-status,
        public address lookups) -- deliberately does not require unlock."""
        return dict(os.environ)

    # -- wallet/balance probing (locked + cached; see probe_lock) -------

    def _compute_raw_wallets_info(self):
        """The expensive part: reads every named wallet's address and
        asks the peer directly for each one's live balance (GET_BALANCE
        -- a query against the network's actual current state, not our
        local chain.dat replica). A wallet's local copy only advances
        when sync-peer runs, which only happens as part of mining --
        querying the peer directly means the balance shown is always
        current regardless of whether mining is running. job-status is
        still read locally, since sync progress is legitimately about
        local state. Never call directly -- go through
        get_wallets_info(), which holds probe_lock around this so
        concurrent callers share one computation instead of racing
        their own subprocesses."""
        job_status, raw = {}, []
        if not self.workdir_initialized():
            return raw, job_status

        env = self.env_without_passphrase()
        rc, out = run_binary(self.bin_dir / "primechain-client", ["job-status", str(self.workdir)], env)
        if rc == 0:
            job_status = self.smooth_frontier(parse_job_status(out))

        # Querying every wallet's balance back-to-back in a tight loop is
        # exactly the kind of burst a peer's connection-rate-limit is
        # designed to catch -- and when one call in the middle of the
        # loop gets rejected for it, defaulting straight to 0 would
        # *cache* a wrong balance for that one wallet until the next
        # refresh happens to succeed for it. Carry forward the last
        # known-good balance per address instead, and spread the calls
        # out a little so a burst is less likely to trigger the limit
        # in the first place.
        with self.probe_lock:
            previous_raw = self.probe_cache[1][0] if self.probe_cache is not None else []
        last_known_by_addr = {w["address"]: w["total_micro_units"] for w in previous_raw if w["address"]}

        registry = self.wallets
        active = registry.active_names()
        names = registry.list_names()
        for i, name in enumerate(names):
            if i > 0:
                time.sleep(0.2)
            try:
                addr = registry.address_of(name, self.bin_dir, env)
            except CliError:
                addr = None
            total = last_known_by_addr.get(addr, 0) if addr is not None else 0
            if addr is not None:
                rc2, out2 = run_peer_query(self.bin_dir, self.peer_host, self.peer_port, ["GET_BALANCE", addr], env)
                if rc2 == 0:
                    total = parse_balance_single(out2)
            raw.append(
                {
                    "name": name,
                    "address": addr,
                    "total_micro_units": total,
                    "active_roles": [r for r, n in active.items() if n == name],
                }
            )
        return raw, job_status

    def _refresh_probe_cache_now(self):
        """Does the actual expensive work and stores the result. Called
        either inline (nothing cached yet -- this one caller has no
        choice but to wait) or from a background thread (stale-while-
        revalidate -- everyone else keeps getting the old answer,
        instantly, until this finishes).

        Must clear probe_refreshing on the way out no matter what --
        _compute_raw_wallets_info() can raise CliError (a query that
        times out, say), and a background refresh that raises without
        clearing this flag would permanently wedge every future call:
        get_wallets_info() only ever starts a new background refresh
        when probe_refreshing is False, so one failed attempt would
        otherwise freeze the cache at its last value forever."""
        try:
            result = self._compute_raw_wallets_info()
        except Exception:
            with self.probe_lock:
                self.probe_refreshing = False
            raise
        with self.probe_lock:
            self.probe_cache = (time.monotonic(), result)
            self.probe_refreshing = False

    def get_wallets_info(self, include_inert):
        """Returns (wallets_info, job_status). `include_inert`
        controls only cheap post-hoc filtering (hiding empty, unused
        default-prime/default-composite leftovers) -- it never affects
        whether the underlying probe runs or is cached, so the filtered
        and unfiltered callers (/api/state vs. the Manage tab) always
        share the same cache entry instead of doubling the work.

        Stale-while-revalidate: once anything is cached, every call
        returns immediately using it -- even if it's past its TTL -- and
        a background thread quietly refreshes it. A request only ever
        blocks on the underlying primechain-client calls the very first
        time, before there's anything to fall back on. This is what
        keeps the UI responsive against a large chain.dat on slow
        storage instead of re-blocking on every poll forever."""
        have_cache = False
        start_background_refresh = False
        with self.probe_lock:
            have_cache = self.probe_cache is not None
            if have_cache:
                is_stale = time.monotonic() - self.probe_cache[0] > self.probe_cache_ttl
                if is_stale and not self.probe_refreshing:
                    self.probe_refreshing = True
                    start_background_refresh = True

        if not have_cache:
            self._refresh_probe_cache_now()  # nothing to show yet -- have to wait
        elif start_background_refresh:
            threading.Thread(target=self._refresh_probe_cache_now, daemon=True).start()

        with self.probe_lock:
            cache = self.probe_cache
        # defensive: another thread could have invalidated the cache in
        # the instant between the check above and this read -- narrow,
        # but don't let that crash the request, just fall back to empty
        raw, job_status = cache[1] if cache is not None else ([], {})

        if include_inert:
            wallets_info = list(raw)
        else:
            wallets_info = [
                w
                for w in raw
                if not (
                    w["name"] in ("default-prime", "default-composite")
                    and w["total_micro_units"] == 0
                    and not w["active_roles"]
                )
            ]
        return wallets_info, job_status

    def invalidate_probe_cache(self):
        """Call after any wallet-mutating action (create/import/delete/
        activate/rekey) so the next /api/state reflects it immediately
        instead of waiting out the TTL."""
        with self.probe_lock:
            self.probe_cache = None

    # -- mining lifecycle ----------------------------------------------

    def is_mining_running(self):
        with self.lock:
            if self.mining_thread is not None and self.mining_thread.is_alive():
                return True
            return self.mining_process is not None and self.mining_process.poll() is None

    def _append_log(self, line):
        with self.lock:
            self.mining_log.append(line.rstrip("\n"))
            # See mining_auth_broken's docstring above -- this is the
            # exact line run-jobs logs and keeps going, so it's the only
            # signal available short of parsing every submission attempt.
            if "wallet passphrase or authentication failed" in line:
                self.mining_auth_broken = True

    def recent_log(self, since):
        with self.lock:
            lines = list(self.mining_log)
        since = max(0, min(since, len(lines)))
        return lines[since:], len(lines)

    def start_mining(self):
        env = self.require_unlocked()
        with self.lock:
            if self.is_mining_running():
                raise CliError("mining is already running")
        self.ensure_workdir(env)
        active = self.wallets.active_names()
        if not active.get("prime") or not active.get("composite"):
            raise CliError(
                "no active prime/composite wallet selected -- create or select one first"
            )
        with self.lock:
            self.mining_log.clear()
            self.mining_failed = False
            self.mining_auth_broken = False
            bin_dir, workdir, target = self.bin_dir, self.workdir, self.target
            thread = threading.Thread(
                target=self._run_mining_sequence,
                args=(env, bin_dir, workdir, target),
                daemon=True,
            )
            self.mining_thread = thread
        thread.start()

    def stop_mining(self):
        with self.lock:
            proc = self.mining_process
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)

    def _run_mining_sequence(self, env, bin_dir, workdir, target):
        # fresh sync attempt -- don't let a stale floor from a previous
        # run suppress this run's early, legitimately-low readings
        with self.lock:
            self.frontier_floor = 0

        def _sync_progress_line():
            # sync-peer prints nothing at all while it works -- a fresh
            # sync from genesis can sit silent for minutes even though
            # it's actively downloading records. Probe job-status
            # ourselves so the log shows real, moving progress instead
            # of looking stuck. Goes through get_wallets_info() (same
            # lock + stale-while-revalidate cache as /api/state) rather
            # than its own direct call -- otherwise this 4-second timer
            # and a browser's state poll could each launch their own
            # job-status against the same chain.dat at once.
            _, job_status = self.get_wallets_info(include_inert=True)
            frontier = job_status.get("LOCAL_FRONTIER")
            return f"[syncing] local frontier: {frontier}" if frontier is not None else None

        try:
            self._append_log("$ sync-peer")
            self._run_streaming(
                env,
                bin_dir,
                ["sync-peer", str(workdir)],
                timeout=SYNC_TIMEOUT_SECONDS,
                heartbeat=_sync_progress_line,
            )
            self._append_log(f"$ add-mine-job --target {target}")
            self._run_streaming(
                env,
                bin_dir,
                ["add-mine-job", str(workdir), "--target", target],
                timeout=DEFAULT_CLI_TIMEOUT_SECONDS,
            )
        except CliError as exc:
            self._append_log(f"[setup failed: {exc}]")
            with self.lock:
                self.mining_failed = True
            return

        self._append_log("$ run-jobs (streaming)")
        try:
            self._run_streaming(env, bin_dir, ["run-jobs", str(workdir)], timeout=None)
        except CliError as exc:
            self._append_log(f"[run-jobs failed: {exc}]")
            with self.lock:
                self.mining_failed = True

    def _run_streaming(self, env, bin_dir, args, timeout, heartbeat=None, heartbeat_seconds=4):
        """Run a primechain-client subcommand, appending output to the log
        line-by-line as it's produced. Raises CliError if the process can't
        start, exits non-zero, or exceeds `timeout` seconds (None = no
        timeout; used for run-jobs, which is meant to run until stopped).

        `heartbeat`, if given, is called every `heartbeat_seconds` while
        the subprocess is alive; its return value (a string, or None to
        skip) is appended to the log. Used where the subprocess itself
        stays silent for long stretches (sync-peer) so the log shows real
        progress instead of looking frozen."""
        cmd = [str(bin_dir / "primechain-client"), *args]
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,  # own process group -- see _terminate_process_tree
            )
        except OSError as exc:
            raise CliError(f"could not run {args[0]}: {exc}") from exc

        with self.lock:
            self.mining_process = proc

        timed_out = threading.Event()
        watchdog = None
        if timeout is not None:
            def _kill_on_timeout():
                timed_out.set()
                _terminate_process_tree(proc)

            watchdog = threading.Timer(timeout, _kill_on_timeout)
            watchdog.start()

        heartbeat_timer = None
        if heartbeat is not None:
            def _tick():
                nonlocal heartbeat_timer
                if proc.poll() is not None:
                    return
                try:
                    line = heartbeat()
                except Exception:  # noqa: BLE001 -- a broken probe must never kill the run
                    line = None
                if line:
                    self._append_log(line)
                if proc.poll() is None:
                    heartbeat_timer = threading.Timer(heartbeat_seconds, _tick)
                    heartbeat_timer.daemon = True
                    heartbeat_timer.start()

            heartbeat_timer = threading.Timer(heartbeat_seconds, _tick)
            heartbeat_timer.daemon = True
            heartbeat_timer.start()

        try:
            for line in proc.stdout:
                self._append_log(line)
        finally:
            code = proc.wait()
            if watchdog is not None:
                watchdog.cancel()
            if heartbeat_timer is not None:
                heartbeat_timer.cancel()
            with self.lock:
                if self.mining_process is proc:
                    self.mining_process = None

        if timed_out.is_set():
            raise CliError(f"{args[0]} timed out after {timeout}s")
        if code != 0:
            raise CliError(f"{args[0]} exited with code {code}")


def _terminate_process_tree(proc):
    """Stop `proc` and everything it spawned. primechain-client sync-peer
    launches its own worker (primechain-sync-download) as a child of the
    primechain-client process, not of us -- terminating just the Popen
    object we hold leaves that worker running as an orphan, still
    writing to the workdir, invisible to "mining stopped" in the UI.
    Started with start_new_session=True so its whole process group can
    be signaled at once instead."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_binary(binary, args, env, timeout=DEFAULT_CLI_TIMEOUT_SECONDS):
    """Run one of the primechain-* binaries to completion and return
    (returncode, combined_output). Raises CliError only if the binary
    can't be started or times out -- a non-zero return from the tool
    itself is returned, not raised, so callers can show its own message."""
    cmd = [str(binary), *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"{binary.name} timed out after {timeout}s") from exc
    except OSError as exc:
        raise CliError(f"could not run {binary.name}: {exc}") from exc
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def run_peer_query(bin_dir, host, port, args, env, timeout=DEFAULT_CLI_TIMEOUT_SECONDS):
    """Runs `primechain-client query <host> <port> <args...>` and returns
    (rc, out) like run_binary() -- except the CLI itself exits 0 as long
    as the network round-trip succeeded, even when the peer's reply is
    an "ERROR ..." line (e.g. rate limiting, an unknown address). That
    would otherwise look like a genuine "empty" answer (0 holdings, no
    nonce) instead of a failed query. Normalize it here so every caller
    checking rc gets the right answer instead of each needing to
    remember to check for a leading "ERROR" separately."""
    rc, out = run_binary(bin_dir / "primechain-client", ["query", host, str(port), *args], env, timeout=timeout)
    if rc == 0 and out.strip().startswith("ERROR"):
        return 1, out
    return rc, out


def parse_holdings(text):
    """Parses the HOLDING lines from either `primechain-client query
    <host> <port> GET_BALANCE <addr>` ('BALANCE <addr> <count>\\nHOLDING
    p amount ...\\nEND_BALANCE') or the workdir-local `balance <store>
    <wallet>` output -- both just emit a run of 'HOLDING <prime>
    <amount>' lines. Returns [{"prime": int, "micro_units": int}, ...]."""
    holdings = []
    for line in text.splitlines():
        m = re.match(r"HOLDING (\d+) (\d+)", line)
        if m:
            holdings.append({"prime": int(m.group(1)), "micro_units": int(m.group(2))})
    return holdings


def parse_balance_single(text):
    """Sums the HOLDING lines -- see parse_holdings()."""
    return sum(h["micro_units"] for h in parse_holdings(text))


def parse_job_status(text):
    status = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            status[parts[0]] = parts[1]
    return status


def parse_nonce(text):
    # "NONCE <address> <current> <next>"
    parts = text.split()
    if len(parts) >= 4 and parts[0] == "NONCE":
        return {"address": parts[1], "current": int(parts[2]), "next": int(parts[3])}
    return None


def parse_economic_policy(text):
    # "ECONOMIC_POLICY transfer_fee_micro_units=1 validator_min_reserve_micro_units=... ..."
    values = {}
    for token in text.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if value.isdigit():
            values[key] = int(value)
    return values


def parse_wallet_history(text):
    # "TX_EVENT integer=... height=... kind=... confirmations=...
    #  direction=sent|received|fee-paid tx_hash=... version=... nonce=...
    #  prime=... amount_micro_units=... amount_denominator=... sender=...
    #  receiver=..."
    events = []
    int_fields = {"integer", "height", "confirmations", "nonce", "prime", "amount_micro_units", "amount_denominator"}
    for line in text.splitlines():
        if not line.startswith("TX_EVENT"):
            continue
        fields = {}
        for token in line.split()[1:]:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            fields[key] = int(value) if key in int_fields and value.isdigit() else value
        events.append(fields)
    return events


def parse_wallet_pending(text):
    # "PENDING_TX direction=sent|received|self|fee-paid tx_hash=... version=...
    #  nonce=... prime=... amount_micro_units=... amount_denominator=...
    #  sender=... receiver=..." -- same shape as a TX_EVENT line but for a
    # transaction still sitting in the peer's mempool: no integer/height
    # (it isn't in a record yet) and no confirmations (it has none).
    events = []
    int_fields = {"nonce", "prime", "amount_micro_units", "amount_denominator"}
    for line in text.splitlines():
        if not line.startswith("PENDING_TX"):
            continue
        fields = {"pending": True}
        for token in line.split()[1:]:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            fields[key] = int(value) if key in int_fields and value.isdigit() else value
        events.append(fields)
    return events


def parse_reward_history(text):
    # "REWARD prime|composite|fee integer=... amount=... role=...
    #  [source=...] record_height=..." -- mining/fee rewards aren't
    # transactions at all (see SequentialNode::credit()), they're a
    # direct ledger credit computed from record ownership, so they
    # never show up in wallet-history's transaction scan. This is the
    # only way to see "you earned X for mining record Y" in Activity.
    events = []
    int_fields = {"integer", "amount", "source", "record_height"}
    for line in text.splitlines():
        if not line.startswith("REWARD "):
            continue
        parts = line.split()
        fields = {"reward": True, "kind": parts[1]}
        for token in parts[2:]:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            fields[key] = int(value) if key in int_fields and value.isdigit() else value
        # Normalize to the same key wallet-history uses so the frontend
        # doesn't need a separate code path just for the amount field.
        if "amount" in fields:
            fields["amount_micro_units"] = fields.pop("amount")
        events.append(fields)
    return events


def find_default_bin_dir():
    """primewallet is a standalone tool -- it isn't nested inside a
    primechain checkout, so there's no single "correct" relative path to
    the binaries. Try common locations next to this file and under the
    current directory, including "../primechain/build" (the natural
    sibling-checkout layout the README's own clone instructions
    produce); fall back to a plain "build" (relative to cwd), which
    validate_binaries() will report clearly if it's wrong. If your
    build lives somewhere else entirely (a fork, a dev branch checked
    out under its own directory name, etc.), pass --bin-dir explicitly
    rather than relying on auto-detection."""
    here = Path(__file__).resolve().parent
    candidates = [
        Path("build"),
        Path.cwd() / "build",
        here / "build",
        here.parent / "primechain" / "build",
    ]
    for candidate in candidates:
        if (candidate / "primechain-client").is_file():
            return candidate.resolve()
    return Path("build")  # validated (and reported) at startup regardless


def validate_binaries(bin_dir):
    problems = []
    for name in ("primechain-client", "primechain-wallet", "primechain-send"):
        binary = bin_dir / name
        if not binary.is_file():
            problems.append(f"missing binary: {binary} (did you build the project? see README/build instructions)")
        elif not os.access(binary, os.X_OK):
            problems.append(f"not executable: {binary}")
    return problems


# Module-level placeholder, not the real state -- Handler's methods
# close over this name and look it up at call time, by which point
# main() (via `global state`) has already replaced it with the real
# AppState built from parsed args. Declared here only so every Handler
# method can reference `state` without each needing its own `global`
# line; nothing reads it before main() runs.
state = None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet on the terminal; mining output goes through /api/log

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            data = path.read_bytes()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        if length > MAX_REQUEST_BODY_BYTES:
            raise CliError(f"request body too large ({length} bytes, max {MAX_REQUEST_BODY_BYTES})")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CliError(f"invalid JSON body: {exc}") from exc

    def _guarded(self, fn):
        try:
            fn()
        except CliError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001 -- last-resort safety net
            self._send_json({"error": f"internal error: {exc}"}, status=500)

    def do_GET(self):
        path = urlsplit(self.path).path
        routes = {
            "/": lambda: self._send_file(STATIC_DIR / "index.html", "text/html"),
            "/index.html": lambda: self._send_file(STATIC_DIR / "index.html", "text/html"),
            "/app.js": lambda: self._send_file(STATIC_DIR / "app.js", "application/javascript"),
            "/style.css": lambda: self._send_file(STATIC_DIR / "style.css", "text/css"),
        }
        if path in routes:
            return routes[path]()
        if path == "/api/state":
            return self._guarded(self._handle_state)
        if path == "/api/log":
            return self._guarded(self._handle_log)
        if path == "/api/wallets/export":
            return self._guarded(self._handle_wallet_export)
        if path == "/api/wallets/list_all":
            return self._guarded(self._handle_wallet_list_all)
        if path == "/api/wallets/holdings":
            return self._guarded(self._handle_wallet_holdings)
        if path == "/api/wallets/history":
            return self._guarded(self._handle_wallet_history)
        self.send_error(404)

    def do_POST(self):
        path = urlsplit(self.path).path
        handlers = {
            "/api/config": self._handle_config,
            "/api/unlock": self._handle_unlock,
            "/api/lock": self._handle_lock,
            "/api/wallets/create": self._handle_wallet_create,
            "/api/wallets/import": self._handle_wallet_import,
            "/api/wallets/activate": self._handle_wallet_activate,
            "/api/wallets/delete": self._handle_wallet_delete,
            "/api/wallets/rekey": self._handle_wallet_rekey,
            "/api/mining/start": self._handle_mining_start,
            "/api/mining/stop": self._handle_mining_stop,
            "/api/send": self._handle_send,
        }
        if path in handlers:
            return self._guarded(handlers[path])
        self.send_error(404)

    # -- handlers --------------------------------------------------------

    def _handle_state(self):
        cfg = state.snapshot_config()
        wallets_info, job_status = state.get_wallets_info(include_inert=False)
        self._send_json(
            {
                "config": cfg,
                "workdir_initialized": state.workdir_initialized(),
                "has_any_wallet": state.wallets.has_any_wallet(),
                "wallets": wallets_info,
                "job_status": job_status,
                "mining_running": state.is_mining_running(),
                "mining_failed": state.mining_failed,
                "mining_auth_broken": state.mining_auth_broken,
            }
        )

    def _handle_wallet_list_all(self):
        wallets_info, _ = state.get_wallets_info(include_inert=True)
        self._send_json({"wallets": wallets_info})

    def _handle_log(self):
        qs = parse_qs(urlsplit(self.path).query)
        try:
            since = int(qs.get("since", ["0"])[0])
        except (ValueError, IndexError):
            since = 0
        lines, total = state.recent_log(since)
        self._send_json({"lines": lines, "next": total})

    def _handle_config(self):
        body = self._read_json_body()
        state.update_config(
            workdir=body.get("workdir"),
            peer_host=body.get("peer_host"),
            peer_port=body.get("peer_port"),
            target=body.get("target"),
        )
        self._send_json({"config": state.snapshot_config()})

    def _handle_unlock(self):
        body = self._read_json_body()
        state.unlock(body.get("passphrase", ""))
        self._send_json({"unlocked": True})

    def _handle_lock(self):
        state.lock_passphrase()
        self._send_json({"unlocked": False})

    @staticmethod
    def _auto_assign_roles(registry, name, activate_as, first_ever):
        """Shared by create and import: decide which mining role(s) a
        newly-added wallet should take over, if any."""
        if activate_as:
            registry.set_active(activate_as, name)
        elif first_ever:
            # this call is what triggered workdir initialization -- any
            # default wallet init-workdir created as a side effect
            # belongs to no one; the wallet the user just named/imported
            # is the real first identity, so it takes both roles
            registry.set_active("prime", name)
            registry.set_active("composite", name)
        else:
            # not the first wallet, but a role might still be unset
            # (e.g. nothing has ever been activated for it): auto-fill
            # so mining/send have something to use without an extra click
            active = registry.active_names()
            for role in ("prime", "composite"):
                if not active.get(role):
                    registry.set_active(role, name)

    def _handle_wallet_create(self):
        env = state.require_unlocked()
        # capture this *before* ensure_workdir, which -- on a brand new
        # workdir -- runs init-workdir, which auto-creates and migrates
        # its own default-prime/default-composite wallets as a side
        # effect. If we checked active-role emptiness afterwards, those
        # auto-generated wallets (which the user never saw or named)
        # would win the "first wallet" race against the one they just
        # typed a name for in onboarding.
        first_ever = not state.workdir_initialized()
        state.ensure_workdir(env)
        body = self._read_json_body()
        name = (body.get("name") or "").strip()
        if not name:
            raise CliError("wallet name is required")
        registry = state.wallets
        address = registry.create(name, state.bin_dir, env)
        self._auto_assign_roles(registry, name, body.get("activate_as"), first_ever)
        state.invalidate_probe_cache()
        self._send_json({"name": name, "address": address})

    def _handle_wallet_import(self):
        env = state.require_unlocked()
        first_ever = not state.workdir_initialized()
        state.ensure_workdir(env)
        body = self._read_json_body()
        name = (body.get("name") or "").strip()
        if not name:
            raise CliError("wallet name is required")
        raw_b64 = body.get("wallet_file_b64") or ""
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CliError(f"could not decode uploaded wallet file: {exc}") from exc
        registry = state.wallets
        address = registry.import_wallet(name, data, state.bin_dir, env)
        self._auto_assign_roles(registry, name, body.get("activate_as"), first_ever)
        state.invalidate_probe_cache()
        self._send_json({"name": name, "address": address})

    def _handle_wallet_activate(self):
        env = state.require_unlocked()
        body = self._read_json_body()
        role = body.get("role")
        name = body.get("name")
        if not role or not name:
            raise CliError("role and name are required")
        if role in ("prime", "composite") and state.is_mining_running():
            raise CliError("stop mining before switching the active wallet")
        state.wallets.set_active(role, name)
        state.invalidate_probe_cache()
        self._send_json({"activated": {role: name}})

    def _handle_wallet_delete(self):
        # Deliberately no require_unlocked() -- deletion doesn't decrypt
        # anything, and it's exactly the escape hatch someone locked out
        # of a wallet (or cleaning up a leftover default-* one) needs.
        # The confirm_name field is the real safety gate here.
        body = self._read_json_body()
        name = (body.get("name") or "").strip()
        confirm = (body.get("confirm_name") or "").strip()
        if not name:
            raise CliError("wallet name is required")
        if confirm != name:
            raise CliError("confirmation name doesn't match")
        state.wallets.delete(name)
        state.invalidate_probe_cache()
        self._send_json({"deleted": name})

    def _handle_wallet_rekey(self):
        # Every wallet primewallet creates/imports gets encrypted under
        # the single passphrase this session unlocked with -- there's no
        # per-wallet passphrase tracking. So "change passphrase" rekeys
        # every wallet that currently opens under the active passphrase,
        # in one pass, and moves the session onto the new one. A wallet
        # that was imported under a *different* original passphrase
        # simply won't match here and is reported as failed, untouched --
        # it keeps its own passphrase, unlock with it again to reach it.
        env = state.require_unlocked()
        body = self._read_json_body()
        new_passphrase = body.get("new_passphrase") or ""
        if len(new_passphrase) < 4:
            raise CliError("choose a passphrase at least 4 characters long")

        registry = state.wallets
        names = registry.list_names()
        if not names:
            raise CliError("no wallets to rekey")

        rekey_env = dict(env)
        rekey_env["PRIMECHAIN_WALLET_NEW_PASSPHRASE"] = new_passphrase
        succeeded, failed = [], []
        for name in names:
            path = registry.named_wallet_path(name)
            rc, out = run_binary(state.bin_dir / "primechain-wallet", ["rekey", str(path)], rekey_env)
            if rc == 0:
                succeeded.append(name)
            else:
                failed.append({"name": name, "error": out.strip()})

        if succeeded:
            with state.lock:
                state.passphrase = new_passphrase
            # canonical prime.wallet/composite.wallet are byte copies of
            # whichever named wallet is active -- refresh any that just
            # got rekeyed so the copy matches the new ciphertext too.
            active = registry.active_names()
            for role, name in active.items():
                if name in succeeded:
                    registry.set_active(role, name)

        self._send_json({"succeeded": succeeded, "failed": failed})

    def _handle_wallet_export(self):
        # Reading a wallet's raw bytes doesn't need the passphrase (the
        # signing key inside stays encrypted either way) -- required
        # anyway so export sits behind the same access gate as every
        # other wallet-touching action.
        state.require_unlocked()
        qs = parse_qs(urlsplit(self.path).query)
        name = (qs.get("name", [""])[0]).strip()
        if not name:
            raise CliError("wallet name is required")
        path = state.wallets.named_wallet_path(name)
        if not path.exists():
            raise CliError(f"no wallet named '{name}'")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}.wallet"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_wallet_history(self):
        # Transaction history, MetaMask-Activity-tab style. Unlike
        # balances/holdings, there's no live network query for the
        # confirmed part -- wallet-history reads it out of the *local*
        # chain.dat replay, so it's only as current as the last sync
        # (recall: mining is the only thing that runs sync-peer). Doesn't
        # need unlock, same reasoning as balances: this is public
        # on-chain data, not anything requiring the signing key.
        #
        # Pending (mempool, not yet in any record) transactions *are* a
        # live query -- wallet-pending asks the peer directly, same as a
        # balance check -- so a just-submitted send shows up immediately
        # instead of only after it's mined and locally synced.
        #
        # The actual computation (wallet-history + wallet-pending +
        # reward-history, all full local replays or live peer queries)
        # lives in AppState.get_wallet_activity() -- cached and coalesced
        # per wallet name, so concurrent identical requests share one
        # computation instead of each spawning their own subprocesses.
        qs = parse_qs(urlsplit(self.path).query)
        name = (qs.get("name", [""])[0]).strip()
        if not name:
            raise CliError("wallet name is required")
        self._send_json(state.get_wallet_activity(name))

    def _handle_wallet_holdings(self):
        # A live GET_BALANCE query, same as the total-balance path -- but
        # returning the actual per-prime-asset breakdown, not just the
        # sum. Without this, sending requires knowing offhand which
        # prime asset # a wallet holds anything of, with no way to find
        # out from inside the app itself. Doesn't need unlock: reading
        # holdings is a public query, same as checking a balance.
        #
        # Also includes the network's current transfer fee. It isn't a
        # free choice -- an authenticated transfer is only valid if its
        # fee exactly equals the active protocol fee (verified in
        # SequentialNode's tx validation), so a client-side "pick any
        # fee" field would just be a way to submit transactions that
        # silently fail to validate. Fetch the real one instead of
        # hardcoding a guess that can go stale if the network's fee
        # policy ever changes.
        qs = parse_qs(urlsplit(self.path).query)
        name = (qs.get("name", [""])[0]).strip()
        if not name:
            raise CliError("wallet name is required")
        env = state.env_without_passphrase()
        address = state.wallets.address_of(name, state.bin_dir, env)
        rc, out = run_peer_query(state.bin_dir, state.peer_host, state.peer_port, ["GET_BALANCE", address], env)
        stale = False
        if rc != 0:
            with state.holdings_lock:
                cached = state.holdings_cache.get(name)
            if cached is None:
                raise CliError(f"could not read holdings: {out.strip()}")
            holdings = cached[1]
            stale = True
        else:
            holdings = parse_holdings(out)
            with state.holdings_lock:
                state.holdings_cache[name] = (time.monotonic(), holdings)
        fee = state.get_economic_policy(env).get("transfer_fee_micro_units")
        self._send_json({"address": address, "holdings": holdings, "transfer_fee_micro_units": fee, "stale": stale})

    def _handle_mining_start(self):
        state.start_mining()
        self._send_json({"started": True})

    def _handle_mining_stop(self):
        state.stop_mining()
        self._send_json({"stopped": True})

    def _handle_send(self):
        env = state.require_unlocked()
        body = self._read_json_body()

        sender_name = (body.get("from") or "").strip()
        if not sender_name:
            raise CliError("'from' wallet name is required")
        receiver = (body.get("receiver") or "").strip()
        if not receiver:
            raise CliError("receiver address is required")
        try:
            prime = int(body.get("prime"))
            amount = int(body.get("amount"))
            fee = int(body.get("fee", 1))
        except (TypeError, ValueError) as exc:
            raise CliError("prime, amount, and fee must be integers") from exc
        if prime < 2 or amount <= 0 or fee < 0:
            raise CliError("prime must be >= 2, amount > 0, fee >= 0")
        # primechain-send parses these with std::stoull, which throws an
        # uncaught C++ exception (not a clean error) on anything bigger
        # than a uint64_t -- confirmed live: it crashes the subprocess
        # outright ("terminate called after throwing an instance of
        # 'std::out_of_range'") instead of failing gracefully. Reject it
        # here with a normal error instead of spawning a process
        # guaranteed to crash.
        UINT64_MAX = 2**64 - 1
        if prime > UINT64_MAX or amount > UINT64_MAX or fee > UINT64_MAX:
            raise CliError(f"prime, amount, and fee must each fit in 64 bits (max {UINT64_MAX})")

        registry = state.wallets
        wallet_path = registry.named_wallet_path(sender_name)
        if not wallet_path.exists():
            raise CliError(f"no wallet named '{sender_name}'")

        sender_address = registry.address_of(sender_name, state.bin_dir, env)

        rc, nonce_out = run_peer_query(state.bin_dir, state.peer_host, state.peer_port, ["GET_NONCE", sender_address], env)
        nonce_info = parse_nonce(nonce_out) if rc == 0 else None
        if nonce_info is None:
            raise CliError(f"could not fetch nonce: {nonce_out.strip()}")

        rc, send_out = run_binary(
            state.bin_dir / "primechain-send",
            [
                "submit",
                state.peer_host,
                str(state.peer_port),
                str(wallet_path),
                receiver,
                str(prime),
                str(amount),
                str(fee),
                str(nonce_info["next"]),
            ],
            env,
        )
        if rc != 0:
            reason = send_out.strip()
            if "wallet passphrase or authentication failed" in reason:
                # Every wallet primewallet creates/imports is encrypted
                # under whatever passphrase was active at the time --
                # there's no per-wallet passphrase tracking (see rekey's
                # docstring). A wallet imported from elsewhere, or
                # created before a later passphrase change that didn't
                # reach it, genuinely won't open under the session's
                # current passphrase. That's not something retrying or
                # rekeying (which itself needs to open the wallet first)
                # can fix from here -- say so plainly instead of
                # surfacing the raw decrypt-failure string.
                raise CliError(
                    f"'{sender_name}' doesn't open with the current passphrase -- "
                    "it was likely created or imported under a different one. "
                    "Lock and unlock with that wallet's own passphrase to use it."
                )
            raise CliError(f"send failed: {reason}")
        # The frontend deliberately re-fetches Activity right after a
        # successful send so the new pending entry shows up immediately
        # (see refreshAccountActivity in app.js) -- without dropping the
        # cached entry here, a send arriving within history_cache_ttl of
        # the last fetch would just get served that same stale,
        # pre-send snapshot back, silently defeating the whole point.
        with state._history_lock_for(sender_name):
            state.history_cache.pop(sender_name, None)
        self._send_json({"result": send_out.strip(), "nonce_used": nonce_info["next"]})


def main():
    global state
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bin-dir", default=None, help="directory with the built primechain-* binaries (default: auto-detected build/ next to this checkout)")
    parser.add_argument("--workdir", default="~/pc-launch-testnet", help="primechain-client workdir (default: ~/pc-launch-testnet); can also be changed from the browser")
    parser.add_argument("--peer-host", default="192.81.209.230", help="default bootstrap validator host; can be changed from the browser")
    parser.add_argument("--peer-port", type=int, default=8339)
    parser.add_argument("--target", default="999999999", help="mine-job target passed to add-mine-job")
    parser.add_argument("--listen-port", type=int, default=8765)
    args = parser.parse_args()

    bin_dir = Path(args.bin_dir).expanduser().resolve() if args.bin_dir else find_default_bin_dir()
    problems = validate_binaries(bin_dir)
    if problems:
        sys.exit(
            "\n".join(problems)
            + "\n\nPass --bin-dir explicitly if the build output isn't at "
            + str(bin_dir)
        )

    state = AppState(
        bin_dir=bin_dir,
        workdir=Path(args.workdir).expanduser().resolve(),
        peer_host=args.peer_host,
        peer_port=args.peer_port,
        target=args.target,
    )

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.listen_port), Handler)
    print(f"primechain-dashboard listening on http://127.0.0.1:{args.listen_port}/")
    print("(127.0.0.1 only -- do not expose this port to the network)")
    print("Open that URL in a browser to set up your workdir/peer and unlock a passphrase.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_mining()


if __name__ == "__main__":
    main()
