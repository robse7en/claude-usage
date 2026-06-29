[CmdletBinding()]
param(
    [int]$PromptDelaySeconds = 5,
    [int]$CloseDelaySeconds = 2,
    [switch]$Show,
    [switch]$LeaveOpen
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PromptDelaySeconds -lt 0) {
    throw 'PromptDelaySeconds must be 0 or greater.'
}

if ($CloseDelaySeconds -lt 0) {
    throw 'CloseDelaySeconds must be 0 or greater.'
}

Get-Command claude -ErrorAction Stop | Out-Null

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace Win32
{
    public static class NativeMethods
    {
        [DllImport("user32.dll")]
        public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hWnd);
    }
}
'@

$selfHandle = [System.Diagnostics.Process]::GetCurrentProcess().MainWindowHandle
if ($selfHandle -ne [IntPtr]::Zero) {
    [void][Win32.NativeMethods]::ShowWindowAsync($selfHandle, 6)
}

$childArgs = @(
    '-NoLogo'
    '-NoExit'
    '-Command'
    'claude'
)

$windowStyle = if ($Show) { 'Normal' } else { 'Minimized' }
$child = Start-Process -FilePath 'powershell.exe' -ArgumentList $childArgs -WindowStyle $windowStyle -PassThru

try {
    $windowReady = $false

    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        if ($child.HasExited) {
            break
        }

        $child.Refresh()

        if ($child.MainWindowHandle -ne 0) {
            $windowReady = $true
            break
        }

        Start-Sleep -Milliseconds 100
    }

    if (-not $windowReady) {
        throw 'The Claude console window did not become ready in time.'
    }

    Start-Sleep -Seconds $PromptDelaySeconds

    if (-not $child.HasExited) {
        if (-not $Show) {
            [void][Win32.NativeMethods]::ShowWindowAsync($child.MainWindowHandle, 9)
        }

        [void][Win32.NativeMethods]::SetForegroundWindow($child.MainWindowHandle)

        Start-Sleep -Milliseconds 250
        $wshell = New-Object -ComObject WScript.Shell
        $wshell.SendKeys('{ENTER}')

        if (-not $Show) {
            Start-Sleep -Milliseconds 100
            [void][Win32.NativeMethods]::ShowWindowAsync($child.MainWindowHandle, 6)
        }

        if (-not $LeaveOpen) {
            Start-Sleep -Seconds $CloseDelaySeconds
        }
    }
}
finally {
    if (-not $LeaveOpen -and $null -ne $child -and -not $child.HasExited) {
        & taskkill /F /T /PID $child.Id 2>$null
    }
}
