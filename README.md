# primewallet

A local wallet and mining control panel for [primechain](https://github.com/midlincoln/primechain), in the shape of a browser extension wallet (MetaMask/Phantom-style) rather than a big dashboard: an account switcher, one account card at a time (address, balance, mining-role toggles), send/receive, and a collapsible activity log.

It's a thin UI over the existing `primechain-client` / `primechain-wallet` / `primechain-send` CLI tools -- it doesn't reimplement wallet crypto, signing, proof construction, or networking. Every action in the browser is one of those binaries run as a subprocess, the same commands the CLI docs already walk you through by hand.

## What it does

- Set up a workdir and bootstrap peer from the browser -- no config file editing.
- Unlock with a wallet passphrase, held in the server process's memory only, never written to disk or logged.
- Create any number of named wallets. Each one can independently hold the prime-mining role, the composite-mining role, both, or neither -- switch which wallet does what per role, per wallet, from its own account card, the same way you'd switch accounts in a browser wallet.
- Start/stop mining (`sync-peer` -> `add-mine-job` -> `run-jobs`) with live streaming output.
- Check balances and send transactions between wallets.

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

If the binaries aren't found automatically (checked next to this checkout, under `../primechain/build`, and under `./build`), point at them directly:

```bash
python3 server.py --bin-dir /path/to/primechain/build
```

`--workdir`, `--peer-host`, `--peer-port`, `--target`, and `--listen-port` are also available; see `--help`.

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
</content>
