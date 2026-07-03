@echo off
REM Builds a single-file Windows .exe for the Claude Usage Widget.
REM Run this from a Windows machine (not the sandbox) with Python installed.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --onefile --windowed --name "ClaudeUsageWidget" main.py

echo.
echo Build complete. Find ClaudeUsageWidget.exe in the dist\ folder.
echo Copy it anywhere you like (e.g. C:\Tools\ClaudeUsageWidget\) and run it.
pause
