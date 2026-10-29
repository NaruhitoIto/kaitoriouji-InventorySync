@echo off
rem NOTE: keep this file ASCII-only.
rem Builds dist\ShippingExport.exe from shipping_export.py (Python is needed only on the build PC).
rem --paths .. bundles the shared API modules in the parent folder
rem (rakuten_rms_api.py, wasabi_api.py, yahoo_shopping_api.py).
cd /d "%~dp0"
if not exist .venv-build python -m venv .venv-build
.venv-build\Scripts\python -m pip install --quiet --upgrade pyinstaller requests
.venv-build\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --paths .. --name ShippingExport shipping_export.py
echo.
echo Done: dist\ShippingExport.exe
pause
