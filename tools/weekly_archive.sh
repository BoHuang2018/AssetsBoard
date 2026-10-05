#!/bin/bash
# Weekly MEXC + Kraken + Crypto.com + on-chain wallets + IBKR archive + snapshot, run by launchd (com.assetsboard.weekly-archive).
# MEXC, Kraken and Crypto.com keys live only in this Mac's Keychain (service assetsboard; legacy bitguard still read); IBKR is read from the local
# IB Gateway (127.0.0.1:4001, read-only API) — so the box cannot do this.
# Each source runs independently: one failing (or the Gateway being closed / logged out) never blocks the others.
# Exports for the box routine: ~/AssetsBoard/exports/{mexc,kraken,cryptocom,wallets,ibkr}_archive.tgz
# Log: ~/Library/Logs/AssetsBoard-archive.log
set -u
LOG="$HOME/Library/Logs/AssetsBoard-archive.log"
mkdir -p "$(dirname "$LOG")"
# keep the log small: keep the last 2000 lines if it grows over 1 MB
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exec >>"$LOG" 2>&1
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1
if [ -d "$HOME/AssetsBoard" ]; then cd "$HOME/AssetsBoard"
elif [ -d "$HOME/bitguard" ]; then cd "$HOME/bitguard"; echo "NOTE: using legacy ~/bitguard (prefer ~/AssetsBoard)"
else echo "FAIL: ~/AssetsBoard (or legacy ~/bitguard) not found"; exit 1; fi

export_dir() {  # export_dir <archive subdir> <name>
  mkdir -p exports && COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf "exports/$2.tmp" "$1" && mv "exports/$2.tmp" "exports/$2" \
    && echo "export: ~/AssetsBoard/exports/$2 ($(du -h "exports/$2" | cut -f1))" || { echo "export $2 FAILED"; return 3; }
}

# ---- MEXC
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') MEXC weekly start ====="
.venv/bin/python -m assetsboard archive --exchange mexc && .venv/bin/python -m assetsboard snapshot --exchange mexc
rc_m=$?
[ $rc_m -eq 0 ] && { export_dir archive/mexc mexc_archive.tgz || rc_m=3; }
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') MEXC weekly $([ $rc_m -eq 0 ] && echo OK || echo "FAILED (exit $rc_m)") ====="

# ---- Kraken (skipped quietly until KRAKEN_API_KEY / KRAKEN_API_SECRET are in the Keychain)
rc_k=0
if .venv/bin/python -c "import sys; from assetsboard import kraken; sys.exit(0 if kraken.available() else 1)"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') Kraken weekly start ====="
  # archive also writes a holdings snapshot into archive/kraken/YYYY-MM/snapshots (the first run reads the full ledger)
  # (no separate `snapshot --exchange kraken`: that would only write a second copy into snapshots/, which is not exported)
  .venv/bin/python -m assetsboard archive --exchange kraken
  rc_k=$?
  # export even after a partial archive error: what was archived is valid and the cursor only advances on success
  if [ -d archive/kraken ]; then export_dir archive/kraken kraken_archive.tgz || rc_k=3; fi
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') Kraken weekly $([ $rc_k -eq 0 ] && echo OK || echo "FAILED (exit $rc_k)") ====="
else
  echo "Kraken: no credentials in Keychain, skipped"
fi

# ---- Crypto.com App (Agent API Key, read-only GET allowlist; skipped quietly until CRYPTOCOM_API_KEY / _SECRET are in the Keychain)
# The weekly call also keeps the key alive: Crypto.com expires an Agent API Key after 30 days without any call.
rc_c=0
if .venv/bin/python -c "import sys; from assetsboard import cryptocom; sys.exit(0 if cryptocom.available() else 1)"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') Crypto.com weekly start ====="
  # archive = snapshot (first one becomes the PnL baseline) + incremental transactions.jsonl
  .venv/bin/python -m assetsboard archive --exchange cryptocom
  rc_c=$?
  [ $rc_c -eq 2 ] && echo "⚠ Crypto.com: API KEY REJECTED (expired or revoked?) — create a new Agent API Key in the App, then: cd ~/AssetsBoard && .venv/bin/python -m assetsboard setup-keychain --exchange cryptocom"
  .venv/bin/python - <<'PY' 2>/dev/null
