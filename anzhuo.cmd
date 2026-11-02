@echo off
rem Force UTF-8 mode: anzhuo reads/writes all text as UTF-8, GBK default breaks it
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\anzhuo.exe" %*
