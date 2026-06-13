# Install Claude usage tray as a per-user Windows login task.

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
                }
            }
        } catch {
        }
    }
    throw "Python 3.11+ was not found. Install Python from python.org and ensure the launcher or python.exe is available."
}

$Root = $PSScriptRoot
if (-not $Root) {
    $Root = (Get-Location).Path
}

Log "=== Claude Usage Install ==="
Log "Root: $Root"

if ($Root -match '\\\\wsl(\$|\.localhost)\\') {
    throw "Install from a native Windows path, not a WSL share: $Root"
}

$Python = Resolve-Python
$VenvDir = Join-Path $Root ".venv"
if (-not (Test-Path $VenvDir)) {
    Log "Creating virtual environment..."
    & $Python.Exe @($Python.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }
}

$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"
Log "Installing dependencies..."
& $PythonExe -m pip install --quiet -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Log "Configure Pushover from the tray menu after startup."

$BasePrefix = & $PythonExe -c "import sys; print(sys.base_exec_prefix)"
$BasePythonw = Join-Path $BasePrefix "pythonw.exe"
$Script = Join-Path $Root "tray_windows.py"
$Command = "`"$BasePythonw`" `"$Script`""

Log "Registering login autostart..."
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
New-Item -Path $RunKey -Force | Out-Null
Remove-ItemProperty -Path $RunKey -Name "ClaudeResetNotifier" -ErrorAction SilentlyContinue
Set-ItemProperty -Path $RunKey -Name "ClaudeUsage" -Value $Command

Log "Starting tray controller..."
Start-Process -FilePath $BasePythonw -ArgumentList "`"$Script`"" -WorkingDirectory $Root -WindowStyle Hidden

Log "Install complete"
Log "Logs: $env:LOCALAPPDATA\ClaudeUsage\notifier.log"
