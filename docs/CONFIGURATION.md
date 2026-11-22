# Configure PersonalOS

Create `.env` in the repository root on the machine running the app. For a standard installation, start with the settings below and enter your OpenRouter key locally.

```dotenv
OPENROUTER_API_KEY=
REDIS_URL=redis://localhost:6379/0
```

The default model is `openrouter/free`. You can change the model and API endpoint with `OPENROUTER_MODEL` and `OPENROUTER_BASE_URL`. Add `HUGGINGFACE_TOKEN` locally if your embedding model requires authenticated downloads.

## Gmail

Enable the Gmail API in your Google Cloud project and create a **Desktop app** OAuth client. Save the downloaded client JSON as `secrets/credentials.json`. Open **Connections** in the dashboard and select **Connect Gmail**. PersonalOS saves the read-only grant in `secrets/token.json`.

Use `GOOGLE_CREDENTIALS_PATH` and `GOOGLE_TOKEN_PATH` to override those paths. The Observer checks for Gmail changes every 60 seconds by default. Set `OBSERVER_POLL_INTERVAL` to change the interval.

## Deadline notifications

| Setting | Default | Purpose |
|---|---|---|
| `DEADLINE_TIMEZONE` | `UTC` | IANA time zone used for quiet hours |
| `DEADLINE_QUIET_START` | `22:00` | Quiet hours start |
| `DEADLINE_QUIET_END` | `07:00` | Quiet hours end. Set both times equal to disable quiet hours. |
| `DEADLINE_REMINDER_INTERVAL` | `15` | Seconds between reminder checks |
| `DEADLINE_HOURLY_LIMIT` | `5` | Maximum in-app reminders per hour |
| `DEADLINE_PHONE_ENABLED` | `false` | Enables automatic critical deadline calls when Twilio is configured |
| `DEADLINE_PHONE_DAILY_LIMIT` | `2` | Maximum call attempts per day |

To enable Twilio, set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, and `TWILIO_TO_NUMBER` in your local `.env`, then set `DEADLINE_PHONE_ENABLED=true`. Restart PersonalOS to apply configuration changes.

## Local services

| Setting | Default |
|---|---|
| `DASHBOARD_HOST` and `DASHBOARD_PORT` | `127.0.0.1` and `8080` |
| `MCP_SERVER_HOST` and `MCP_SERVER_PORT` | `127.0.0.1` and `8000` |
| `CHROMA_PERSIST_PATH` | `./chroma_data` |
| `CHROMA_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` |

With Docker Compose, create the local `.env` and `secrets/` directory before running `docker compose up --build`. Compose supplies the local environment at runtime and mounts the credentials and runtime directories. The dashboard and Redis are published on localhost. The MCP server runs inside the app container.
