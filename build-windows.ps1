# Build the Claude usage tray as a single Windows executable.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Log {
    param([string]$Msg)
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $Msg"
}

function Resolve-Python {
    $candidates = @(
        @("py", "-3.11"),
        @("py", "-3"),
        @("python")
    )
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $candidateArgs = @()
        if ($candidate.Count -gt 1) {
            $candidateArgs = $candidate[1..($candidate.Count - 1)]
        }
        try {
            & $exe @candidateArgs -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @{
                    Exe = $exe
                    Args = $candidateArgs
                    UsesUv = $false
                }
            }
        } catch {
        }
    }
    throw "Python 3.11+ was not found. Install Python or uv, then rerun this build script."
}

$Root = $PSScriptRoot
if (-not $Root) {
    $Root = (Get-Location).Path
}

Log "=== Claude Usage EXE Build ==="
Log "Root: $Root"

$env:UV_CACHE_DIR = Join-Path $Root ".uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root ".uv-python"
$VenvDir = Join-Path $Root ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Log "Creating virtual environment with uv..."
        & $uv.Source venv --python 3.11 $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }
    } else {
        $Python = Resolve-Python
        Log "Creating virtual environment..."
        & $Python.Exe @($Python.Args) -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }
    }
}

$Python = @{
    Exe = $PythonExe
    Args = @()
}

Log "Installing build dependencies..."
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    & $uv.Source pip install --python $Python.Exe --quiet -r (Join-Path $Root "requirements.txt") pyinstaller
} else {
    & $Python.Exe @($Python.Args) -m pip install --quiet -r (Join-Path $Root "requirements.txt") pyinstaller
}
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

Log "Running tests..."
& $Python.Exe @($Python.Args) -m unittest test_packaging.py
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

Log "Building single-file executable..."
& $Python.Exe @($Python.Args) -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name ClaudeUsage `
    --add-data "$(Join-Path $Root "run-claude-minimized.ps1");." `
    --hidden-import pystray._win32 `
    --collect-submodules PIL `
    (Join-Path $Root "tray_windows.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Log "Build complete: $(Join-Path $Root "dist\ClaudeUsage.exe")"
