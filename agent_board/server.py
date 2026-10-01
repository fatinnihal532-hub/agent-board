"""HTTP server: a small JSON API, a Server-Sent Events stream, and the static front end."""

from __future__ import annotations

import json
import mimetypes
import queue
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .db import InvalidTransition
from .engine import Engine

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
HEARTBEAT_SECONDS = 15
_TASK_ROUTE = re.compile(r"^/api/tasks/([0-9a-f]{32})(?:/(approve|reject|retry))?$")


class BadRequest(Exception):
    pass


def parse_new_task(body: dict) -> dict:
    title = body.get("title")
    agent = body.get("agent")
    instructions = body.get("instructions", "")
    budget = body.get("budget", 0)
    if not isinstance(title, str) or not title.strip():
        raise BadRequest("title is required")
    if not isinstance(agent, str) or not agent.strip():
        raise BadRequest("agent is required")
    if not isinstance(instructions, str):
        raise BadRequest("instructions must be text")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0:
        raise BadRequest("budget must be a number that is zero or more")
    return {"title": title.strip(), "agent": agent.strip(),
            "instructions": instructions.strip(), "budget": float(budget)}


def make_handler(engine: Engine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        # --- helpers ---------------------------------------------------------------------

        def send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status: HTTPStatus, message: str) -> None:
            self.send_json({"error": message}, status)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise BadRequest("request body is too large")
            if not length:
                return {}
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise BadRequest("request body is not valid JSON") from None
            if not isinstance(body, dict):
                raise BadRequest("request body must be a JSON object")
            return body

        def dispatch(self, action) -> None:
            try:
                action()
            except BadRequest as e:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(e))
            except KeyError:
                self.send_error_json(HTTPStatus.NOT_FOUND, "no such task")
            except InvalidTransition as e:
                self.send_error_json(HTTPStatus.CONFLICT, str(e))

        # --- routes ----------------------------------------------------------------------

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/tasks":
                self.send_json([t.to_dict() for t in engine.db.all()])
            elif path == "/api/events":
                self.stream_events()
            else:
                self.send_static(path)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/tasks":
                self.dispatch(lambda: self.send_json(
                    engine.submit(**parse_new_task(self.read_json())).to_dict(), HTTPStatus.CREATED))
                return
            match = _TASK_ROUTE.match(path)
            if not match or not match.group(2):
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            task_id, action = match.groups()

            def act() -> None:
                body = self.read_json()
                if action == "approve":
                    task = engine.approve(task_id)
                elif action == "retry":
                    task = engine.retry(task_id)
                else:
                    feedback = body.get("feedback")
                    if not isinstance(feedback, str) or not feedback.strip():
                        raise BadRequest("feedback is required when sending a task back")
                    task = engine.reject(task_id, feedback.strip())
                self.send_json(task.to_dict())

            self.dispatch(act)

        def do_DELETE(self) -> None:
            match = _TASK_ROUTE.match(self.path.split("?", 1)[0])
            if not match or match.group(2):
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            elif engine.delete(match.group(1)):
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "no such task")

        def stream_events(self) -> None:
            events = engine.bus.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    try:
                        event = events.get(timeout=HEARTBEAT_SECONDS)
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass  # the browser went away
            finally:
                engine.bus.unsubscribe(events)

        def send_static(self, path: str) -> None:
            name = "index.html" if path == "/" else path.lstrip("/")
            target = (STATIC / name).resolve()
            # Refuse anything that resolves outside the static folder.
            if STATIC.resolve() not in target.parents or not target.is_file():
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def make_server(engine: Engine, host: str = "127.0.0.1", port: int = 5173) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    server.daemon_threads = True
    return server
