#!/bin/sh
# Install the AssetsBoard pre-commit guard into this clone's .git/hooks (run from the project root).
set -e
cd "$(dirname "$0")/.."
[ -d .git ] || { echo "no .git here — nothing to install"; exit 0; }
cat > .git/hooks/pre-commit <<'H'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/tools/precommit_scan.py"
H
chmod +x .git/hooks/pre-commit tools/precommit_scan.py
echo "installed .git/hooks/pre-commit"
