<#
    Starts the whole stack locally: storage emulator, API, and the web pages.

    Azure Functions Core Tools picks its Python worker from the *activated*
    virtual environment, which it detects through the VIRTUAL_ENV variable.
    Setting that here is what stops it falling back to whichever Python
    happens to be first on the system PATH.

    Usage:
        .\run-local.ps1              # start everything
        .\run-local.ps1 -Stop        # stop everything
#>

param(
    [switch]$Stop,
    [int]$ApiPort = 7071,
    [int]$WebPort = 5500
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$logs = Join-Path $root ".azurite"

function Stop-Stack {
    Get-Process func, azurite, node -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.Path -like "*func*" -or $_.CommandLine -like "*azurite*") } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Get-Process func -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*azurite*" -or $_.CommandLine -like "*serve-web*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "Stopped." -ForegroundColor Yellow
}

if ($Stop) { Stop-Stack; return }

if (-not (Test-Path $venv)) {
    Write-Host "No .venv found. Creating one..." -ForegroundColor Cyan
    & py -3.14 -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install --upgrade pip
    & "$venv\Scripts\python.exe" -m pip install -r (Join-Path $root "requirements-dev.txt")
}

New-Item -ItemType Directory -Force -Path $logs | Out-Null

# npm installs three shims per tool: azurite.ps1, azurite.cmd and a bare
# "azurite" for Git Bash. Plain Get-Command returns the .ps1 first, which
# Start-Process cannot launch ("%1 is not a valid Win32 application"). Ask for
# a real executable instead, preferring the .cmd/.exe forms Windows can start.
function Resolve-Launchable([string]$name) {
    $apps = @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)
    $preferred = $apps | Where-Object { $_.Source -match '\.(exe|cmd|bat)$' } | Select-Object -First 1
    if ($preferred) { return $preferred.Source }
    return $null
}

# --- Storage emulator ---------------------------------------------------
$azuriteUp = $false
try {
    $c = New-Object Net.Sockets.TcpClient
    $c.Connect("127.0.0.1", 10002); $c.Close(); $azuriteUp = $true
} catch { }

if ($azuriteUp) {
    Write-Host "Azurite already running on 10002" -ForegroundColor DarkGray
} else {
    Write-Host "Starting Azurite..." -ForegroundColor Cyan
    $azurite = Resolve-Launchable "azurite"
    if (-not $azurite) { throw "Azurite is not installed. Run: npm install -g azurite" }
    Start-Process -FilePath $azurite `
        -ArgumentList "--silent","--location","$logs","--tableHost","127.0.0.1" `
        -RedirectStandardOutput "$logs\azurite.out.log" `
        -RedirectStandardError  "$logs\azurite.err.log" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

# --- API ----------------------------------------------------------------
Get-Process func -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

# This pair is the whole trick: Core Tools reads VIRTUAL_ENV to decide which
# interpreter (and therefore which worker) to run.
$env:VIRTUAL_ENV = $venv
$env:PATH = "$venv\Scripts;$env:PATH"

Write-Host "Starting Functions host on :$ApiPort..." -ForegroundColor Cyan
$func = Resolve-Launchable "func"
if (-not $func) { throw "Azure Functions Core Tools not found. Install v4: npm install -g azure-functions-core-tools@4" }
Start-Process -FilePath $func `
    -ArgumentList "start","--port","$ApiPort" `
    -WorkingDirectory (Join-Path $root "api") `
    -RedirectStandardOutput "$logs\func.out.log" `
    -RedirectStandardError  "$logs\func.err.log" `
    -WindowStyle Hidden

$apiUp = $false
foreach ($i in 1..45) {
    try {
        Invoke-WebRequest "http://127.0.0.1:$ApiPort/api/health" -TimeoutSec 2 -UseBasicParsing | Out-Null
        $apiUp = $true; break
    } catch { Start-Sleep -Seconds 1 }
}

if (-not $apiUp) {
    Write-Host "API did not come up. Last log lines:" -ForegroundColor Red
    Get-Content "$logs\func.out.log" -Tail 20
    return
}
Write-Host "  API ready:  http://localhost:$ApiPort/api/health" -ForegroundColor Green

# --- Web ----------------------------------------------------------------
Write-Host "Starting web server on :$WebPort..." -ForegroundColor Cyan
Start-Process -FilePath "$venv\Scripts\python.exe" `
    -ArgumentList "-m","http.server","$WebPort","--bind","0.0.0.0" `
    -WorkingDirectory (Join-Path $root "web") `
    -RedirectStandardOutput "$logs\web.out.log" `
    -RedirectStandardError  "$logs\web.err.log" `
    -WindowStyle Hidden
Start-Sleep -Seconds 2

$lan = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -First 1 -ExpandProperty IPAddress)

Write-Host ""
Write-Host "  Rider map:  http://localhost:$WebPort/"        -ForegroundColor Green
Write-Host "  Driver:     http://localhost:$WebPort/driver.html" -ForegroundColor Green
Write-Host "  Admin:      http://localhost:$WebPort/admin.html"  -ForegroundColor Green
if ($lan) {
    Write-Host ""
    Write-Host "  From your phone on the same WiFi: http://${lan}:$WebPort/driver.html" -ForegroundColor Yellow
    Write-Host "  (Browser GPS needs HTTPS off-localhost, so use the simulator" -ForegroundColor DarkGray
    Write-Host "   locally and a real phone against the deployed Azure URL.)" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "Stop everything with: .\run-local.ps1 -Stop" -ForegroundColor DarkGray
