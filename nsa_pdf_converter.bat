@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
python nsa_convertor.py
pause