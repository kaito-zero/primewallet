# primewallet

A local wallet and mining control panel for [primechain](https://github.com/midlincoln/primechain), in the shape of a browser extension wallet (MetaMask/Phantom-style) rather than a big dashboard: an account switcher, one account card at a time (address, balance, mining-role toggles), send/receive, and a collapsible activity log.

It's a thin UI over the existing `primechain-client` / `primechain-wallet` / `primechain-send` CLI tools -- it doesn't reimplement wallet crypto, signing, proof construction, or networking. Every action in the browser is one of those binaries run as a subprocess, the same commands the CLI docs already walk you through by hand.

## What it does

- Set up a workdir and bootstrap peer from the browser -- no config file editing.
- Unlock with a wallet passphrase, held in the server process's memory only, never written to disk or logged.
- Create any number of named wallets, or import one from a backup `.wallet` file. Each one can independently hold the prime-mining role, the composite-mining role, both, or neither -- switch which wallet does what per role, per wallet, from its own account card, the same way you'd switch accounts in a browser wallet.
- Start/stop mining (`sync-peer` -> `add-mine-job` -> `run-jobs`) with live streaming output; a persistent banner shows mining status on every screen, since a mining process can outlive a wallet's own bookkeeping.
- Check balances and send transactions between wallets.
- Export a wallet to a `.wallet` file, or change the passphrase for every wallet in the workdir that currently opens with it, from Settings.
- Manage tab lists every wallet in the workdir (including ones the account switcher hides because they're empty and unused) and lets you delete any of them -- doesn't require being unlocked, since deleting a wallet you're locked out of is exactly the point.

## Requirements

A built primechain checkout with the `primechain-client`, `primechain-wallet`, and `primechain-send` binaries:

```bash
git clone https://github.com/midlincoln/primechain.git
cd primechain
git submodule update --init
cmake -S . -B build
cmake --build build -- -j2
```

Python 3, standard library only -- nothing to `pip install`.

## Run it

```bash
python3 server.py
```

Then open `http://127.0.0.1:8765/`. Workdir, peer, and passphrase are all set from the browser on first run.

If the binaries aren't found automatically (checked under `./build` and a `build/` folder next to a few sibling primechain checkouts -- see `find_default_bin_dir()` in `server.py` for the exact search order), point at them directly:

```bash
python3 server.py --bin-dir /path/to/primechain/build
```

Change-passphrase needs a `primechain-wallet` with the `rekey` subcommand, which isn't in upstream primechain yet ([PR pending](https://github.com/midlincoln/primechain/pulls)) -- everything else works against a plain upstream build.

`--workdir`, `--peer-host`, `--peer-port`, `--target`, and `--listen-port` are also available; see `--help`.

## How mining runs

Asked about explicitly by the primechain maintainer, since multi-wallet
role switching can otherwise be mistaken for a protocol-level effect
rather than a client one:

- **One process, one proof store.** primewallet drives exactly one
  `run-jobs` subprocess against exactly one workdir/proof store at a
  time. There's no per-identity process fan-out and no shared proof
  store across separate processes.
- **One prime/composite pair, never several identities in parallel.**
  At most one wallet is designated the prime miner and one the
  composite miner (the same wallet can hold both). primewallet does
  not run multiple mining identities concurrently to increase win
  odds -- that's a deliberate scope decision, not a limitation we
  didn't notice. If you want that, run the plain CLI's `run-jobs`
  yourself against separate workdirs/identities; primewallet is a
  normal single-identity wallet client, not a mining-optimization tool.
- **Role reassignment is serialized with mining state.** The server
  refuses to switch which wallet holds a role while `run-jobs` is
  running (matching the CLI's own constraint) -- role changes only
  take effect between runs, never mid-run. The UI's "Start mining"
  action on a wallet that isn't the current miner stops the running
  process, reassigns both roles to that wallet, and restarts -- always
  with an explicit confirmation first, never silently.
- **Balances are live, sync progress is local.** Wallet balances come
  from a direct `GET_BALANCE` query to the configured peer, independent
  of whether mining/sync-peer has run recently. Only sync-progress
  display (frontier height, job status) reads the local workdir replica.

## Security notes

- Binds to `127.0.0.1` only. This process holds a decryption passphrase in memory and controls your mining process and wallet files -- it must never be exposed to the network.
- The passphrase is never written to disk, never logged, and is passed to the CLI subprocesses the same way `PRIMECHAIN_WALLET_PASSPHRASE` already works on the command line.
- No telemetry, no external network calls beyond what you configure as your peer.

## Layout

```
server.py          stdlib-only HTTP server + REST API, wraps the CLI binaries
static/index.html  markup
static/app.js       UI logic, polls /api/state and /api/log
static/style.css    styling
```
