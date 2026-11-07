@echo off
REM Roda o monitor no Windows e reinicia sozinho se cair.
REM Dica: crie um atalho deste arquivo na pasta shell:startup para iniciar com o Windows.
cd /d "%~dp0\.."
:loop
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe main.py
) else (
  python main.py
)
echo Monitor parou. Reiniciando em 30s (Ctrl+C para cancelar)...
timeout /t 30 /nobreak >nul
goto loop
