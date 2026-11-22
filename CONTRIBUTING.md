# Contributing to PersonalOS

## Development setup

Use Python 3.11 or later and a local Redis instance. From the repository root, create a virtual environment, activate it, and install the project with its test dependencies.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Configure local services using the [configuration guide](docs/CONFIGURATION.md). Keep credentials and runtime data in local, ignored files.

```bash
python -m personal_os_agent --no-mcp
```

Dashboard-only mode uses Redis for tasks and reminders. Use `--skip-poll` to test the Planner with synthetic messages while Gmail polling is paused.

## Code layout

- `personal_os_agent/agents/` contains the Gmail Observer and deadline Planner.
- `personal_os_agent/api/` contains FastAPI routes, dashboard assets, and WebSocket updates.
- `personal_os_agent/core/` contains bootstrap, deadline rules, and reminder scheduling.
- `personal_os_agent/mcp_server/` contains read-only Gmail tools and OAuth credentials handling.
- `personal_os_agent/memory/` contains Redis and ChromaDB persistence.
- `personal_os_agent/config/` contains application settings. `personal_os_agent/utils/` contains logging and the Twilio adapter.

In async code, move blocking provider calls to a thread. Route operational settings through `Settings`. Log event names, identifiers, and error types. Keep message bodies, model requests, tokens, and credentials out of logs.

## Tests

Choose a Redis database reserved for tests. In PowerShell, run the same unit suite used by CI.

```powershell
$env:PERSONALOS_TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
python -m pytest -m unit
```

Tests that contact a running app carry the `integration` marker and require a separate running instance. Use synthetic messages for deadline extraction checks and mocks for Twilio calls.

Update the [architecture guide](docs/ARCHITECTURE.md) when changing interfaces or data flow, and document new settings in the [configuration guide](docs/CONFIGURATION.md).
