param(
    [Parameter(Mandatory=$true)][string]$QueueFile,
    [Parameter(Mandatory=$true)][ValidateRange(1,100000)][int]$ExpectedCount,
    [string]$Output = "$env:LOCALAPPDATA\SelectedDownloader\downloads",
    [string]$Python = "$env:LOCALAPPDATA\SelectedDownloader\.venv\Scripts\python.exe",
    [string]$Browser = 'C:\Program Files\Google\Chrome\Application\chrome.exe',
    [string]$Profile = "$env:LOCALAPPDATA\SelectedDownloader\capture-profile"
)
$ErrorActionPreference = 'Stop'
$env:PATH = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;" + $env:PATH
if (!(Test-Path -LiteralPath $QueueFile -PathType Leaf)) { throw 'Queue file is missing' }
# Windows PowerShell 5 treats redirected native stderr as ErrorRecords, even
# when FFmpeg is only printing progress. The process exit code is authoritative.
$ErrorActionPreference = 'Continue'
& $Python -u (Join-Path $PSScriptRoot 'download_from_captured_m3u8.py') `
    --url-file $QueueFile --expected-count $ExpectedCount --output $Output `
    --no-auto-expand --no-discover-dropdowns --no-top-right-click --parallel 1 `
    --capture-timeout 120 --skip-existing --browser-executable $Browser --user-data-dir $Profile
exit $LASTEXITCODE
