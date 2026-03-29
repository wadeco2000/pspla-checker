@echo off
setlocal enabledelayedexpansion

REM Kill any existing deploy monitor windows before starting
taskkill /fi "WINDOWTITLE eq PSPLA Deploy Monitor" /f >NUL 2>NUL

title PSPLA Deploy Monitor
echo.
echo  =============================================
echo   PSPLA Deploy Monitor
echo   Started: %date% %time%
echo  =============================================
echo.

REM Test beep (uses PC speaker, works even if audio is muted)
powershell -Command "[console]::beep(600,200)" 2>NUL
echo  Audio check: if you heard a beep, you're good
echo  (Beeps use PC speaker so they work even if volume is muted)
echo.

REM ---- Git info ----
echo  ----- Latest Commit -----
pushd C:\Users\WadeAdmin\pspla-checker 2>NUL

for /f "delims=" %%a in ('git log -1 --format^="%%h" 2^>NUL') do set GIT_HASH=%%a
for /f "delims=" %%a in ('git log -1 --format^="%%an" 2^>NUL') do set GIT_AUTHOR=%%a
for /f "delims=" %%a in ('git log -1 --format^="%%ai" 2^>NUL') do set GIT_DATE=%%a
for /f "delims=" %%a in ('git log -1 --format^="%%s" 2^>NUL') do set GIT_MSG=%%a

echo   Commit:  !GIT_HASH!
echo   Author:  !GIT_AUTHOR!
echo   Date:    !GIT_DATE!
echo   Message: !GIT_MSG!
echo  -------------------------
echo.
popd

REM ---- PHASE 1: GitHub Actions ----
echo  [Phase 1/3] Checking GitHub Actions deploy workflow...
echo.

REM Grab deploy details — find latest non-skipped run (in_progress, queued, or success)
gh run list --repo wadeco2000/pspla-checker --workflow deploy-dashboard.yml --limit 5 --json displayTitle,headSha,createdAt,status,conclusion --jq "[.[] | select(.conclusion != \"skipped\")] | .[0].displayTitle" > %TEMP%\pspla_gh_title.txt 2>NUL
set GH_TITLE=
for /f "delims=" %%a in (%TEMP%\pspla_gh_title.txt) do set GH_TITLE=%%a

gh run list --repo wadeco2000/pspla-checker --workflow deploy-dashboard.yml --limit 5 --json headSha,status,conclusion --jq "[.[] | select(.conclusion != \"skipped\")] | .[0].headSha[0:7]" > %TEMP%\pspla_gh_sha.txt 2>NUL
set GH_SHA=
for /f "delims=" %%a in (%TEMP%\pspla_gh_sha.txt) do set GH_SHA=%%a

gh run list --repo wadeco2000/pspla-checker --workflow deploy-dashboard.yml --limit 5 --json createdAt,status,conclusion --jq "[.[] | select(.conclusion != \"skipped\")] | .[0].createdAt" > %TEMP%\pspla_gh_created.txt 2>NUL
set GH_CREATED=
for /f "delims=" %%a in (%TEMP%\pspla_gh_created.txt) do set GH_CREATED=%%a

if defined GH_TITLE echo   Deploy:  !GH_TITLE!
if defined GH_SHA echo   Commit:  !GH_SHA!
if defined GH_CREATED echo   Started: !GH_CREATED!
echo.

:gh_loop
gh run list --repo wadeco2000/pspla-checker --workflow deploy-dashboard.yml --limit 5 --json status,conclusion --jq "[.[] | select(.conclusion != \"skipped\")] | .[0].status + \" \" + (.[0].conclusion // \"\")" > %TEMP%\pspla_gh.txt 2>NUL
set GH_STATUS=
set GH_CONCLUSION=
for /f "tokens=1,2" %%a in (%TEMP%\pspla_gh.txt) do (
    set GH_STATUS=%%a
    set GH_CONCLUSION=%%b
)

if "!GH_STATUS!"=="" (
    echo  [%time%] GitHub Actions: could not fetch status, retrying...
    ping -n 11 127.0.0.1 >NUL
    goto gh_loop
)

echo  [%time%] GitHub Actions: !GH_STATUS! / !GH_CONCLUSION!

if "!GH_STATUS!"=="completed" goto gh_done

ping -n 11 127.0.0.1 >NUL
goto gh_loop

:gh_done

REM Grab finish time
gh run list --repo wadeco2000/pspla-checker --workflow deploy-dashboard.yml --limit 5 --json updatedAt,conclusion --jq "[.[] | select(.conclusion != \"skipped\")] | .[0].updatedAt" > %TEMP%\pspla_gh_fin.txt 2>NUL
set GH_FINISHED=
for /f "delims=" %%a in (%TEMP%\pspla_gh_fin.txt) do set GH_FINISHED=%%a

