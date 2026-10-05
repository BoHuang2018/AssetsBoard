"""`setup-keychain [--exchange bitget|mexc|kraken]`: store read-only API values in the macOS login Keychain.

Items: generic password, service "assetsboard" (reads legacy "bitguard" too), account = BITGET_API_KEY / _SECRET / _PASSPHRASE
(or MEXC_API_KEY / MEXC_API_SECRET with --exchange mexc, KRAKEN_API_KEY / KRAKEN_API_SECRET with --exchange kraken).
Values are never printed, logged or put on a command line:
- default: `security add-generic-password ... -w` with -w last, so `security` itself prompts (hidden, twice);
- --getpass: read with getpass here and feed `security -i` through stdin (not visible in `ps`).
"""
from __future__ import annotations

import getpass
import subprocess
import sys

from .client import cli_cmd, KEYCHAIN_SERVICE, KRAKEN_SECRET_NAMES, MEXC_SECRET_NAMES, SECRET_NAMES, SECURITY_BIN, keychain_has, keychain_supported, keychain_read

LABELS = {"BITGET_API_KEY": "Bitget API Key", "BITGET_API_SECRET": "Bitget Secret Key", "BITGET_API_PASSPHRASE": "Bitget Passphrase",
          "MEXC_API_KEY": "MEXC Access Key", "MEXC_API_SECRET": "MEXC Secret Key",
          "KRAKEN_API_KEY": "Kraken API Key", "KRAKEN_API_SECRET": "Kraken Private Key",
          "IBKR_FLEX_TOKEN": "IBKR Flex Web Service Token", "IBKR_FLEX_QUERY_ID": "IBKR Activity Flex Query ID",
          "CRYPTOCOM_API_KEY": "Crypto.com Agent API Key", "CRYPTOCOM_API_SECRET": "Crypto.com Agent Secret Key"}
EXCHANGES = {"bitget": ("Bitget", SECRET_NAMES), "mexc": ("MEXC", MEXC_SECRET_NAMES), "kraken": ("Kraken", KRAKEN_SECRET_NAMES),
             "ibkr-flex": ("IBKR Flex", ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID")),
             "cryptocom": ("Crypto.com App（Agent API Key）", ("CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET"))}


def _base(name: str) -> list[str]:
    return ["add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-l", f"assetsboard {name}",
            "-j", "assetsboard read-only exchange API credential"]


def _store_security_prompt(name: str) -> bool:
    print(f"  security 會要求輸入兩次（輸入時不會顯示）：", flush=True)
    r = subprocess.run([SECURITY_BIN, *_base(name), "-w"])
    return r.returncode == 0


def _store_getpass(name: str) -> bool:
    v1 = getpass.getpass(f"  {LABELS[name]}（不會顯示）：").strip()
    v2 = getpass.getpass("  再輸入一次：").strip()
    if not v1 or v1 != v2:
        print("  兩次輸入不一致或為空，略過。")
        return False
    if any(ch in v1 for ch in "\"\\\n\r") or " " in v1:
        print("  值含有空白、引號或反斜線，--getpass 模式無法安全傳遞；請改用預設模式（不加 --getpass）。")
        return False
    cmd = (f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a {name} -l "assetsboard {name}" '
           f'-j "assetsboard read-only exchange API credential" -w "{v1}"\n')
    r = subprocess.run([SECURITY_BIN, "-i"], input=cmd, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    del v1, v2, cmd
    return r.returncode == 0 and "error" not in (r.stderr or "").lower()


def run(use_getpass: bool = True, only: list[str] | None = None, use_security_prompt: bool = False,
        exchange: str = "bitget") -> int:
    if not keychain_supported():
        print("setup-keychain 只支援 macOS（找不到 /usr/bin/security）。", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("請在終端機（Terminal）直接執行，需要互動輸入。", file=sys.stderr)
        return 2
    title, all_names = EXCHANGES[exchange]
    names = [n for n in all_names if not only or n in only]
    if not names:
        print(f"--only 指定的項目不屬於 {title}（{', '.join(all_names)}）", file=sys.stderr)
        return 2
    print(f"=== 將 {title} 唯讀 API 憑證存入 macOS 鑰匙圈（服務名稱 assetsboard（若無則讀取舊的 bitguard））===")
    print("值不會顯示、不會寫入任何檔案；之後 assetsboard（含 AssetsBoard.app）會從鑰匙圈讀取。")
    print("提醒：請只使用「唯讀」權限的 API Key。\n")
    ok_all = True
    for n in names:
        print(f"[{LABELS[n]}] {n}：{'已存在 → 覆寫' if keychain_has(n, legacy=False) else '新增'}")
        ok = _store_security_prompt(n) if use_security_prompt else _store_getpass(n)
        saved = bool(ok and keychain_has(n, legacy=False) and keychain_read(n))
        if ok and not saved:
            print("  ✘ 鑰匙圈裡的值是空的，請重新執行（預設模式）")
        print("  ✔ 已儲存" if saved else "  ✘ 未儲存")
        ok_all &= saved
    if ok_all and exchange == "cryptocom":
        from .cryptocom import secret_problem as cdc_problem
        bad = cdc_problem(keychain_read("CRYPTOCOM_API_KEY"), keychain_read("CRYPTOCOM_API_SECRET"))
        if bad:
            print(f"\n  ✘ {bad}")
            ok_all = False
    if ok_all and exchange == "kraken":
        from .kraken import secret_problem
        bad = secret_problem(keychain_read("KRAKEN_API_KEY"), keychain_read("KRAKEN_API_SECRET"))
        if bad:
            print(f"\n  ✘ {bad}")
            ok_all = False
    print()
    if ok_all:
        print(f"完成。驗證（唯讀查詢，在 ~/AssetsBoard 執行）：{cli_cmd()} " + ("ibkr-flex-fetch" if exchange == "ibkr-flex" else f"perms --exchange {exchange}"))
        print("第一次由 App 讀取鑰匙圈時，macOS 可能會跳出「允許存取」視窗，請按「永遠允許」。")
        print("刪除：security delete-generic-password -s assetsboard -a <名稱>")
    return 0 if ok_all else 1
