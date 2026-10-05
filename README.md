# AssetsBoard (AB)

**AssetsBoard** is a **read-only** multi-venue investment dashboard: CEX (Bitget, MEXC, Kraken, Crypto.com App), Interactive Brokers (local IB Gateway + Flex Query), and public on-chain wallets (e.g. Arbitrum, Bittensor).

- **No trading, no withdrawals, no wallet private keys.**
- Exchange API keys stay in the **macOS Keychain** (or environment variables).
- Local web UI / native Mac app on **127.0.0.1:47651** (never uses 8765).
- Python **stdlib only** for exchange/wallet clients (optional `pywebview` + `certifi` for the Mac app).

短名 **AB**。儀表板與 CLI 預設繁體中文；本 README 以英文為主。

## Requirements

- Python 3.11+ (3.12 recommended on macOS via python.org)
- macOS for Keychain, AssetsBoard.app, IB Gateway, and the weekly LaunchAgent
- Read-only API keys on each exchange (see per-exchange notes in docs below)

## Quick start

```bash
git clone https://github.com/BoHuang2018/AssetsBoard.git ~/AssetsBoard
cd ~/AssetsBoard
python3 -m venv .venv
.venv/bin/pip install certifi pywebview   # Mac app / TLS for python.org builds
.venv/bin/python -m assetsboard --help
```

### Store API keys (macOS Keychain)

```bash
cd ~/AssetsBoard
.venv/bin/python -m assetsboard setup-keychain --exchange bitget      # BITGET_*
.venv/bin/python -m assetsboard setup-keychain --exchange mexc
.venv/bin/python -m assetsboard setup-keychain --exchange kraken
.venv/bin/python -m assetsboard setup-keychain --exchange cryptocom
.venv/bin/python -m assetsboard setup-keychain --exchange ibkr-flex   # Flex token + query id
```

Keychain **service name** is `assetsboard`. Existing items under the legacy service `bitguard` are still **read** automatically (your old keys keep working; this repo does not migrate or delete them).

### On-chain wallets (no keys)

Copy the example config and edit **your** addresses (never commit this file):

```bash
mkdir -p archive/wallets
cp wallets.example.json archive/wallets/wallets.json
# edit addresses, then:
.venv/bin/python -m assetsboard archive --exchange wallets
```

### Dashboard

```bash
.venv/bin/python -m assetsboard archive          # read-only fetch + archive
.venv/bin/python -m assetsboard analyze
.venv/bin/python -m assetsboard web              # http://127.0.0.1:47651/
# or native window:
tools/build_mac_app.sh && open ~/Applications/AssetsBoard.app
```

## Migrating from BitGuard / `~/bitguard`

| Old | New |
|-----|-----|
| Product **BitGuard** | **AssetsBoard** (AB) |
| Package `bitguard` | `assetsboard` |
| `python -m bitguard` | `python -m assetsboard` |
| `~/bitguard` | Prefer `~/AssetsBoard` (weekly script still falls back to `~/bitguard`) |
| App `BitGuard.app` | `AssetsBoard.app` |
| LaunchAgent `com.bitguard.mexc-archive` | `com.assetsboard.weekly-archive` |
| Keychain service `bitguard` | Writes `assetsboard`; **still reads** `bitguard` |

Copy or rename your local folder, keep `.venv` or recreate it, and point LaunchAgent / app build scripts at the new path. **Do not** commit `archive/`, `exports/`, or `snapshots/`.

## Safety model

- Exchange modules use **strict allowlists** of read-only endpoints (writes refused locally).
- IBKR live: only localhost Gateway when **you** ask (not on the weekly LaunchAgent). Outgoing TWS messages allowlisted; prefer Gateway “Read-Only API”.
- IBKR Flex: monthly HTTP fetch in the weekly job (token in Keychain) — **no Gateway** required.
- Wallets: public RPC / explorer APIs only.
- Refresh token + Host checks on the local web server.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Layout

```
assetsboard/     Python package (CLI, clients, analysis, static UI)
tests/           Unit tests (signing, allowlists, offline fixtures)
tools/           Mac app builder, weekly archive script, LaunchAgent plist example
wallets.example.json
archive/         Local only (gitignored) — raw history
snapshots/       Local only (gitignored) — analysis output
exports/         Local only (gitignored) — tarballs for sync
```

## License

Use at your own risk. This is personal portfolio tooling, not financial advice.

### ether.fi Cash（開銷帳戶）
複製 `etherfi_cash.example.json` → `archive/etherfi_cash/config.json`，填入 Cash vault 地址（`enabled: true`），再執行：
```bash
.venv/bin/python -m assetsboard archive --exchange etherfi-cash
.venv/bin/python -m assetsboard analyze
```
餘額與 Arbitrum USDC 儲值來自公開瀏覽器 API；卡片商戶明細無公開 API，里程碑涵蓋率以儲值為開銷代理。開銷帳戶 float **不計入**投資總額／投資盈虧。

### IBKR: manual live refresh (no scheduled Gateway)

The weekly LaunchAgent does **not** open or query IB Gateway. For live NAV/positions:

1. Open and log into IB Gateway (API on, Read-Only, port 4001).
2. Ask AssetsBoard to refresh, or run `archive --exchange ibkr`.

**Flex Query** (deposits, dividends, fees, history) still runs **monthly** via HTTP (`ibkr-flex-fetch`) in `tools/weekly_archive.sh` and does **not** need Gateway.
