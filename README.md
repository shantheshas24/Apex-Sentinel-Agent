# PersonalOS Deadline Tracker

PersonalOS turns authorized Gmail messages into deadline tasks. The dashboard shows source evidence, review status, due time, and priority. You can confirm or edit deadlines, snooze reminders, mark tasks complete, and export the list as CSV.

## Run locally

1. Install Python 3.11 or later and start Redis.
2. Install the dependencies and copy the example environment file.

   ```powershell
   python -m pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. Set `OPENROUTER_API_KEY` in `.env`. The default model uses the [OpenRouter free router](https://openrouter.ai/openrouter/free).
4. Enable the Gmail API for your Google Cloud project. Create a Desktop OAuth client and save the downloaded credentials as `secrets/credentials.json`.
5. Start the app.

   ```powershell
   python main.py
   ```

6. Open the dashboard and select **Connect Gmail** to grant read-only access. Select **Reconnect Gmail** if your saved connection has expired or used the earlier permissions. [Open the deadline dashboard](http://localhost:8080)

**Check Gmail** requests an immediate scan. On the first scan the Observer pages through unread mail and inbox mail received in the past day. Afterwards it resumes from a saved Gmail history checkpoint and recovers the elapsed inbox window if Google expires that checkpoint. The Planner uses the subject and up to 12,000 characters of inline email text. A local fallback can still recognize clearly worded cutoffs for review. Jobs that need the model remain in Redis and retry without blocking newer mail. With OpenRouter, requests ask the service to route only to providers that deny data collection. Run the dashboard on localhost for single-user use.

## Manage deadlines and reminders

- **Needs review** means the task has no confirmed due time. Open the card to review the evidence and choose a date and time in your local time zone. An explicit ISO timestamp with a UTC offset can be scheduled directly.
- **Upcoming** covers confirmed deadlines within seven days. Later tasks are **Pending**. Overdue and completed tasks have their own views.
- **Reminders** shows recent in-app alerts. Low-importance tasks get one planned alert an hour before the deadline. Medium gets 24 and 2 hours, and High gets 24 and 3 hours. High-importance tasks become Critical within four hours. The worker combines reminders that become eligible together.
- To also receive browser notifications, open **Connections** and select **Enable browser notifications**. Keep the dashboard open to receive browser alerts. The app keeps a dashboard history while the tab is closed.
- Snooze delays alerts. Changing a due time replaces its schedule. Marking a task complete cancels its remaining reminders.
- To enable critical calls, configure Twilio and set `DEADLINE_PHONE_ENABLED=true` in `.env`. The worker automatically attempts one call per unfinished, unsnoozed critical Gmail deadline during active reminder hours, subject to the daily call limit. The deadline card shows the call status reported by Twilio. Sample emails can create dashboard reminders. Quiet hours and rate limits are documented in [.env.example](.env.example).

## Try it without Gmail or a model

With Redis running, start `python main.py --no-mcp` in one terminal. In another terminal using the same Redis URL, run `python tests/fixtures/seed_dashboard.py`. Open a high-importance sample, set a due time a few hours ahead, and save it. During active reminder hours, the **Reminders** panel updates on the next worker cycle. Mark the task complete to cancel its next reminder.

Use `python main.py --skip-poll` and **Test Deadline** to try the configured model without connecting Gmail.

## Verify the workflow

The app writes rotating diagnostics to `logs/agent-runtime.log`. Logs from earlier versions may contain email text. Review those files before sharing them.

Use a spare Redis database for the unit suite.

```powershell
python -m pip install -e ".[dev]"
$env:PERSONALOS_TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
python -m pytest -m unit
```

`python scripts/evaluate_deadlines.py` sends eight labeled synthetic messages to the configured model and reports task precision, recall, due-wording accuracy, latency, and token usage. Pass `--case multiple` for a single test. The evaluation sends no Gmail data or phone calls.

See [architecture](docs/ARCHITECTURE.md) for data flow and API paths and [the changelog](CHANGELOG.md) for prior releases. The original hackathon materials in `docs/` document the earlier project.
