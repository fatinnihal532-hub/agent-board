# agent-board

A task board that actually runs the work: hand a task to an AI agent, watch its output
stream in live, then approve it or send it back with feedback.

```
Queued ──▶ Running ──▶ Needs you ──▶ Done
              │            │
              ▼            └── send back with feedback ──▶ Queued (runs again)
           Failed ── retry ──▶ Queued
```

- **Real execution** – a pool of worker threads runs queued tasks on Claude, several at once.
- **Live updates** – output streams to every open browser tab over Server-Sent Events.
- **Human in the loop** – nothing is "done" until a person approves it. Sending a task back
  re-runs it with your feedback in the prompt.
- **Budgets** – a task's dollar budget is turned into a hard cap on output tokens, and the
  real cost of every attempt is recorded from the API's token counts.
- **Survives restarts** – tasks live in SQLite; anything interrupted mid-run is requeued on
  start-up.
- **No framework, no build step** – the server is Python's standard library, the front end is
  plain HTML, CSS and JavaScript.

## Run

```bash
python -m agent_board
```

Then open http://127.0.0.1:5173.

Without an API key the board uses a **demo runner** that streams canned text, so you can try
the whole flow offline and for free. To run tasks on Claude:

```bash
pip install -r requirements.txt
```

Set `ANTHROPIC_API_KEY`, then start the server again.

| Option | Default | Meaning |
|---|---|---|
| `--port` | 5173 | Port to listen on |
| `--host` | 127.0.0.1 | Address to bind. The API has no login, so keep it on localhost |
| `--db` | `agent-board.db` | SQLite file |
| `--workers` | 2 | Tasks that may run at the same time |
| `--runner` | `auto` | `claude`, `demo`, or `auto` (Claude if a key is set) |
| `--model` | `claude-opus-5-5` | Also read from `AGENT_BOARD_MODEL` |

## Architecture

```
browser ──HTTP──▶ server.py ──▶ engine.py ──▶ runners.py ──▶ Claude API
   ▲                                │  ▲
   └────── Server-Sent Events ──────┘  └──▶ db.py (SQLite)
```

| File | Responsibility |
|---|---|
| [agent_board/db.py](agent_board/db.py) | Tasks in SQLite and the table of legal status changes |
| [agent_board/engine.py](agent_board/engine.py) | Worker pool, task lifecycle, event bus |
| [agent_board/runners.py](agent_board/runners.py) | `ClaudeRunner` (streaming, budget cap, cost) and `DemoRunner` |
| [agent_board/server.py](agent_board/server.py) | JSON API, event stream, static files |
| [agent_board/static/](agent_board/static/) | The front end |

Design points worth reading the code for:

- **Claiming a task is one atomic SQL statement** (`UPDATE … WHERE status = 'queued'`), so two
  workers can never run the same task. A test fires eight threads at one task to check this.
- **Illegal moves are impossible, not just hidden.** Every status change goes through one
  table of allowed transitions; approving a task twice is a `409 Conflict`.
- **A slow browser cannot stall the workers.** Each subscriber has a bounded queue; one that
  fills up is dropped and resyncs when it reconnects.
- **A failing agent cannot take a worker down.** The error is stored on the task, which waits
  for a retry.

## API

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/tasks` | | All tasks |
| POST | `/api/tasks` | `{title, agent, instructions?, budget?}` | `201` and the new task |
| POST | `/api/tasks/{id}/approve` | | Review → Done |
| POST | `/api/tasks/{id}/reject` | `{feedback}` | Review → Queued, runs again |
| POST | `/api/tasks/{id}/retry` | | Failed → Queued |
| DELETE | `/api/tasks/{id}` | | `204` |
| GET | `/api/events` | | Event stream: `task`, `output`, `deleted` |

Errors are JSON `{"error": "..."}` with `400` (bad input), `404` (no such task) or `409`
(not allowed in the task's current state).

## Tests

```bash
python -m unittest discover -s tests
```

24 tests start the real engine and a real HTTP server on a free port. They cover the full
lifecycle, feedback and retry, cost accounting, parallel execution, crash recovery, input
validation, path traversal on static files, and the live event stream.

## Not verified

`ClaudeRunner` follows the Anthropic SDK's documented streaming pattern, and its budget
arithmetic is unit-tested, but it has not been run against the live API. Everything else was
exercised with the demo runner and test runners.

## Limits

- No login: anyone who can reach the port can add tasks. It is meant for one person on localhost.
- Agents produce text only; they have no tools and cannot change files.
- The budget caps output tokens. Input tokens are counted in the cost but not capped.
