# Windows equivalent of scripts/run-local.sh
# Usage:
#   .\scripts\run-local.ps1
#   $env:PORT="8086"; $env:OIC_ENV_FILE=".env.prod"; .\scripts\run-local.ps1

$ErrorActionPreference = "Stop"
$ProjDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjDir

# HOST defaults to loopback only. Set $env:MCP_HOST="0.0.0.0" to expose it on
# the network (put TLS and access control in front of it if you do).
$BindHost = if ($env:MCP_HOST) { $env:MCP_HOST } else { "127.0.0.1" }
$BindPort = if ($env:PORT) { $env:PORT } else { "8085" }

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt

& ".\.venv\Scripts\python.exe" -m uvicorn mcp_server.main:app `
    --host $BindHost --port $BindPort --ws websockets
