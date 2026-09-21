param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$environments = @(
    @{ Name = "DEV"; EnvFile = ".env.dev"; Port = "8085" },
    @{ Name = "TEST"; EnvFile = ".env.test"; Port = "8086" },
    @{ Name = "UAT"; EnvFile = ".env.uat"; Port = "8087" },
    @{ Name = "PROD"; EnvFile = ".env.prod"; Port = "8088" }
)

function Start-OicEnvironment {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [string]$EnvFile,
        [Parameter(Mandatory = $true)] [string]$Port,
        [Parameter(Mandatory = $true)] [string]$RootPath
    )

    $command = @(
        "Set-Location `"$RootPath`";",
        "`$env:OIC_ENV_FILE=`"$EnvFile`";",
        "`$env:PORT=`"$Port`";",
        ".\scripts\run-local.ps1"
    ) -join " "

    Write-Host "Starting $Name on port $Port using $EnvFile"
    Start-Process powershell -ArgumentList @('-NoExit', '-Command', $command)
}

foreach ($environment in $environments) {
    Start-OicEnvironment -Name $environment.Name -EnvFile $environment.EnvFile -Port $environment.Port -RootPath $ProjectRoot
}

Write-Host "`nAll environments started."
foreach ($environment in $environments) {
    Write-Host "$($environment.Name): ws://127.0.0.1:$($environment.Port)/ws"
}
