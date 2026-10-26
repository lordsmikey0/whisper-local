param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'

function Find-CompatiblePython {
    param([string]$Executable, [string[]]$LauncherArguments = @())
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $detected = & $Executable @LauncherArguments -c 'import sys; assert (3,10) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $detected) {
            return [string]($detected | Select-Object -Last 1)
        }
    } catch {
        # Ignore missing interpreters and Windows Store aliases.
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $null
}

# Ask for supported versions explicitly: py -3 may select Python 3.14+.
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($version in @('3.12', '3.11', '3.10', '3.13')) {
        $python = Find-CompatiblePython -Executable 'py' -LauncherArguments @('-' + $version)
        if ($python) { break }
    }
}
if (-not $python) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) {
        $python = Find-CompatiblePython -Executable $bundled
    }
}
if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $python = Find-CompatiblePython -Executable 'python'
}
if (-not $python) {
    Write-Host 'No compatible 64-bit Python found. Install Python 3.12 x64 from https://www.python.org/downloads/windows/ and run setup.cmd again.'
    exit 1
}
Write-Host ('Using Python: ' + $python)
& $python --version
if ($CheckOnly) { exit 0 }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $python -m venv --without-pip .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m pip --python .venv install pip -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Ready. Double-click web.cmd for the browser UI, or use run.cmd for the CLI.'
