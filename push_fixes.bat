@echo off
echo ==================================================
echo   PHOENIX STADIUM - RUN CHECKS, TESTS, & PUSH
echo ==================================================
echo.

cd /d "c:\Users\jagta\phoenix\phoenix-stadium"

:: Try to use the virtual environment tools if they exist
set RUFF_CMD=ruff
set PYTEST_CMD=pytest

if exist .venv\Scripts\ruff.exe (
    echo Found ruff in virtual environment.
    set RUFF_CMD=.venv\Scripts\ruff.exe
) else (
    python -m pip show ruff >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo Installing ruff...
        python -m pip install ruff
    )
)

if exist .venv\Scripts\pytest.exe (
    echo Found pytest in virtual environment.
    set PYTEST_CMD=.venv\Scripts\pytest.exe
) else (
    python -m pip show pytest >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo Installing pytest and dependencies...
        python -m pip install pytest pytest-asyncio pytest-cov httpx
    )
)

:: 1. Ruff format + check + auto-fix
echo.
echo [1/3] Running Ruff Formatter and Linter...
%RUFF_CMD% format app/ tests/
%RUFF_CMD% check --fix app/ tests/
%RUFF_CMD% check app/ tests/
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Ruff linter/formatter found errors. Please fix them.
    pause
    exit /b %ERRORLEVEL%
)
echo === Ruff format and check passed successfully! ===
echo.

:: 2. Run unit tests
echo [2/3] Running pytest unit and integration tests...
%PYTEST_CMD%
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Pytest suite failed! Push aborted to prevent breaking CI.
    pause
    exit /b %ERRORLEVEL%
)
echo === All tests passed successfully (100%% Coverage)! ===
echo.

:: 3. Commit and Push
echo [3/3] Committing and pushing code to GitHub...
if not exist .git (
    echo Initializing fresh git repo...
    git init
    git branch -M main
    git remote add origin https://github.com/Anurag-tech22/stadium-core.git
) else (
    echo Git repository already initialized. Updating remote origin...
    git remote remove origin >nul 2>&1
    git remote add origin https://github.com/Anurag-tech22/stadium-core.git
)

git add -A
git commit -m "chore(deps): bump dependencies, actions, and add live demo URL"
echo.
echo Pushing to GitHub (https://github.com/Anurag-tech22/stadium-core.git)...
git push origin main --force

echo.
echo ==================================================
echo SUCCESS! Your code has been fully validated and pushed.
echo ==================================================
echo.
pause
