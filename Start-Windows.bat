@echo off
rem GlassMCP: запуск (Windows). Ничего не ставит в систему:
rem портативный Python -> .\python, зависимости -> .venv, cloudflared -> .\bin.
setlocal
cd /d "%~dp0"
set PY_TAG=20261110
set PY_VER=3.12.16

rem ---------- 1) Python ----------
if exist "python\python.exe" goto :py_ok
if exist ".venv\Scripts\python.exe" goto :use_venv_py
echo -> Python not found - downloading portable build...
set ASSET=cpython-%PY_VER%%%2B%PY_TAG%-x86_64-pc-windows-msvc-install_only.tar.gz
set URL=https://github.com/astral-sh/python-build-standalone/releases/download/%PY_TAG%/%ASSET%
if not exist .pydl mkdir .pydl
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri $env:URL -OutFile '.pydl\py.tgz' } catch { exit 1 }" || (echo ERROR: download failed & exit /b 1)
tar -xzf .pydl\py.tgz -C .pydl
rmdir /s /q python 2>nul
move .pydl\python python >nul
rmdir /s /q .pydl
set PY=python\python.exe
goto :venv
:use_venv_py
set PY=.venv\Scripts\python.exe
goto :deps
:py_ok
set PY=python\python.exe

:venv
if exist ".venv\Scripts\python.exe" goto :deps
echo -> creating virtual environment...
"%PY%" -m venv .venv || (echo ERROR: venv failed & exit /b 1)

:deps
echo -> dependencies...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip 2>nul
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo ERROR: pip failed & exit /b 1)

rem ---------- 2) token ----------
if exist ".env" findstr /b /c:"MCP_TOKEN=" .env >nul 2>&1 && goto :start
".venv\Scripts\python.exe" -c "import secrets;open('.env','w').write('MCP_TOKEN='+secrets.token_urlsafe(32))"
echo -> generated new token (.env)

:start
if not exist logs mkdir logs
set DASH_PORT=8090
set MCP_PORT=8000
for /f %%p in ('.venv\Scripts\python.exe -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c.get('dashboard_port',8090), c.get('port',8000))" 2^>nul') do (
  for /f "tokens=1,2" %%a in ("%%p") do (
    set DASH_PORT=%%a
    set MCP_PORT=%%b
  )
)
set WANT_TUNNEL=0
for %%a in (%*) do if /i "%%a"=="--tunnel" set WANT_TUNNEL=1

rem уже запущен?
powershell -NoProfile -Command "if (Test-Path .server_pid) { $p=Get-Content .server_pid; try { Get-Process -Id $p | Out-Null; exit 0 } catch { exit 1 } } else { exit 1 }" && (
  echo already running. Panel: http://127.0.0.1:%DASH_PORT%
  start "" "http://127.0.0.1:%DASH_PORT%"
  goto :eof
)
echo -> starting GlassMCP...
powershell -NoProfile -Command "$p = Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '-m','server' -WorkingDirectory (Get-Location) -WindowStyle Hidden -RedirectStandardOutput 'logs\server.log' -RedirectStandardError 'logs\server.err.log' -PassThru; Set-Content -Path '.server_pid' -Value $p.Id"
timeout /t 3 /nobreak >nul
if "%WANT_TUNNEL%"=="1" (
  echo -> starting tunnel...
  powershell -NoProfile -Command "try { Invoke-WebRequest -Uri ('http://127.0.0.1:'+$env:DASH_PORT+'/api/tunnel/start') -Method POST -ContentType 'application/json' -Body '{\"mode\":\"quick\"}' -UseBasicParsing | Out-Null } catch {}"
)
start "" "http://127.0.0.1:%DASH_PORT%"
echo GlassMCP started:  panel http://127.0.0.1:%DASH_PORT%  ^•  MCP http://127.0.0.1:%MCP_PORT%/mcp
if "%WANT_TUNNEL%"=="1" echo tunnel: URL will appear in the panel within ~30 s
echo stop: Stop-Windows.bat
endlocal
