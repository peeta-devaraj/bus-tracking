<#
    Put simulated buses on the deployed (Azure) map for a live demo.

    Reads the API address and admin key straight from the Function App, so
    there is nothing to copy and paste. Each route's buses run in their own
    window; close a window (or press Ctrl+C in it) to stop those buses.

    Usage:
        .\infra\demo.ps1                     # 3 buses on the town route, 2 to Kanyakumari
        .\infra\demo.ps1 -Routes NGL-SUC     # just one route
#>

param(
    [string]  $ResourceGroup = "bustrack-rg",
    [string]  $FunctionApp   = "bustrack-api-b7016d",
    [string[]]$Routes        = @("NGL-VAD-KKD", "NGL-KK"),
    [int]     $Buses         = 3
)

$ErrorActionPreference = "Stop"
$root   = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"

$state = az functionapp show -g $ResourceGroup -n $FunctionApp --query state -o tsv
if ($state -ne "Running") { throw "Function App is '$state'. Start it: az functionapp start -g $ResourceGroup -n $FunctionApp" }

$api = "https://$FunctionApp.azurewebsites.net/api"
$key = az functionapp config appsettings list -g $ResourceGroup -n $FunctionApp `
    --query "[?name=='ADMIN_KEY'].value" -o tsv

foreach ($route in $Routes) {
    $n = if ($route -eq "NGL-KK") { [Math]::Min($Buses, 2) } else { $Buses }
    $cmd = "`$env:BUSTRACK_API='$api'; `$env:ADMIN_KEY='$key'; " +
           "& '$python' '$root\tools\simulator.py' --route $route --buses $n"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "Started $n simulated bus(es) on $route" -ForegroundColor Green
}

Write-Host ""
Write-Host "Rider map: https://zealous-meadow-0a54d1100.3.azurestaticapps.net/" -ForegroundColor Cyan
Write-Host "Admin:     https://zealous-meadow-0a54d1100.3.azurestaticapps.net/admin.html?key=$key" -ForegroundColor Cyan
