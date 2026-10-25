$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'

# Use a regular Python installation, or the bundled runtime on this computer.
$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $ErrorActionPreference = 'Continue'
    $detected = & py -3 -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0) { $candidates += $detected }
    $ErrorActionPreference = 'Stop'
}
$bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$candidates += $bundled
$python = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $python) {
    Write-Host 'Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/ and run setup.cmd again.'
    exit 1
}
& $python -c 'import sys; assert (3,10) <= sys.version_info[:2] <= (3,13)'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $python -m venv --without-pip .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m pip --python .venv install pip -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Ready. Double-click web.cmd for the browser UI, or use run.cmd for the CLI.'
