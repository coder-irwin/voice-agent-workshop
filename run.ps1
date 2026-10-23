# Start the voice agent on Windows, then open http://localhost:8000
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Check if Ollama is running
$ollamaRunning = $false
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
    if ($resp.StatusCode -eq 200) { $ollamaRunning = $true }
} catch {}

if (-not $ollamaRunning) {
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollamaCmd) {
        Write-Host "Starting Ollama in the background..." -ForegroundColor Cyan
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 1
            try {
                $resp = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
                if ($resp.StatusCode -eq 200) { $ollamaRunning = $true; break }
            } catch {}
        }
    }
}

if (-not $ollamaRunning -and -not $env:GEMINI_API_KEY) {
    Write-Host "[!] Note: Ollama is not running on localhost:11434 and GEMINI_API_KEY is not set." -ForegroundColor Yellow
    Write-Host "    The server will start, but the brain requires Ollama (https://ollama.com) or a GEMINI_API_KEY to answer queries." -ForegroundColor Yellow
}

$PY = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
Write-Host "`n🎙️  Voice Agent running! Open http://localhost:8000 in Chrome, click 'Start Listening' and talk!`n" -ForegroundColor Green

& $PY -m uvicorn app.main:app --port 8000
