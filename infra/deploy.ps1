<#
    Provision and deploy the whole system to Azure.

    Everything it creates fits inside the Azure for Students allowance:

        Resource group        free
        Storage account       a few paise a month at demo volumes
        Function App          Linux Consumption; 1M free executions/month
        Static Web App        Free tier
        Budget alert          free, and set up FIRST

    An Azure for Students subscription has no card attached, so it stops when
    the credit runs out rather than billing you. The budget alert still goes in
    first, because "it stopped working during the demo" is a worse surprise
    than an email at 50%.

    Prerequisites:
        az login
        az account set --subscription "<your subscription>"

    Usage:
        .\infra\deploy.ps1                       # create everything and deploy
        .\infra\deploy.ps1 -CodeOnly             # redeploy code to existing resources
        .\infra\deploy.ps1 -Destroy              # delete the resource group
#>

param(
    [string]$Prefix       = "bustrack",
    [string]$Location     = "centralindia",
    [string]$ResourceGroup,
    [int]   $BudgetInr    = 500,
    [switch]$CodeOnly,
    [switch]$Destroy
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

if (-not $ResourceGroup) { $ResourceGroup = "$Prefix-rg" }

# Storage account names must be globally unique, lowercase, 3-24 chars.
$suffix         = (az account show --query id -o tsv).Substring(0, 6)
$storageAccount = ($Prefix + $suffix) -replace '[^a-z0-9]', ''
if ($storageAccount.Length -gt 24) { $storageAccount = $storageAccount.Substring(0, 24) }
$functionApp    = "$Prefix-api-$suffix"
$staticWebApp   = "$Prefix-web-$suffix"

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }
function Note($message) { Write-Host "    $message" -ForegroundColor DarkGray }

# --------------------------------------------------------------------------

if ($Destroy) {
    Write-Host "This deletes the resource group '$ResourceGroup' and everything in it." -ForegroundColor Yellow
    $answer = Read-Host "Type the resource group name to confirm"
    if ($answer -ne $ResourceGroup) { Write-Host "Cancelled."; return }
    az group delete --name $ResourceGroup --yes --no-wait
    Write-Host "Deletion started." -ForegroundColor Yellow
    return
}

Step "Checking the Azure CLI login"
$account = az account show --query "{name:name, id:id}" -o json 2>$null | ConvertFrom-Json
if (-not $account) { throw "Not logged in. Run: az login" }
Note "Subscription: $($account.name)"

