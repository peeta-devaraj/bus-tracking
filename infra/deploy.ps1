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
    [int]   $BudgetInr    = 1000,
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
    #
    # Scoped to the whole SUBSCRIPTION, not this resource group. A group-level
    # budget only watches this project, and the spend that actually threatens a
    # student credit is usually some other forgotten resource.
    #
    # Created through the REST API because `az consumption budget create`
    # rejects Azure for Students subscriptions with "Invalid budget
    # configuration". An earlier version of this step called that command with
    # 2>$null inside try/catch -- but a failing native command does not throw
    # in Windows PowerShell, so it printed "Budget created." while creating
    # nothing. Success is now judged by $LASTEXITCODE and read back.
    Step "Subscription budget alert at INR $BudgetInr"
    $subscriptionId = az account show --query id -o tsv
    $alertEmail     = az account show --query user.name -o tsv

    function BudgetNotification($threshold, $type) {
        @{
            enabled       = $true
            operator      = "GreaterThan"
            threshold     = $threshold
            thresholdType = $type
            contactEmails = @($alertEmail)
            contactRoles  = @("Owner")
        }
    }

    $budgetFile = Join-Path $env:TEMP "bustrack-budget.json"
    @{
        properties = @{
            category   = "Cost"
            amount     = $BudgetInr
            timeGrain  = "Monthly"
            timePeriod = @{
                startDate = (Get-Date -Format "yyyy-MM-01") + "T00:00:00Z"
                endDate   = (Get-Date).AddYears(1).ToString("yyyy-MM-01") + "T00:00:00Z"
            }
            notifications = @{
                actual_50_percent    = (BudgetNotification 50 "Actual")
                actual_90_percent    = (BudgetNotification 90 "Actual")
                forecast_100_percent = (BudgetNotification 100 "Forecasted")
            }
        }
    } | ConvertTo-Json -Depth 10 | Set-Content -Path $budgetFile -Encoding utf8

    # PUT is idempotent, so re-running the script updates rather than duplicates.
    $budgetUri = "https://management.azure.com/subscriptions/$subscriptionId" +
                 "/providers/Microsoft.Consumption/budgets/student-credit-guard?api-version=2023-05-01"
    az rest --method put --uri $budgetUri --body "@$budgetFile" --output none 2>$null
    $budgetOk = ($LASTEXITCODE -eq 0)
    Remove-Item $budgetFile -ErrorAction SilentlyContinue

    if ($budgetOk) {
        Note "Budget 'student-credit-guard' active; alerts go to $alertEmail."
    } else {
        # Not fatal, but never pretend a guardrail exists when it does not.
        Write-Host "    Could NOT create the budget. No spending alert is in place." -ForegroundColor Yellow
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

# Static Web Apps does NOT get "<name>.azurestaticapps.net". Azure generates a
# random hostname like "zealous-meadow-0a54d1100.3.azurestaticapps.net", so the
# real value has to be read back or CORS silently blocks every request from the
# actual site.
$webHost = az staticwebapp show --name $staticWebApp --resource-group $ResourceGroup `
    --query "defaultHostname" -o tsv
Note "Web hostname: $webHost"

$existingCors = az functionapp cors show --name $functionApp --resource-group $ResourceGroup `
    --query "allowedOrigins" -o tsv
foreach ($origin in @("https://$webHost", "http://localhost:5500")) {
    if ($existingCors -notcontains $origin) {
        az functionapp cors add --name $functionApp --resource-group $ResourceGroup `
            --allowed-origins $origin --output none 2>$null
    }
}

# --------------------------------------------------------------------------
Step "Deploying the API"
Push-Location (Join-Path $root "api")
try {
    func azure functionapp publish $functionApp --python
} finally {
    Pop-Location
}

# --------------------------------------------------------------------------
Step "Building the web pages against the deployed API"

# Build into a temp copy rather than editing web/js/config.js in place. An
# earlier version rewrote the file and restored it afterwards, which left the
# repo holding a deployed URL whenever the deploy was interrupted. The working
# tree should never depend on a deploy finishing cleanly.
$apiBase  = "https://$functionApp.azurewebsites.net/api"
$buildDir = Join-Path ([IO.Path]::GetTempPath()) "bustrack-build-$(Get-Random)"

Copy-Item (Join-Path $root "web") $buildDir -Recurse -Force
$configPath = Join-Path $buildDir "js\config.js"
(Get-Content $configPath -Raw) -replace 'apiBase: "[^"]*"', "apiBase: `"$apiBase`"" |
    Set-Content $configPath -Encoding utf8
Note "API base baked in: $apiBase"

Step "Deploying the web pages"
try {
    $token = az staticwebapp secrets list --name $staticWebApp --resource-group $ResourceGroup `
        --query "properties.apiKey" -o tsv
    npx --yes @azure/static-web-apps-cli deploy $buildDir `
        --deployment-token $token --env production
} finally {
    Remove-Item $buildDir -Recurse -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------
$webUrl = $webHost

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
