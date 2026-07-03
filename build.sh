#!/usr/bin/env bash
# Builds a standalone Claude Usage Widget for macOS or Linux.
# Run on the target platform (a macOS build must be made on a Mac, etc.).
set -euo pipefail

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt pyinstaller

# --windowed gives a .app bundle on macOS; on Linux it produces a GUI binary.
pyinstaller --noconfirm --onefile --windowed --name "ClaudeUsageWidget" main.py

echo
if [[ "$(uname)" == "Darwin" ]]; then
  echo "Build complete: dist/ClaudeUsageWidget.app"
  echo "Drag it to /Applications, or run: open dist/ClaudeUsageWidget.app"
else
  echo "Build complete: dist/ClaudeUsageWidget"
  echo "Copy it anywhere on your PATH and run it."
fi