if (-not $CodeOnly) {

    Step "Resource group: $ResourceGroup ($Location)"
    az group create --name $ResourceGroup --location $Location --output none

    # ---- Budget alert BEFORE anything that can cost money -----------------
    Step "Budget alert at INR $BudgetInr"
    $budgetJson = @{
        category      = "Cost"
        amount        = $BudgetInr
        timeGrain     = "Monthly"
        timePeriod    = @{ startDate = (Get-Date -Format "yyyy-MM-01") }
        notifications = @{
            fiftyPercent = @{
                enabled = $true; operator = "GreaterThan"; threshold = 50
                contactEmails = @((az account show --query user.name -o tsv))
            }
            ninetyPercent = @{
                enabled = $true; operator = "GreaterThan"; threshold = 90
                contactEmails = @((az account show --query user.name -o tsv))
            }
        }
    } | ConvertTo-Json -Depth 8 -Compress

    $budgetFile = Join-Path $env:TEMP "bustrack-budget.json"
    $budgetJson | Set-Content -Path $budgetFile -Encoding utf8
    try {
        az consumption budget create-with-rg `
            --resource-group $ResourceGroup --budget-name "$Prefix-budget" `
            --amount $BudgetInr --category Cost --time-grain Monthly `
            --start-date (Get-Date -Format "yyyy-MM-01") `
            --end-date (Get-Date).AddYears(1).ToString("yyyy-MM-01") `
            --output none 2>$null
        Note "Budget created."
    } catch {
        # Some student subscriptions restrict the Consumption API. Not fatal,
        # but say so clearly rather than pretending a guardrail exists.
        Write-Host "    Could not create the budget automatically." -ForegroundColor Yellow
        Write-Host "    Set one by hand: portal.azure.com > Cost Management > Budgets" -ForegroundColor Yellow
    }

    Step "Storage account: $storageAccount"
    az storage account create `
        --name $storageAccount --resource-group $ResourceGroup --location $Location `
        --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 `
        --allow-blob-public-access false --output none

    Step "Function App: $functionApp"
    az functionapp create `
        --name $functionApp --resource-group $ResourceGroup `
        --storage-account $storageAccount --consumption-plan-location $Location `
        --runtime python --runtime-version 3.11 --functions-version 4 `
        --os-type Linux --output none

    Step "Static Web App: $staticWebApp"
    # Static Web Apps has limited region availability; centralindia is not one
    # of them, so this one resource goes to the nearest supported region.
    az staticwebapp create `
        --name $staticWebApp --resource-group $ResourceGroup `
        --location "eastasia" --sku Free --output none
}

# --------------------------------------------------------------------------
Step "Configuring the API"

$adminKey = -join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
$storageConn = az storage account show-connection-string `
    --name $storageAccount --resource-group $ResourceGroup --query connectionString -o tsv

az functionapp config appsettings set `
    --name $functionApp --resource-group $ResourceGroup `
    --settings `
        "AzureWebJobsStorage=$storageConn" `
        "ADMIN_KEY=$adminKey" `
        "BUSTRACK_ALLOWED_ORIGIN=*" `
    --output none

Note "Admin key generated. It is printed at the end -- save it."

Step "Allowing the web app to call the API (CORS)"
az functionapp cors add --name $functionApp --resource-group $ResourceGroup `
    --allowed-origins "https://$staticWebApp.azurestaticapps.net" --output none 2>$null
az functionapp cors add --name $functionApp --resource-group $ResourceGroup `
    --allowed-origins "http://localhost:5500" --output none 2>$null

# --------------------------------------------------------------------------
Step "Deploying the API"
Push-Location (Join-Path $root "api")
try {
    func azure functionapp publish $functionApp --python
} finally {
    Pop-Location
}

# --------------------------------------------------------------------------
Step "Pointing the web pages at the deployed API"

$apiBase = "https://$functionApp.azurewebsites.net/api"
$configPath = Join-Path $root "web\js\config.js"
$original = Get-Content $configPath -Raw
$deployed = $original -replace 'apiBase: "[^"]*"', "apiBase: `"$apiBase`""
$deployed | Set-Content $configPath -Encoding utf8

Step "Deploying the web pages"
try {
    $token = az staticwebapp secrets list --name $staticWebApp --resource-group $ResourceGroup `
        --query "properties.apiKey" -o tsv
    npx --yes @azure/static-web-apps-cli deploy (Join-Path $root "web") `
        --deployment-token $token --env production
} finally {
    # Restore the local default so the repo keeps working against localhost.
    $original | Set-Content $configPath -Encoding utf8
}

# --------------------------------------------------------------------------
$webUrl = az staticwebapp show --name $staticWebApp --resource-group $ResourceGroup `
    --query "defaultHostname" -o tsv

Write-Host "`n----------------------------------------------------------" -ForegroundColor Green
Write-Host " Deployed" -ForegroundColor Green
Write-Host "----------------------------------------------------------" -ForegroundColor Green
Write-Host " Rider map   https://$webUrl/"
Write-Host " Driver      https://$webUrl/driver.html"
Write-Host " Admin       https://$webUrl/admin.html?key=$adminKey"
Write-Host " API health  $apiBase/health"
Write-Host ""
Write-Host " Admin key   $adminKey" -ForegroundColor Yellow
Write-Host " Save that now. It is the only thing protecting bus secrets." -ForegroundColor Yellow
Write-Host ""
Write-Host " Seed routes and run the simulator against Azure:" -ForegroundColor DarkGray
Write-Host "   `$env:BUSTRACK_API='$apiBase'; `$env:ADMIN_KEY='$adminKey'"
Write-Host "   python tools/seed_nagercoil.py"
Write-Host "   python tools/simulator.py --route NGL-VAD-KKD --buses 3"
Write-Host ""
Write-Host " Tear it all down with: .\infra\deploy.ps1 -Destroy" -ForegroundColor DarkGray
