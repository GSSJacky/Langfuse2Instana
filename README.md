# Langfuse2Instana

A production-ready service that polls [Langfuse](https://langfuse.com) for LLM observability data and forwards it to [IBM Instana](https://www.ibm.com/products/instana) (or any OTLP-compatible backend) as **traces** and **metrics**.

Designed and validated with **IBM watsonx Orchestrate (WXO) ADK Developer Edition** on **Windows Server 2022**.

---

## Architecture

```
[ WXO ADK / LLM Application ]
         │  emits traces
         ▼
[ Langfuse (SaaS or on-prem) ]
         │  REST API (poll every N seconds)
         ▼
[ Langfuse2Instana (this service) ]
  Fetcher → Converter → OTLP Exporter
         │
         ├─ POST /v1/traces  ──► Instana OTLP acceptor
         └─ POST /v1/metrics ──►  (traces + derived metrics)
```

---

## Features

- **Multi-source** — monitor multiple Langfuse instances (SaaS + on-prem) simultaneously
- **Polling + Webhook** — periodic polling with optional webhook receiver for near-real-time export
- **Instana-aware SpanKind** — root spans are promoted to `SERVER` so Instana correctly identifies entry calls and builds service dashboards
- **GenAI semantic conventions** — maps Langfuse GENERATION observations to OTel `gen_ai.*` attributes (model, tokens, cost)
- **Metrics export** — token usage, cost, and latency metrics via OTLP metrics protocol
- **Deduplication** — SQLite-based state tracking to avoid re-exporting traces across restarts
- **Windows Service ready** — graceful shutdown on `SIGBREAK`/`SIGINT`/`SIGTERM`; works with NSSM
- **Containerized** — Docker and docker-compose ready

---

## Quick Start

### 1. Install

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\Activate.ps1
pip install .
```

### 2. Configure

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your Langfuse and Instana credentials
```

### 3. Run

```bash
langfuse2instana -c config.yaml
```

### Docker

```bash
cp config.yaml.example config.yaml
# Edit config.yaml
docker-compose up -d
```

---

## Deploying on Windows Server 2022 (WXO ADK host)

This walks through installing the service directly on a **Windows Server 2022** host running the **watsonx Orchestrate ADK** server.

```
[ Windows Server 2022 host ]
   WXO ADK server  ──emits──►  Langfuse (localhost:3010)
                                    ▲
   Langfuse2Instana ──polls───────┘   ──OTLP/HTTPS──►  Instana
   (Windows Service via NSSM)
```

### Prerequisites

- Windows Server 2022, administrator access (PowerShell)
- **Python 3.10+** — <https://www.python.org/downloads/windows/> (tick **"Add python.exe to PATH"**)
- Langfuse **public key** + **secret key** (from Langfuse → Settings → API Keys)
- Instana **OTLP endpoint** + **agent/ingestion key**
- Outbound HTTPS to Langfuse and Instana OTLP endpoint (no inbound ports needed for polling-only mode)
- [NSSM](https://nssm.cc/download) (optional, for Windows Service)

### Step 1 — Copy the project

Place the project folder at `C:\langfuse2instana`. The directory must contain `pyproject.toml` and `src\`.

### Step 2 — Create venv and install

```powershell
cd C:\langfuse2instana
python -m venv venv
.\venv\Scripts\Activate.ps1     # or: venv\Scripts\activate.bat in cmd.exe
pip install .
```

> ⚠️ Always run `python -m venv venv` **in the project directory** before `pip install .`.
> Copying a venv from another location will embed the old Python path in the `.exe` launcher and cause:
> `Fatal error in launcher: Unable to create process using '...\python.exe'`

> If `Activate.ps1` is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### Step 3 — Configure

Write `config.yaml` as UTF-8. Use PowerShell `[System.IO.File]::WriteAllText(...)` rather than `notepad` to avoid Windows cp1252 encoding issues:

```powershell
$config = @"
sources:
  - name: "wxo-adk"
    langfuse_host: "http://localhost:3010"
    public_key: "${LANGFUSE_PUBLIC_KEY}"
    secret_key: "${LANGFUSE_SECRET_KEY}"
    poll_interval_seconds: 30
    lookback_minutes: 60
    service_name: "wxo-server"
    environment: "production"
    max_traces_per_poll: 1000
    include_io: false

export:
  endpoint: "https://otlp-<color>-saas.instana.io"
  protocol: "http/protobuf"
  headers:
    x-instana-key: "${INSTANA_KEY}"
  insecure: false
  timeout_seconds: 30
  max_retries: 3

state:
  db_path: "C:\\langfuse2instana\\state.db"
  retention_days: 7

metrics:
  enabled: true

log_level: "INFO"
"@
[System.IO.File]::WriteAllText("C:\langfuse2instana\config.yaml", $config, [System.Text.Encoding]::UTF8)
```

### Step 4 — Run interactively (test)

```powershell
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-..."
$env:LANGFUSE_SECRET_KEY = "sk-lf-..."
$env:INSTANA_KEY         = "..."

.\venv\Scripts\langfuse2instana.exe -c C:\langfuse2instana\config.yaml
```

### Step 5 — Run as a Windows Service (NSSM)

```powershell
# Set secrets at Machine scope so the service can read them
[Environment]::SetEnvironmentVariable("LANGFUSE_PUBLIC_KEY", "pk-lf-...", "Machine")
[Environment]::SetEnvironmentVariable("LANGFUSE_SECRET_KEY", "sk-lf-...", "Machine")
[Environment]::SetEnvironmentVariable("INSTANA_KEY", "...", "Machine")

# Register and start
New-Item -ItemType Directory -Force -Path C:\langfuse2instana\logs | Out-Null
nssm install langfuse2instana "C:\langfuse2instana\venv\Scripts\langfuse2instana.exe" "-c C:\langfuse2instana\config.yaml"
nssm set langfuse2instana AppDirectory C:\langfuse2instana
nssm set langfuse2instana Start SERVICE_AUTO_START
nssm set langfuse2instana AppStopMethodConsole 1500
nssm set langfuse2instana AppStdout C:\langfuse2instana\logs\out.log
nssm set langfuse2instana AppStderr C:\langfuse2instana\logs\err.log
nssm set langfuse2instana AppRotateFiles 1
nssm start langfuse2instana
```

---

## Configuration Reference

See [`config.yaml.example`](config.yaml.example) for all options.

| Section | Key | Description |
|---------|-----|-------------|
| `sources[].langfuse_host` | Langfuse API URL | `http://localhost:3010` for local ADK |
| `sources[].public_key` | Langfuse public key | Basic auth username |
| `sources[].secret_key` | Langfuse secret key | Basic auth password |
| `sources[].service_name` | OTel `service.name` | Appears in Instana as the service name |
| `sources[].poll_interval_seconds` | Poll frequency | Default: 60 |
| `sources[].lookback_minutes` | Lookback window | Default: 5. Set to 60+ on first run to catch history |
| `export.endpoint` | OTLP endpoint | e.g. `https://otlp-blue-saas.instana.io` |
| `export.headers.x-instana-key` | Instana agent/ingestion key | For authentication |
| `state.db_path` | SQLite state file | Use absolute path on Windows |

Environment variables can be referenced in config with `${VAR_NAME}` syntax.

---

## Viewing Data in Instana

### Traces
- Go to **Analytics → Traces**
- Filter: `Destination service.name = <your service_name>`
- ⚠️ Check **"Show internal calls"** to see child spans (`LangGraph`, `agent`, `answer` etc.)
  — these are `INTERNAL` spans (correct OTel semantics for in-process work). Only the root span is `SERVER`.

### Services
- Go to **Services** and search for your `service_name`
- The service appears after at least one trace is exported with a `SERVER` root span

### Custom Metrics (token usage, cost, latency)
- Go to **Custom Dashboards → Add Widget**
- Search for: `langfuse.llm.token.usage`, `langfuse.llm.cost`, `langfuse.trace.duration`, `langfuse.generation.count`
- These OTLP metrics do **not** appear in the Service summary screen — they require a Custom Dashboard

---

## WXO ADK Integration Notes

When running **watsonx Orchestrate ADK Developer Edition** with `--with-langfuse`:

```
orchestrate server start -l --env-file c:\wxolite\.env --with-langfuse
```

- WXO routes traces to the **local Langfuse** instance (`localhost:3010`), **not** the cloud WXO trace store
- The cloud WXO trace store (`/instances/<id>/v1/traces`) will be **empty** — this is by design
- Use `langfuse2instana` (this project) to forward from Langfuse → Instana
- Use `wxo-otel-forwarder` only when WXO writes to the cloud trace store (i.e., **without** `--with-langfuse`)

### WXO ADK auth for cloud trace store (MCSP)

If you need to access the cloud WXO trace store directly, use `WXO_AUTH_TYPE=mcsp` with the `WO_API_KEY` from your ADK env:

```env
WXO_AUTH_TYPE=mcsp
WXO_BASE_URL=https://api.dl.watson-orchestrate.ibm.com
WXO_INSTANCE_ID=<your-instance-id>   # from WO_INSTANCE after /instances/
WXO_API_KEY=<your-WO_API_KEY>
WXO_MCSP_URL=https://iam.platform.saas.ibm.com
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `UnicodeDecodeError: 'charmap'` on startup | `config.yaml` saved with Windows cp1252 encoding | Re-write with `[System.IO.File]::WriteAllText(..., [System.Text.Encoding]::UTF8)` |
| `Fatal error in launcher: Unable to create process` | `.exe` points to old venv path | Delete `venv\`, re-run `python -m venv venv && pip install .` in the project directory |
| `yaml.scanner.ScannerError: mapping values not allowed` | YAML corrupted by PowerShell `-replace` | Re-write `config.yaml` with the PowerShell heredoc approach above |
| `All N traces already exported` after state.db delete | Multiple `state.db` files in different locations | `Get-ChildItem -Path C:\ -Recurse -Filter state.db` and delete all |
| Traces exported (200 OK) but not visible in Instana UI | Looking at wrong view | Use **Analytics → Traces** (not Application/Calls view); check **"Show internal calls"** |
| Service visible but metrics empty in Service screen | OTLP metrics go to Custom Dashboards, not Service screen | Create a Custom Dashboard with `langfuse.*` metrics |
| `WXO_AUTH_TYPE must be one of ['bearer','cpd','ibm_iam']` | Old version of `wxo-otel-forwarder` installed | Copy updated `config.py` and `auth.py` from source; run `pip install -e .` |
| Traces show as `INTERNAL` only, service not created in Instana | Root span `SpanKind` was `INTERNAL` | Fixed in this version: single natural root span is promoted to `SERVER` automatically |
| Langfuse traces only appear in Observations tab, not Traces tab | WXO appends to existing session trace | Start a **new chat session** (new browser tab / refresh) to generate a new trace ID |

---

## SpanKind Mapping (Instana compatibility)

| Observation type | SpanKind | Instana interpretation |
|-----------------|----------|----------------------|
| Root observation (single natural root) | `SERVER` | Entry call → service dashboard populated |
| Root when multiple top-level obs exist | Synthetic `SERVER` root + children as `INTERNAL` | Entry call via synthetic root |
| `GENERATION` | `CLIENT` | Exit/remote call to LLM provider |
| All other `SPAN` / `EVENT` | `INTERNAL` | In-process work (visible with "Show internal calls") |

---

## Data Mapping

### Langfuse → OpenTelemetry Spans

| Langfuse | OTel Span |
|----------|-----------|
| Trace | Trace with root span |
| Observation (SPAN) | Span (INTERNAL) |
| Observation (GENERATION) | Span (CLIENT) + `gen_ai.*` attributes |
| Observation (EVENT) | Span (point-in-time) |
| `observation.parentObservationId` | `parentSpanId` |
| `trace.userId` | `enduser.id` |
| `trace.sessionId` | `session.id` |
| `observation.model` | `gen_ai.request.model` |
| `observation.usage.input` | `gen_ai.usage.input_tokens` |
| `observation.usage.output` | `gen_ai.usage.output_tokens` |
| `observation.calculatedTotalCost` | `gen_ai.usage.cost` |

### Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `langfuse.llm.token.usage` | Counter | Token usage by model and type (input/output) |
| `langfuse.llm.cost` | Counter | Cost by model (USD) |
| `langfuse.generation.count` | Counter | Number of LLM calls by model |
| `langfuse.trace.duration` | Gauge | End-to-end trace duration (ms) |

---

## Webhook Mode (optional, near-real-time)

```yaml
webhook:
  enabled: true
  host: "0.0.0.0"
  port: 8000
```

Open inbound port on Windows:
```powershell
New-NetFirewallRule -DisplayName "langfuse2instana webhook" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

Configure in Langfuse → Settings → Webhooks → URL: `http://<host>:8000/webhook/langfuse`

| Endpoint | Description |
|----------|-------------|
| `POST /webhook/langfuse` | Generic webhook (auto-resolves source by project_id) |
| `POST /webhook/langfuse/{source_name}` | Source-specific webhook |
| `POST /api/trigger` | Manual trigger: `{"trace_id": "...", "source": "..."}` |
| `GET /health` | Health check |

---

## License

Apache License 2.0
