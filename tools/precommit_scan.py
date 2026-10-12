#!/usr/bin/env python3
"""Pre-commit guard for AssetsBoard: block commits that would leak private data.

Blocks, in the staged diff (added lines only) and staged file names:
  1. any value from the local `.env` (project root, gitignored) — matched literally, case-insensitive;
  2. any address / id from the private configs (archive/wallets/wallets.json, archive/etherfi_cash/config.json);
  3. any token whose sha256 (lower-cased) is listed in tools/private_hashes.txt (known real addresses / account ids —
     only hashes are committed, so this works on machines without the .env);
  4. secret-like patterns (private-key blocks, exchange / GitHub / AWS / Slack keys, long literals assigned to
     key/secret/token/password names);
  5. private files: .env (except .env.example), wallets.json, analysis_overrides.json, anything under archive/,
     snapshots/, exports/, imports/ (except .gitkeep).
A line can opt out of check 4 only with the marker `allowlist-secret` (for published doc example keys).
Matches are reported by file:line and rule, never by value. Exit 1 = blocked.
Install: sh tools/install_hooks.sh   (bypass in an emergency: git commit --no-verify — don't).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HASHES = ROOT / "tools" / "private_hashes.txt"
TOKEN = re.compile(r"[A-Za-z0-9_]{7,80}")
SECRET_PATTERNS = [
    ("private-key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("MEXC api key", re.compile(r"\bmx0v[A-Za-z0-9]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("secret literal", re.compile(r"(?i)(api_?key|secret|token|passphrase|password)[\"']?\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']")),
]
PRIVATE_FILE = re.compile(r"(^|/)\.env(\..+)?$|(^|/)wallets\.json$|(^|/)analysis_overrides\.json$|^(archive|snapshots|exports|imports)/")
SAFE_FILE = re.compile(r"(^|/)\.env\.example$|(^|/)\.gitkeep$")


def sha(v: str) -> str:
    return hashlib.sha256(v.strip().lower().encode()).hexdigest()


def private_values(root: Path = ROOT) -> set[str]:
    vals: set[str] = set()
    sys.path.insert(0, str(root))
    try:
        from assetsboard import envconfig
        env = envconfig.parse((root / ".env").read_text(encoding="utf-8")) if (root / ".env").exists() else {}
    except Exception:
        env = {}
    vals |= {v for v in env.values() if len(v) >= 6}
    for p, pick in ((root / "archive/wallets/wallets.json", lambda d: [w.get("address") for w in d.get("wallets") or []]),
                    (root / "archive/etherfi_cash/config.json", lambda d: [d.get("vault")])):
        try:
            vals |= {str(a) for a in pick(json.loads(p.read_text(encoding="utf-8"))) if a and "DEADBEEF" not in str(a).upper()}
        except (OSError, ValueError, AttributeError):
            pass
    return {v for v in vals if len(v) >= 6}


LOCAL_HASHES = ROOT / ".git" / "assetsboard_private_hashes.txt"   # clone-local, never committed


def known_hashes(*paths: Path) -> set[str]:
    out: set[str] = set()
    for path in paths or (HASHES, LOCAL_HASHES):
        try:
            out |= {l.split()[0] for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")}
        except OSError:
            pass
    return out


def scan(files: dict[str, list[tuple[int, str]]], values: set[str], hashes: set[str]) -> list[str]:
    """files: path -> [(line_no, added_line)]. Returns findings (no values)."""
    out: list[str] = []
    low = [v.lower() for v in values]
    for f, lines in files.items():
        if PRIVATE_FILE.search(f) and not SAFE_FILE.search(f):
            out.append(f"{f}: private file must not be committed")
            continue
        for n, line in lines:
            ll = line.lower()
            if any(v in ll for v in low):
                out.append(f"{f}:{n}: contains a value from .env / private config")
            elif any(sha(t) in hashes for t in TOKEN.findall(line)):
                out.append(f"{f}:{n}: contains a known private address / account id")
            elif "allowlist-secret" not in line:
                for rule, rx in SECRET_PATTERNS:
                    if rx.search(line):
                        out.append(f"{f}:{n}: looks like a secret ({rule})")
                        break
    return out


def staged() -> dict[str, list[tuple[int, str]]]:
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR"],
                          cwd=ROOT, capture_output=True, text=True, errors="replace").stdout
    files: dict[str, list[tuple[int, str]]] = {}
    cur, n = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
            if cur:
                files.setdefault(cur, [])
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            n = int(m.group(1)) if m else 0
        elif cur and line.startswith("+"):
            files[cur].append((n, line[1:]))
            n += 1
    return files


def main() -> int:
    found = scan(staged(), private_values(), known_hashes())
    if found:
        print("pre-commit: blocked — private data in staged changes:", file=sys.stderr)
        for x in found:
            print("  " + x, file=sys.stderr)
        print("Move the value to .env (gitignored) and use a fake placeholder in code/tests.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
