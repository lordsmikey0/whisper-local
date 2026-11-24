param([ValidateSet('auto', 'cuda', 'cpu')][string]$Device = 'auto')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYANNOTE_METRICS_ENABLED = '0'
$env:CUDA_VISIBLE_DEVICES = '0'
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
$useCuda = $Device -eq 'cuda'
if ($Device -eq 'auto' -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    & nvidia-smi --query-gpu=name --format=csv,noheader
    $useCuda = $LASTEXITCODE -eq 0
}
if ($useCuda) {
    & '.\.diarization-venv\Scripts\python.exe' -m pip install --upgrade --no-deps --index-url https://download.pytorch.org/whl/cu126 'torch==2.8.0+cu126' 'torchaudio==2.8.0+cu126'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} elseif ($Device -eq 'cpu') {
    & '.\.diarization-venv\Scripts\python.exe' -m pip install --upgrade --no-deps --index-url https://download.pytorch.org/whl/cpu 'torch==2.8.0+cpu' 'torchaudio==2.8.0+cpu'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
# Pin base versions; a matching CUDA local version also satisfies these pins.
& '.\.diarization-venv\Scripts\python.exe' -m pip install -r requirements-speakers.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.diarization-venv\Scripts\python.exe' -c 'import diarize_worker; from pyannote.audio import Pipeline; import torch; print(Pipeline); print("Torch:", torch.__version__, "CUDA available:", torch.cuda.is_available())'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Device -eq 'cuda') {
    & '.\.diarization-venv\Scripts\python.exe' -c 'import torch; assert torch.cuda.is_available(), "CUDA unavailable: check the NVIDIA driver"'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Set-Content -LiteralPath '.diarization-venv\.ready' -Value 'pyannote.audio 4.0.1 auto' -Encoding ascii
Write-Host 'Speaker libraries ready. Open speaker setup in the web UI to download the model.'
