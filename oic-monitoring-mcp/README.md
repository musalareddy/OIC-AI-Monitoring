# OIC Monitoring MCP Server

A read-only [MCP](https://modelcontextprotocol.io) server for Oracle Integration Cloud (OIC). Point an MCP client (Claude Code, etc.) at it and ask questions about your integrations, connections, runtime instances, errors, and flow logs in plain language - the server translates those into OIC REST API calls and returns clean, LLM-friendly JSON.

Built with FastAPI + WebSocket, authenticates via OAuth2 Client Credentials (IDCS/IAM).

**Contents**

- [Requirements](#requirements)
- [Installation](#installation): [Windows](#windows), [macOS](#macos), [Linux](#linux)
- [Configuration](#configuration-env)
- [Connect an MCP client](#connect-an-mcp-client)
- [Running multiple environments from one codebase](#running-multiple-environments-from-one-codebase)
- [Keeping the server running](#keeping-the-server-running)
  - [Option A: session-only](#option-a-session-only-dies-when-you-close-the-terminal)
  - [Option B: permanent background service](#option-b-permanent-background-service-survives-reboot)
- [Tools](#tools)
- [Response format](#response-format)
- [How it works](#how-it-works)
- [Production hardening](#production-hardening)
- [Troubleshooting](#troubleshooting)

## Requirements

| Item | Requirement |
|---|---|
| Python | **3.10 or newer** (3.11+ recommended). The code uses the `str \| None` type syntax, which is a hard 3.10 floor. |
| OS | Windows 10/11, macOS 12+, or any modern Linux |
| Network | Outbound HTTPS to your OIC instance and to your IDCS/IAM token URL |
| OIC access | A confidential application (client ID + secret) with the `ServiceUser` role, see [Configuration](#configuration-env) |

Disk footprint is small: the virtual environment is roughly 120MB, and logs are capped at about 60MB total.

## Installation

The flow is the same on every platform:

1. Install Python 3.10+
2. Get the code
3. Create a virtual environment and install dependencies
4. Create and fill in your `.env`
5. Start the server and verify

Only step 1 and the virtual environment activation command differ per OS.

### Windows

**1. Install Python**

The easiest route is winget, in PowerShell:

```powershell
winget install -e --id Python.Python.3.12
```

Or download the installer from [python.org/downloads/windows](https://www.python.org/downloads/windows/). If you use the installer, tick **"Add python.exe to PATH"** on the first screen. That single checkbox is the cause of most "python is not recognized" problems later.

Close and reopen PowerShell, then confirm:

```powershell
py -3 --version
```

You should see `Python 3.10.x` or newer. The `py` launcher ships with the official installer and is the most reliable way to invoke Python on Windows, so the commands below use it.

**2. Get the code**

```powershell
git clone <your-repo-url> oic-mcp
cd oic-mcp
```

**3. Create a virtual environment and install dependencies**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks the activation script with a "running scripts is disabled" error, allow signed local scripts for your user once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Using `cmd.exe` instead of PowerShell? Activate with `.venv\Scripts\activate.bat`.

**4. Configure**

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in the values described in [Configuration](#configuration-env).

**5. Start the server**

```powershell
.\scripts\run-local.ps1
```

### macOS

**1. Install Python**

macOS ships with a system Python that you should not build against. Install your own with [Homebrew](https://brew.sh):

```bash
brew install python@3.12
```

Then confirm:

```bash
python3 --version
```

No Homebrew? Either install it first, or download the macOS installer from [python.org/downloads/macos](https://www.python.org/downloads/macos/).

**2. Get the code**

```bash
git clone <your-repo-url> oic-mcp
cd oic-mcp
```

**3. Create a virtual environment and install dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**4. Configure**

```bash
cp .env.example .env
nano .env
```

**5. Start the server**

```bash
chmod +x scripts/*.sh
./scripts/run-local.sh
```

### Linux

**1. Install Python**

Debian / Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

The `python3-venv` package is separate on Debian-family distros and is easy to miss. Without it, `python3 -m venv` fails with an `ensurepip is not available` error.

RHEL / Rocky / Alma / Fedora:

```bash
sudo dnf install -y python3.12 python3.12-devel git
```

Confirm the version:

```bash
python3 --version
```

If your distro is stuck below 3.10 (for example RHEL 8, which ships 3.6), install a newer interpreter alongside the system one (`python3.11` or `python3.12` from AppStream or deadsnakes) and use that explicit binary when creating the virtual environment, for example `python3.12 -m venv .venv`.

**2. Get the code**

```bash
git clone <your-repo-url> oic-mcp
cd oic-mcp
```

**3. Create a virtual environment and install dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**4. Configure**

```bash
cp .env.example .env
nano .env
```

**5. Start the server**

```bash
chmod +x scripts/*.sh
./scripts/run-local.sh
```

### Verify the install

The server listens on `ws://127.0.0.1:8085/ws` by default. In a **second** terminal:

```bash
python3 scripts/ws-call.py tools/list
```

On Windows:

```powershell
.\.venv\Scripts\python.exe scripts\ws-call.py tools/list
```

You should get a JSON list of roughly 40 tools. There is also a plain HTTP health check that needs no WebSocket client:

```bash
curl http://127.0.0.1:8085/healthz
# {"status": "ok"}
```

If you get a connection error or a 401, go to [Troubleshooting](#troubleshooting).

### Changing the host and port

`run-local.sh` and `run-local.ps1` both read a `PORT` variable, and bind to loopback only unless told otherwise:

```bash
# Linux / macOS
PORT=8086 ./scripts/run-local.sh
HOST=0.0.0.0 PORT=8086 ./scripts/run-local.sh
```

```powershell
# Windows
$env:PORT="8086"; .\scripts\run-local.ps1
$env:MCP_HOST="0.0.0.0"; $env:PORT="8086"; .\scripts\run-local.ps1
```

Or call uvicorn directly, which is what the scripts do under the hood:

```bash
uvicorn mcp_server.main:app --host 127.0.0.1 --port 8085 --ws websockets
```

Binding to `0.0.0.0` exposes an unauthenticated WebSocket to your network. Only do it behind TLS and a firewall, see [Production hardening](#production-hardening).

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Notes |
|---|---|---|
| `OIC_BASE_URL` | yes | e.g. `https://<instance>.integration.<region>.ocp.oraclecloud.com`, no trailing slash |
| `OIC_INSTANCE_NAME` | recommended | attached as `integrationInstance=` on every request; matches your OIC console URL |
| `OAUTH_TOKEN_URL` | yes | e.g. `https://<idcs-domain>.identity.oraclecloud.com/oauth2/v1/token` |
| `OAUTH_CLIENT_ID` | yes | confidential app client ID |
| `OAUTH_CLIENT_SECRET` | yes | confidential app client secret |
| `OAUTH_SCOPE` | sometimes | only needed if your app isn't pre-configured with the OIC resource/scope in IDCS - see [Troubleshooting](#troubleshooting) |
| `HTTP_TIMEOUT_SECS` | no | default `30` |
| `HTTP_MAX_RETRIES` | no | default `2` |
| `MCP_LOG_FILE` | no | default `mcp_server.log`; rotated automatically, see [Logging](#logging) |
| `OIC_ENV_FILE` | no | which env file this process loads, default `.env`, see [multiple environments](#running-multiple-environments-from-one-codebase) |

Your confidential app's client also needs the **`ServiceUser`** application role assigned against the OIC instance's resource app in IDCS/IAM (not on the client app itself) - otherwise every call 401s even with a valid token. See [Troubleshooting](#troubleshooting).

`.env` and every `.env.*` file are gitignored (only `.env.example` is tracked), so your secrets stay out of the repo.

## Connect an MCP client

**Claude Code:**

```bash
claude mcp add-json oic '{"type":"ws","url":"ws://127.0.0.1:8085/ws"}'
```

**Any other client that supports raw JSON config**, add this to its MCP servers config (e.g. `.mcp.json`, or copy `mcp.json.example`):

```json
{
  "mcpServers": {
    "oic": {
      "type": "ws",
      "url": "ws://127.0.0.1:8085/ws"
    }
  }
}
```

> This server only supports the WebSocket transport - it is not a stdio server, so `"type": "stdio"` or a spawned-command config will not work here. Start it as its own process first, then point your client at the URL.

Once connected, just ask your agent things like *"list the activated integrations"* or *"show me the last 20 runtime instances for INTEGRATION_CODE"* - no need to call tools by name yourself.

## Running multiple environments from one codebase

You do **not** need a second clone to monitor Dev, Test, and Prod. One checkout runs as many processes as you need, each pointed at its own env file by the `OIC_ENV_FILE` variable, and each on its own port.

`mcp_server/settings.py` reads `OIC_ENV_FILE` when the process starts and loads that file instead of `.env`. Everything else about the process is identical, same code, same tools.

**1. Create one env file per environment**

```bash
cp .env.example .env.dev
cp .env.example .env.test
cp .env.example .env.prod
```

Fill each one with that environment's own `OIC_BASE_URL`, `OIC_INSTANCE_NAME`, and OAuth credentials. Give each a distinct log file so their logs don't interleave:

```ini
# in .env.prod
MCP_LOG_FILE=mcp_server.prod.log
```

**2. Start one process per environment, each on its own port**

Linux / macOS:

```bash
OIC_ENV_FILE=.env.dev  PORT=8085 ./scripts/run-local.sh
OIC_ENV_FILE=.env.test PORT=8086 ./scripts/run-local.sh
OIC_ENV_FILE=.env.prod PORT=8087 ./scripts/run-local.sh
```

Windows PowerShell, one per terminal since each sets its own variables:

```powershell
$env:OIC_ENV_FILE=".env.prod"; $env:PORT="8087"; .\scripts\run-local.ps1
```

Or calling uvicorn directly:

```bash
OIC_ENV_FILE=.env.prod uvicorn mcp_server.main:app --host 127.0.0.1 --port 8087 --ws websockets
```

**3. Register each one with your client under a distinct name**

```json
{
  "mcpServers": {
    "oic-dev":  { "type": "ws", "url": "ws://127.0.0.1:8085/ws" },
    "oic-test": { "type": "ws", "url": "ws://127.0.0.1:8086/ws" },
    "oic-prod": { "type": "ws", "url": "ws://127.0.0.1:8087/ws" }
  }
}
```

Your agent then sees three clearly named tool sets, so you can ask it to compare the same integration across environments inside one conversation.

A suggested layout:

| Environment | Env file | Port | Client name | Log file |
|---|---|---|---|---|
| Dev | `.env.dev` | 8085 | `oic-dev` | `mcp_server.dev.log` |
| Test | `.env.test` | 8086 | `oic-test` | `mcp_server.test.log` |
| Prod | `.env.prod` | 8087 | `oic-prod` | `mcp_server.prod.log` |

**Things worth knowing**

- `OIC_ENV_FILE` is read once at process startup. Changing it, or editing the env file itself, requires restarting that process.
- Real OS environment variables take precedence over anything in the env file. If you have `OIC_BASE_URL` exported in your shell profile, every process picks that up regardless of which env file it loaded. Keep those variables out of your shell profile.
- Each process needs its own port. Two processes on the same port fail with "address already in use".
- Under Docker, `--env-file` injects real environment variables, so `OIC_ENV_FILE` is unnecessary there. Just point `--env-file` at the right file.
- Every tool here is read-only, but least privilege still costs nothing: give each environment's OAuth app only the `ServiceUser` role.

## Keeping the server running

Two supported patterns. Pick one deliberately, because they behave very differently when you log out.

### Option A: session-only (dies when you close the terminal)

Best for development, ad-hoc investigation, and anything where you do not want a forgotten process holding credentials in memory overnight.

**Run it in the foreground, in its own terminal window:**

```bash
# Linux / macOS
./scripts/run-local.sh
```

```powershell
# Windows
.\scripts\run-local.ps1
```

That is the whole method. The process is a child of that terminal:

- `Ctrl+C` stops it immediately.
- Closing the terminal window, ending the SSH session, or logging out kills it.
- It never restarts on its own, and it does not come back after a reboot.

Logs stream to the terminal and to `mcp_server.log` at the same time, so this is also the easiest mode to debug in.

If you want your prompt back but still want the process to die with the session, background it as a shell job rather than daemonising it:

```bash
./scripts/run-local.sh > uvicorn.log 2>&1 &
echo "started as PID $!"

# later, from the same shell
kill %1
```

Do not wrap it in `nohup`, `setsid`, `disown`, `screen`, or `tmux` if session-scoped behaviour is what you want. All of those exist specifically to detach a process from your session and will keep it alive after you log out.

To confirm nothing is left behind after you close the session:

```bash
# Linux / macOS
pgrep -af "mcp_server.main"
```

```powershell
# Windows
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*mcp_server.main*" } |
  Select-Object ProcessId, CommandLine
```

### Option B: permanent background service (survives reboot)

Best for a shared server, or a workstation where the team expects the MCP endpoint to always be there. In every case below the service starts at boot and restarts automatically if it crashes.

Do not use `nohup ... &` for this. It survives logout but not a reboot, and nothing restarts it if the process dies. Use your OS's service manager.

#### Linux (systemd)

Create `/etc/systemd/system/oic-mcp.service`:

```ini
[Unit]
Description=OIC Monitoring MCP Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=oicmcp
Group=oicmcp
WorkingDirectory=/opt/oic-mcp
Environment=OIC_ENV_FILE=/opt/oic-mcp/.env.prod
ExecStart=/opt/oic-mcp/.venv/bin/uvicorn mcp_server.main:app --host 127.0.0.1 --port 8085 --ws websockets
Restart=always
RestartSec=5

# Basic hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo useradd --system --home /opt/oic-mcp --shell /usr/sbin/nologin oicmcp
sudo chown -R oicmcp:oicmcp /opt/oic-mcp
sudo chmod 600 /opt/oic-mcp/.env.prod

sudo systemctl daemon-reload
sudo systemctl enable --now oic-mcp
sudo systemctl status oic-mcp
```

`enable` is what makes it come back after a reboot. `Restart=always` is what makes it come back after a crash. You need both.

Logs go to the journal:

```bash
journalctl -u oic-mcp -f
```

For a **second environment**, copy the unit to `oic-mcp-test.service`, change the `Environment=OIC_ENV_FILE=` line and the `--port`, then `sudo systemctl enable --now oic-mcp-test`.

Prefer running it as your own user? Put the same unit at `~/.config/systemd/user/oic-mcp.service`, enable it with `systemctl --user enable --now oic-mcp`, and run `sudo loginctl enable-linger $USER` so it starts at boot rather than at your first login.

#### macOS (launchd)

Create `~/Library/LaunchAgents/com.oic.mcp.plist`, replacing `/Users/you/oic-mcp` with your actual path:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.oic.mcp</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/oic-mcp/.venv/bin/uvicorn</string>
    <string>mcp_server.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8085</string>
    <string>--ws</string><string>websockets</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/you/oic-mcp</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>OIC_ENV_FILE</key>
    <string>/Users/you/oic-mcp/.env.prod</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <key>StandardOutPath</key>
  <string>/Users/you/oic-mcp/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/oic-mcp/launchd.err.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.oic.mcp.plist
launchctl print gui/$(id -u)/com.oic.mcp | head -20
```

`RunAtLoad` starts it immediately and again at every login. `KeepAlive` restarts it if it exits.

To stop it, or to reload after editing the plist:

```bash
launchctl bootout gui/$(id -u)/com.oic.mcp
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.oic.mcp.plist
```

A LaunchAgent in `~/Library/LaunchAgents` starts when **you** log in. If the machine must serve the endpoint before anyone logs in, put the same plist in `/Library/LaunchDaemons/` instead (owned by `root:wheel`, mode `644`), add a `UserName` key so it does not run as root, and load it with `sudo launchctl bootstrap system /Library/LaunchDaemons/com.oic.mcp.plist`.

For a second environment, duplicate the plist with a new `Label` (`com.oic.mcp.test`), a different port, and a different `OIC_ENV_FILE`.

#### Windows (NSSM, recommended)

[NSSM](https://nssm.cc/) wraps any executable as a proper Windows service. Install it with `winget install nssm` or `choco install nssm`, then in an **Administrator** PowerShell:

```powershell
$proj = "D:\oic_mcp_git"

nssm install OicMcp "$proj\.venv\Scripts\uvicorn.exe" "mcp_server.main:app --host 127.0.0.1 --port 8085 --ws websockets"
nssm set OicMcp AppDirectory $proj
nssm set OicMcp AppEnvironmentExtra "OIC_ENV_FILE=$proj\.env.prod"
nssm set OicMcp Start SERVICE_AUTO_START
nssm set OicMcp AppStdout "$proj\service.out.log"
nssm set OicMcp AppStderr "$proj\service.err.log"
nssm set OicMcp AppExit Default Restart
nssm set OicMcp AppRestartDelay 5000

nssm start OicMcp
```

`SERVICE_AUTO_START` is what brings it back after a reboot, and `AppExit Default Restart` is what brings it back after a crash.

Manage it like any other service:

```powershell
Get-Service OicMcp
nssm restart OicMcp
nssm stop OicMcp
nssm remove OicMcp confirm
```

For a second environment, install another service under a different name (`OicMcpTest`) with its own port and `OIC_ENV_FILE`.

#### Windows (Task Scheduler, no extra tooling)

If you cannot install NSSM, Task Scheduler can start it at boot. First create `start-prod.bat` in the project folder, because a scheduled task cannot easily set a working directory inline:

```bat
@echo off
cd /d D:\oic_mcp_git
set OIC_ENV_FILE=D:\oic_mcp_git\.env.prod
".venv\Scripts\python.exe" -m uvicorn mcp_server.main:app --host 127.0.0.1 --port 8085 --ws websockets
```

Then register it, in an Administrator PowerShell:

```powershell
schtasks /Create /TN "OIC MCP Server" /TR "D:\oic_mcp_git\start-prod.bat" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
schtasks /Run /TN "OIC MCP Server"
schtasks /Query /TN "OIC MCP Server"
```

This starts at boot but does not restart on crash by default. Add that in Task Scheduler under the task's **Settings** tab: "If the task fails, restart every 1 minute", up to 3 times. NSSM handles this better, which is why it is the recommended option.

#### Docker (any platform)

The restart policy does the same job as a service manager, including across host reboots, as long as the Docker daemon itself starts at boot:

```bash
docker build -t oic-mcp:latest .

docker run -d \
  --name oic-mcp-prod \
  --restart unless-stopped \
  -p 8085:8080 \
  --env-file .env.prod \
  oic-mcp:latest
```

The container listens on 8080 internally, so map whichever host port you want. Run a second environment by changing the name, host port, and env file:

```bash
docker run -d --name oic-mcp-test --restart unless-stopped \
  -p 8086:8080 --env-file .env.test oic-mcp:latest
```

Check on it with `docker ps` and `docker logs -f oic-mcp-prod`.

### Which one should I use?

| | Session-only | Permanent service |
|---|---|---|
| Survives closing the terminal | no | yes |
| Survives logout | no | yes |
| Survives reboot | no | yes |
| Restarts after a crash | no | yes |
| Setup effort | none | a few minutes, once |
| Good for | development, one-off investigations | shared servers, always-on team use |

## Tools

All tools are discoverable via `tools/list` and are read-only. Many accept an optional `version`; when omitted, the latest version is resolved automatically.

**Integrations**
- `list_integrations` - optional `onlyActivated`, `limit`, `page`
- `list_activated_integrations`
- `get_integration` - by `identifier` and `version`
- `get_integration_auto` - design-time details by `code` or `code|version`, auto-resolves latest
- `search_integration_by_name` - full-catalogue search (auto-paginated), exact or partial match, always returns a list
- `list_integrations_search` - client-side paged search across `code`/`name`/`description`/`keywords`
- `export_integration` - download the integration zip as base64, or `listOnly` of entries + previews

**Runtime monitoring**
- `list_instances` - optional `integrationId`, `status`, `startTime`/`endTime`, `timewindow`, `limit`
- `get_instance` - full detail by `instanceId`
- `get_instance_activity_stream` - step-by-step flow/execution log for one instance
- `list_errors` - optional `integrationId`, `timewindow`, `limit`
- `list_metrics` - historical tracking metrics, hourly or daily
- `list_schedules` / `get_schedule` - schedule info per integration

**Connections, packages, and building blocks**
- `list_connections` / `get_connection` / `get_connection_detail`
- `list_packages` / `get_package`
- `list_lookups` / `get_lookup`
- `get_library`
- `list_adapters` / `get_adapter`
- `list_agents` / `list_agent_groups`
- `list_endpoints` - integration endpoints with role and connection

**Design-time analysis**
- `summarize_integration` - trigger/targets/tracking variables at a glance
- `summarize_integration_with_steps` - the above plus selected step I/O summaries
- `summarize_flow_controls` - count and sample Switch/ForEach/Route/Fault/Scope constructs
- `summarize_mappings` - extract mapping steps
- `deep_flow_outline` - compact textual outline of the whole flow
- `get_integration_step` - raw JSON subtree(s) matching a `stepName` (exact + fuzzy), plus matching endpoints
- `summarize_step_io` - suspected SQL/query snippets and parameters for a `stepName`, falls back to endpoint match if no step is found

**Utility**
- `fetch_raw_path` - fetch any relative OIC path
- `search_json` - substring search over any JSON-like structure

Design-time tools accept an optional `designJsonPath` to read a previously-downloaded design JSON from disk instead of calling OIC - useful for offline analysis or avoiding repeat calls while iterating.

## Response format

Every `tools/call` result follows the MCP spec envelope: `{"content": [{"type": "text", "text": "<json-or-plain-text>"}], "isError": false}`. The actual tool payload is JSON-serialized inside `text` - parse it once more to get structured data:

```bash
python3 scripts/ws-call.py tools/call '{"name":"list_integrations","arguments":{"limit":3}}' \
  | python3 -c "
import json, sys
resp = json.load(sys.stdin)
payload = json.loads(resp['result']['content'][0]['text'])
print(json.dumps(payload, indent=2))
"
```

Tool execution errors (e.g. OIC unreachable, bad identifier) come back the same way with `isError: true` - check that flag rather than assuming success. Genuine protocol errors (unknown method, unknown tool name) use a real JSON-RPC `error` object instead. Large payloads are capped at 100,000 characters and clearly marked `[TRUNCATED ...]` when cut - never silently.

## How it works

- The server exposes one WebSocket endpoint speaking JSON-RPC 2.0 / MCP. Clients call `tools/list` to discover tools and `tools/call` to run them.
- On each call it fetches from OIC's REST API via an authenticated `httpx.AsyncClient`. The OAuth token is cached and refreshed automatically on expiry.
- The WebSocket handshake negotiates the `mcp` subprotocol when a client offers it, and `initialize` returns a spec-compliant `protocolVersion` and object-typed `capabilities` - required for strict clients like Claude Code to accept the connection at all.
- Redirects are followed manually rather than via httpx's built-in handling: OIC's design-time gateway 307-redirects to a different host than `OIC_BASE_URL`, and httpx strips the `Authorization` header on any cross-host redirect by default. Manual handling preserves it for this known, trusted hop.

### Logging

Logs go to `mcp_server.log` (override with `MCP_LOG_FILE`), rotated automatically at 10MB per file with 5 backups (~60MB ceiling) - it will never grow unbounded. No logrotate, cron job, or sudo needed on any platform, the app manages its own log size on every write.

When running one process per environment, set a distinct `MCP_LOG_FILE` in each env file so the logs stay separable.

## Production hardening

- Run behind TLS (reverse proxy like Nginx/Traefik) and restrict network access. The WebSocket endpoint has no authentication of its own, so never expose it directly to an untrusted network.
- Keep the bind address on `127.0.0.1` unless you have a specific reason not to.
- Store secrets in a vault; never commit `.env`. On Linux, `chmod 600` the env file and own it as the service user.
- Grant the OAuth client the minimum role needed (`ServiceUser` is read-level; avoid `ServiceDeveloper` unless you specifically need create/import tools).
- Use a process manager (systemd, launchd, NSSM) so it survives reboots, see [Option B](#option-b-permanent-background-service-survives-reboot).
- Watch payload sizes on large catalogues - prefer `list_integrations_search` with narrow terms and paging over pulling entire lists.

## Troubleshooting

**Install and startup**

- **`python` or `py` is not recognized (Windows)** - Python was installed without "Add python.exe to PATH". Re-run the installer, choose Modify, and enable it, or reinstall via `winget install -e --id Python.Python.3.12`. Open a new terminal afterwards.
- **`running scripts is disabled on this system` (Windows)** - PowerShell's execution policy is blocking virtual environment activation. Run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, or use `cmd.exe` with `.venv\Scripts\activate.bat`.
- **`ensurepip is not available` (Debian/Ubuntu)** - install the separate venv package: `sudo apt install python3-venv`.
- **`TypeError: unsupported operand type(s) for |`** - you are on Python 3.9 or older. Install 3.10+ and recreate the virtual environment with the newer interpreter.
- **`ValidationError` on startup naming `OIC_BASE_URL` or `OAUTH_*`** - the env file was not found or is incomplete. Confirm you copied `.env.example` to `.env`, that you started the process from the project directory, and that `OIC_ENV_FILE` (if set) points at a file that exists.
- **`address already in use`** - another process holds the port. Find it with `lsof -i :8085` (Linux/macOS) or `netstat -ano | findstr :8085` (Windows), or just start on a different `PORT`.

**Authentication**

- **401/403 from the token URL** - check `OAUTH_CLIENT_ID`/`OAUTH_CLIENT_SECRET` and that `OAUTH_TOKEN_URL` is correct for your IDCS/IAM domain.
- **Token request succeeds (200) but every OIC call still 401s** - this is almost always a missing IDCS role, not a bad token. In OCI Console → Identity & Security → Domains → your domain → find the **OIC instance's own resource app** (not your confidential client app) → Application roles → `ServiceUser` → assign your confidential client app as an application. Get a fresh token after assigning it - an existing token won't retroactively gain the role.

**Connecting**

- **Connection refused / can't reach the WebSocket** - confirm the server process is actually running (`pgrep -af mcp_server.main`, `systemctl status oic-mcp`, or `Get-Service OicMcp`) and that nothing else is bound to the same port. `curl http://127.0.0.1:8085/healthz` is the quickest check.
- **Claude Code shows the server as "still connecting" or its tools never load** - the server must already be running *before* you start the client session; it isn't retried automatically if it wasn't up yet. Restart the client after confirming the server is healthy.
- **The wrong environment's data comes back** - a real OS environment variable is overriding your env file, since those take precedence. Check with `env | grep OIC_` (Linux/macOS) or `Get-ChildItem Env:OIC_*` (Windows) and clear anything stale from your shell profile.

**Using the tools**

- **404 on certain flow/design paths** - prefer the design-time tools (`get_integration_auto`, `summarize_*`) over raw path fetches; they handle version resolution and known endpoint quirks for you.
- **Large or slow responses** - narrow with `list_integrations_search`/`search_integration_by_name` and paging (`perPage`, `maxPages`) rather than pulling full catalogues.
- **Health check** - `GET /healthz` returns `{"status": "ok"}` when the process itself is up (does not verify OIC connectivity).

## License

MIT
