# Deadline tracker architecture

`main.py` starts FastAPI, the Gmail MCP server, the Observer and Planner agents, and a reminder worker. In dashboard-only mode, FastAPI and the reminder worker run against Redis without the email agents.

## Email to deadline

1. The Observer reads paginated Gmail changes through MCP and deduplicates messages into `events:queue`. It saves each page checkpoint only after the page has been queued. If Gmail history expires, it rescans the elapsed inbox window.
2. The Planner leases one Redis job at a time. It validates source excerpts and due phrases, persists the tasks in one transaction, and acknowledges the job. Expired leases return to the queue and processing errors schedule a delayed retry. ChromaDB supplies priority preferences.
3. The deadline service combines the extracted fields with editable task state in `deadline:tasks`. Relative, missing, and unresolvable dates require confirmation. Priority is calculated from importance and time remaining.
4. The reminder worker checks unfinished tasks with a confirmed due time. It applies snooze, quiet hours, and rate limits before claiming a reminder. The claim and dashboard history update are atomic. Connected dashboards receive the reminder over WebSocket. A browser notification requires browser permission.
5. When Twilio calls are enabled, the worker reserves one call attempt for each eligible critical Gmail deadline and rechecks the task before calling. It stores the call SID and polls Twilio for the outcome displayed on the deadline card. Sample deadlines are eligible for dashboard reminders.

The MCP server exposes `sync_gmail(cursor)` and `read_emails(max_results, query)` over SSE at `/sse`. Both use the Gmail read-only scope. Redis holds email content in pending scan jobs, along with deduplication IDs, source evidence for saved deadlines, task state, and the latest 50 in-app reminders. Keep Redis persistence enabled so jobs, checkpoints, and tasks survive a Redis restart.

## Dashboard and API

| Endpoint | Purpose |
|---|---|
| `GET /` | Deadline dashboard |
| `GET /api/deadlines` | Task cards with derived status and priority |
| `PATCH /api/deadlines/{id}` | Confirm or edit the due time, complete, reopen, or snooze a task |
| `GET /api/reminders` | Recent in-app alerts and phone availability |
| `GET /api/emails` | Compatibility feed for stored deadline extractions |
| `POST /api/poll/now` | Trigger a Gmail scan |
| `POST /api/events/inject` | Queue a synthetic email for classification |
| `GET /api/metrics` and `GET /api/feed` | Task counts and recent activity |
| `GET /api/preferences` and `POST /api/preferences` | Read and add preferences |
| `GET /api/health` | Service health |
| `WS /ws` | Dashboard and reminder updates |

The dashboard defaults to a loopback bind for single-user use. The reminder worker needs a running app to generate alerts.