if "!GH_CONCLUSION!"=="success" (
    echo.
    echo  GitHub Actions: BUILD SUCCESSFUL
    if defined GH_FINISHED echo  Finished: !GH_FINISHED!
    powershell -Command "[console]::beep(700,150)" 2>NUL
) else (
    echo.
    echo  GitHub Actions: FAILED [!GH_CONCLUSION!]
    echo  Check: https://github.com/wadeco2000/pspla-checker/actions
    powershell -Command "[console]::beep(300,500); Start-Sleep -m 200; [console]::beep(300,500)" 2>NUL
    powershell -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('GitHub Actions deploy FAILED!', 'Deploy Failed', 'OK', 'Error')"
    pause
    exit /b 1
)
echo.

REM ---- PHASE 2: Azure container startup ----
echo  [Phase 2/3] Waiting for Azure container to start...
echo  (This can take several minutes)
echo.

:azure_loop
curl -s -o NUL -w "%%{http_code}" --max-time 10 https://pspla-checker-eub7bpa0gphthhgh.newzealandnorth-01.azurewebsites.net/health > %TEMP%\pspla_poll.txt 2>NUL
set AZ_CODE=
set /p AZ_CODE=<%TEMP%\pspla_poll.txt

set AZ_MSG=unknown
if "!AZ_CODE!"=="000" set AZ_MSG=no response - container is down
if "!AZ_CODE!"=="200" set AZ_MSG=OK - app is running
if "!AZ_CODE!"=="301" set AZ_MSG=redirect to custom domain - app is running
if "!AZ_CODE!"=="302" set AZ_MSG=redirect to login - app is running
if "!AZ_CODE!"=="500" set AZ_MSG=server error - app crashed
if "!AZ_CODE!"=="502" set AZ_MSG=bad gateway - container starting
if "!AZ_CODE!"=="503" set AZ_MSG=not ready yet - container still booting
if "!AZ_CODE!"=="504" set AZ_MSG=timeout - container still booting

echo  [%time%] Azure: HTTP !AZ_CODE! - !AZ_MSG!

if "!AZ_CODE!"=="200" goto azure_done
if "!AZ_CODE!"=="301" goto azure_done
if "!AZ_CODE!"=="302" goto azure_done

ping -n 16 127.0.0.1 >NUL
goto azure_loop

:azure_done
echo.
echo  Azure container: UP
powershell -Command "[console]::beep(700,150)" 2>NUL
echo.

REM ---- PHASE 3: Custom domain ----
echo  [Phase 3/3] Checking custom domain...
echo.

:domain_loop
curl -s -o NUL -w "%%{http_code}" --max-time 10 https://www.psplachecker.co.nz/health > %TEMP%\pspla_poll2.txt 2>NUL
set SITE_CODE=
set /p SITE_CODE=<%TEMP%\pspla_poll2.txt

set SITE_MSG=unknown
if "!SITE_CODE!"=="000" set SITE_MSG=no response - not reachable
if "!SITE_CODE!"=="200" set SITE_MSG=OK - site is live
if "!SITE_CODE!"=="301" set SITE_MSG=redirect - site is live
if "!SITE_CODE!"=="302" set SITE_MSG=redirect to login - site is live
if "!SITE_CODE!"=="500" set SITE_MSG=server error - app crashed
if "!SITE_CODE!"=="502" set SITE_MSG=bad gateway - not ready
if "!SITE_CODE!"=="503" set SITE_MSG=not ready yet - still booting
if "!SITE_CODE!"=="504" set SITE_MSG=timeout - still booting

echo  [%time%] Domain: HTTP !SITE_CODE! - !SITE_MSG!

if "!SITE_CODE!"=="200" goto site_done
if "!SITE_CODE!"=="301" goto site_done
if "!SITE_CODE!"=="302" goto site_done

ping -n 11 127.0.0.1 >NUL
goto domain_loop

:site_done
echo.
echo  =============================================
echo   ALL SYSTEMS GO!
echo.
echo   Commit:          !GIT_HASH! - !GIT_MSG!
echo   Deploy:          !GH_TITLE!
echo   Build started:   !GH_CREATED!
echo   Build finished:  !GH_FINISHED!
echo.
echo   GitHub Actions:  SUCCESS
echo   Azure container: HTTP !AZ_CODE!
echo   Custom domain:   HTTP !SITE_CODE!
echo   Completed:       %date% %time%
echo  =============================================
echo.

REM Play success melody
powershell -Command "[console]::beep(523,200); Start-Sleep -m 100; [console]::beep(659,200); Start-Sleep -m 100; [console]::beep(784,200); Start-Sleep -m 100; [console]::beep(1047,400)" 2>NUL

REM Windows notification
powershell -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('PSPLA site deployed and live!', 'Deploy Complete', 'OK', 'Information')"

REM ---- OPTIONAL: Call Server check ----
echo.
echo  [Optional] Checking call server health...
curl -s --max-time 5 https://gemini-call-server-dqd4b6a8dtdpezcx.newzealandnorth-01.azurewebsites.net/health > %TEMP%\pspla_cs.txt 2>NUL
set CS_VER=
for /f "tokens=*" %%a in ('powershell -Command "(Get-Content %TEMP%\pspla_cs.txt | ConvertFrom-Json).version" 2^>NUL') do set CS_VER=%%a
if defined CS_VER (
    echo   Call server: OK - version !CS_VER!
) else (
    echo   Call server: not reachable or no version
)
echo.

pause
