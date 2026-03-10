@echo off
cd /d "%~dp0"
echo.
echo  Generating and publishing live site...
echo.

python generate_static.py
if errorlevel 1 (
    echo.
    echo  ERROR: Site generation failed. Check above for details.
    pause
    exit /b 1
)

git add docs/index.html
git commit -m "Publish live site update"
git push

echo.
echo  Done! Site will be live in 1-2 minutes.
echo  Visit: https://wadeco2000.github.io/pspla-checker/
echo.
pause
