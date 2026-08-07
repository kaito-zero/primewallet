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

    def create(self, name, bin_dir, env):
        path = self.named_wallet_path(name)
        if path.exists():
            raise CliError(f"a wallet named '{name}' already exists")
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
            raise CliError(f"a wallet named '{name}' already exists")
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
        with self.lock:
            if self.is_mining_running():
                raise CliError("stop mining before changing configuration")
            if workdir is not None:
                self.workdir = Path(workdir).expanduser().resolve()
            if peer_host is not None:
                self.peer_host = peer_host
            if peer_port is not None:
                self.peer_port = int(peer_port)
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

        registry = self.wallets
        active = registry.active_names()
        for name in registry.list_names():
            try:
                addr = registry.address_of(name, self.bin_dir, env)
            except CliError:
                addr = None
            total = 0
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
        instantly, until this finishes)."""
        result = self._compute_raw_wallets_info()
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


def find_default_bin_dir():
    """primewallet is a standalone tool -- it isn't nested inside a
    primechain checkout, so there's no single "correct" relative path to
    the binaries. Try common locations next to this file and under the
    current directory; fall back to a plain "build" (relative to cwd),
    which validate_binaries() will report clearly if it's wrong."""
    here = Path(__file__).resolve().parent
    candidates = [
        Path("build"),
        Path.cwd() / "build",
        here / "build",
        here.parent / "primechain" / "build",
        here.parent / "primechain3" / "build",
        here.parent / "primechain2" / "build",
        # primechain-pr is an active development fork, rebuilt often --
        # rebuilding it while it's the binaries a live run-jobs/sync-peer
        # process is executing from can hit "Text file busy". It has
        # useful not-yet-upstreamed fixes (e.g. rekey), so it's still a
        # candidate, just last: point --bin-dir at it explicitly if you
        # want it as the default instead.
        here.parent / "primechain-pr" / "build",
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


state = None  # AppState, set in main()


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

    def _handle_wallet_holdings(self):
        # A live GET_BALANCE query, same as the total-balance path -- but
        # returning the actual per-prime-asset breakdown, not just the
        # sum. Without this, sending requires knowing offhand which
        # prime asset # a wallet holds anything of, with no way to find
        # out from inside the app itself. Doesn't need unlock: reading
        # holdings is a public query, same as checking a balance.
        qs = parse_qs(urlsplit(self.path).query)
        name = (qs.get("name", [""])[0]).strip()
        if not name:
            raise CliError("wallet name is required")
        env = state.env_without_passphrase()
        address = state.wallets.address_of(name, state.bin_dir, env)
        rc, out = run_peer_query(state.bin_dir, state.peer_host, state.peer_port, ["GET_BALANCE", address], env)
        if rc != 0:
            raise CliError(f"could not read holdings: {out.strip()}")
        self._send_json({"address": address, "holdings": parse_holdings(out)})

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
            raise CliError(f"send failed: {send_out.strip()}")
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
