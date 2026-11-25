# NVIDIA NIM

BitCadence governs agents that run on NVIDIA NIM. A NIM worker only sees jobs
the gateway releases to it, so any job created with `requires_approval` (or
sent to a gated role) waits for a human before a single GPU token is spent.
Every step lands in the job's append-only audit trail.

NIM serves models behind an OpenAI-compatible API. The same worker runs
against either:

| Where the model runs | `NIM_BASE_URL` | API key |
|---|---|---|
| NVIDIA's hosted API catalog | `https://integrate.api.nvidia.com/v1` (default) | required (`nvapi-...`) |
| A NIM container on your own GPUs | `http://<host>:8000/v1` | not needed |

## Run the worker

Register an agent identity for it (role `nim` here), then:

```powershell
$env:MCO_GATEWAY_URL = "http://127.0.0.1:18789"
$env:MCO_AGENT_TOKEN = "<token from mco register>"
$env:NIM_API_KEY     = "<nvapi key>"          # hosted catalog only
$env:NIM_MODEL       = "nvidia/nemotron-3.5-lightning-30b-a3b"
python -m mco.workers.nim
```

| Variable | Default |
|---|---|
| `NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `NIM_API_KEY` | falls back to `NVIDIA_API_KEY` |
| `NIM_MODEL` | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `NIM_MAX_TOKENS` / `NIM_TEMPERATURE` | `2048` / `0.2` |
| `AGENT_ROLE` / `AGENT_INSTANCE_ID` | `nim` / `nim-1` |
| `POLL_INTERVAL` | `15` seconds |

To gate everything the worker does, add its role to the gated roles, or
create its jobs with `"requires_approval": true`.

## What the worker guarantees

- **No work before approval.** It leases only what the gateway offers it.
- **Stops when it loses the job.** It streams the answer and checks it still
  holds the lease before the request and between chunks. A cancelled job or
  an expired lease stops generation, and the partial result is withheld.
- **Failures go to governance.** A NIM error (bad key, retired model, empty
  answer) fails the job with the HTTP status and NIM's message, so retries
  and escalation behave as for any worker. The API key never appears in errors.
- **Traceable handoff.** The completed job records the model and whether it
  ran on NVIDIA's hosted catalog or a self-hosted NIM.

## Model connection

Settings -> Model Connections has an **NVIDIA NIM (API catalog)** provider.
Testing it makes one free `GET /models` call. For a self-hosted NIM, add it
as **Custom (OpenAI-compatible)** with its `http://<host>:8000/v1` URL.

## Try it

`scripts/demo_nim_gated.py` runs the whole path on a private gateway with a
throwaway database: create a gated job, show the worker gets nothing, approve
it, let the worker run it on NIM, and print the audit trail.

```powershell
$env:NVIDIA_API_KEY = "<nvapi key>"
python scripts/demo_nim_gated.py --out nim-run.md
```

A recorded run is in [nvidia-nim-demo-run.md](nvidia-nim-demo-run.md).

## Tested

- Hosted API catalog with `nvidia/nemotron-3.5-lightning-30b-a3b`: live run
  recorded above.
- Self-hosted NIM container: covered by unit tests against the same API shape,
  not yet run against a real container.