from pathlib import Path
from assetsboard import cryptocom
k = cryptocom.key_status(Path("archive/cryptocom"))
if k.get("expires"):
    print(f"Crypto.com key: expires {k['expires'][:10]}{' (assumed: first use + 30 d)' if k.get('expires_assumed') else ''}, {k['days_left']:.0f} days left")
if k.get("warning"):
    print("⚠ Crypto.com key: " + k["warning"])
PY
  if [ -d archive/cryptocom ]; then export_dir archive/cryptocom cryptocom_archive.tgz || rc_c=3; fi
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') Crypto.com weekly $([ $rc_c -eq 0 ] && echo OK || echo "FAILED (exit $rc_c)") ====="
else
  echo "Crypto.com: no credentials in Keychain, skipped"
fi

# ---- On-chain wallets (public RPC / explorer; no keys; always attempted)
rc_w=0
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') wallets weekly start ====="
.venv/bin/python -m assetsboard archive --exchange wallets
rc_w=$?
if [ -d archive/wallets ]; then export_dir archive/wallets wallets_archive.tgz || rc_w=3; fi
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') wallets weekly $([ $rc_w -eq 0 ] && echo OK || echo "FAILED (exit $rc_w)") ====="

# ---- IBKR (only when IB Gateway is running and logged in; otherwise logged and skipped, not an error)
rc_i=0
# Flex Web Service (deposits, dividends, fees, full trade history) — monthly: the query period is "last month", so fetch on the
# first Monday of the month (day 1–7), or as a catch-up when the newest Flex import is older than 35 days (or missing).
# Only runs once a token is stored; independent of the Gateway.
flex_age=$(.venv/bin/python - <<'PY' 2>/dev/null
import json, datetime as dt
try:
    st = json.load(open("archive/ibkr/flex/state.json"))
    t = max(dt.datetime.fromisoformat(i["imported_oslo"]) for i in st["imports"])
    print((dt.datetime.now(t.tzinfo) - t).days)
except Exception:
    print(9999)
PY
)
dom=$((10#$(date +%d)))
if [ "$dom" -le 7 ] || [ "${flex_age:-9999}" -gt 35 ]; then
  echo "IBKR Flex: monthly fetch (day $dom, last import ${flex_age:-?} days ago)"
  .venv/bin/python -m assetsboard ibkr-flex-fetch --if-configured || echo "IBKR Flex: fetch failed (exit $?), continuing"
else
  echo "IBKR Flex: not due (day $dom, last import ${flex_age} days ago; runs on the first Monday of the month)"
fi
if .venv/bin/python -c "import sys; from assetsboard import ibkr; sys.exit(0 if ibkr.available() else 1)"; then
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') IBKR weekly start ====="
  .venv/bin/python -m assetsboard archive --exchange ibkr
  rc_i=$?
  [ $rc_i -eq 2 ] && { echo "IBKR: Gateway not ready (logged out?), skipped"; rc_i=0; }
  if [ -d archive/ibkr ]; then export_dir archive/ibkr ibkr_archive.tgz || rc_i=3; fi
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') IBKR weekly $([ $rc_i -eq 0 ] && echo OK || echo "FAILED (exit $rc_i)") ====="
else
  echo "IBKR: IB Gateway not running on 127.0.0.1:4001, skipped"
  if [ -d archive/ibkr/flex ]; then export_dir archive/ibkr ibkr_archive.tgz || rc_i=3; fi
fi


# ---- ether.fi Cash (開銷帳戶)
rc_e=0
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ether.fi Cash weekly start ====="
.venv/bin/python -m assetsboard archive --exchange etherfi-cash
rc_e=$?
[ -d archive/etherfi_cash ] && export_dir archive/etherfi_cash etherfi_cash_archive.tgz || true
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ether.fi Cash weekly $([ $rc_e -eq 0 ] && echo OK || echo "FAILED (exit $rc_e)") ====="

rc=$rc_m; [ $rc_k -gt $rc ] && rc=$rc_k; [ $rc_c -gt $rc ] && rc=$rc_c; [ $rc_w -gt $rc ] && rc=$rc_w; [ $rc_e -gt $rc ] && rc=$rc_e; [ $rc_i -gt $rc ] && rc=$rc_i
exit $rc
