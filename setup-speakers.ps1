$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYANNOTE_METRICS_ENABLED = '0'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Run setup.cmd first.'
    exit 1
}
if (-not (Test-Path -LiteralPath '.diarization-venv\Scripts\python.exe')) {
    & $python -m venv --without-pip .diarization-venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m pip --python .diarization-venv install pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# Keep dependencies isolated. Inference explicitly uses CPU in diarize_worker.py.
& '.\.diarization-venv\Scripts\python.exe' -m pip install -r requirements-speakers.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.diarization-venv\Scripts\python.exe' -c 'import diarize_worker; from pyannote.audio import Pipeline; print(Pipeline)'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Set-Content -LiteralPath '.diarization-venv\.ready' -Value 'pyannote.audio 4.0.1 CPU' -Encoding ascii
Write-Host 'Speaker libraries ready. Open speaker setup in the web UI to download the model.'
