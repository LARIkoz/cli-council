#!/usr/bin/env bash
# install.sh — entry point for the strict install contract (installer/CONTRACT.md).
#
# The star path is AGENT-DRIVEN: open this repo in Claude Code (or any agent that
# can run commands) and say "install cli-council per installer/CONTRACT.md". The
# agent runs the gates, installs the vendor CLIs you pick, and hands you each
# login link. This script is the manual fallback: it detects what you have and
# points you at the next gate. It never installs anything without you.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

command -v python3 >/dev/null || { echo "python3 (3.11+) required"; exit 1; }

echo "cli-council — install contract (manual fallback)"
echo "Full gated flow, agent-driven: installer/CONTRACT.md"
echo
echo "== Gate 0: detect =="
python3 installer/doctor.py detect
echo
echo "== Next =="
echo "1. Pick optional voices (native Claude is already your default)."
echo "2. Install + log in each via its official flow (see the table in installer/CONTRACT.md)."
echo "3. Smoke each:   python3 installer/doctor.py smoke <voice>"
echo "4. Enrol PASSes: python3 installer/doctor.py enroll claude <voice>..."
echo "5. Dry-run:      python3 -m council \"a test question\""
echo
echo "Put $REPO/bin on your PATH to call 'council' from anywhere."
