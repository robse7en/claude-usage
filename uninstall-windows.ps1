# Remove Claude usage login autostart.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$removed = $false
if (Get-ItemProperty -Path $RunKey -Name "ClaudeUsage" -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $RunKey -Name "ClaudeUsage"
    Write-Host "Removed ClaudeUsage autostart entry."
    $removed = $true
}
if (Get-ItemProperty -Path $RunKey -Name "ClaudeResetNotifier" -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $RunKey -Name "ClaudeResetNotifier"
    Write-Host "Removed old ClaudeResetNotifier autostart entry."
    $removed = $true
}
if (-not $removed) {
    Write-Host "ClaudeUsage autostart entry was not present."
}
