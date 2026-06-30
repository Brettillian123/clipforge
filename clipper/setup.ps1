# ClipForge setup - run once on the machine you'll clip on (an NVIDIA GPU is fastest).
# Creates a LOCAL venv + work dirs, installs deps, copies the Poppins fonts for libass,
# and pre-generates the watermark. The local home defaults to %USERPROFILE%\clipforge;
# override with the CLIPFORGE_HOME environment variable.
$ErrorActionPreference = "Stop"
$root = if ($env:CLIPFORGE_HOME) { $env:CLIPFORGE_HOME } else { Join-Path $env:USERPROFILE "clipforge" }
$code = $PSScriptRoot

Write-Host "ClipForge setup -> $root"
New-Item -ItemType Directory -Force -Path $root, "$root\work", "$root\fonts", "$root\models" | Out-Null

# 1) venv (Python 3.14 preferred; fall back to whatever `py` gives)
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    try { py -3.14 -m venv "$root\.venv" } catch { py -3 -m venv "$root\.venv" }
}
$py = "$root\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r "$code\requirements.txt"

# 2) GPU wheels only when an NVIDIA GPU is present (Desktop is AMD -> CPU)
$nv = $false
try { nvidia-smi *> $null; if ($LASTEXITCODE -eq 0) { $nv = $true } } catch {}
if ($nv) {
    Write-Host "NVIDIA GPU detected: installing CUDA wheels (cuBLAS + cuDNN)."
    & $py -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
} else {
    Write-Host "No NVIDIA GPU: transcription will run on CPU (hours for a long VOD)."
    Write-Host "  -> Do the heavy transcription on the NVIDIA laptop; clips/code sync via OneDrive."
}

# 3) Poppins fonts for libass (captions) - copy from the per-user font store
$fontSrc = Join-Path $env:LOCALAPPDATA "Microsoft\Windows\Fonts"
Get-ChildItem -Path $fontSrc, "C:\Windows\Fonts" -Filter "Poppins*.ttf" -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-Item $_.FullName "$root\fonts\" -Force -ErrorAction SilentlyContinue }
$nfonts = (Get-ChildItem "$root\fonts\Poppins*.ttf" -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Host "Poppins fonts available for libass: $nfonts"
if ($nfonts -eq 0) { Write-Host "  WARNING: no Poppins fonts found; captions will use a fallback font." }

# 4) pre-generate the watermark
$env:PYTHONPATH = $code
& $py -c "from pipeline import config, branding; print('watermark:', branding.render_watermark(config.load_config(), force=True))"

# 5) download the Twemoji emoji set (openly licensed) used by the editor + renderer
& $py "$code\tools\fetch_emoji.py"

Write-Host "`nDone. Launch the Studio with:"
Write-Host "  `$env:PYTHONPATH='$code'; & '$py' '$code\dashboard.py'"
Write-Host "Or run a one-off clip job from the CLI:"
Write-Host "  `$env:PYTHONPATH='$code'; & '$py' '$code\clip.py' '<path\to\your-vod.mp4>'"
