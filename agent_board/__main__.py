"""Start the board: python -m agent_board"""

from __future__ import annotations

import argparse
import os
import sys

from .db import Database
from .engine import Engine
from .runners import ClaudeRunner, DemoRunner
from .server import make_server


def pick_runner(name: str, model: str):
    if name == "auto":
        name = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "demo"
    if name == "demo":
        return DemoRunner(), "demo runner (no API key, canned output)"
    try:
        return ClaudeRunner(model), f"Claude ({model})"
    except ImportError:
        sys.exit("The Claude runner needs the Anthropic SDK: pip install anthropic")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_board")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--db", default="agent-board.db", help="path to the SQLite file")
    parser.add_argument("--workers", type=int, default=2, help="tasks that may run at once")
    parser.add_argument("--runner", choices=["auto", "demo", "claude"], default="auto")
    parser.add_argument("--model", default=os.environ.get("AGENT_BOARD_MODEL", "claude-opus-5-5"))
    args = parser.parse_args(argv)

    runner, label = pick_runner(args.runner, args.model)
    db = Database(args.db)
    engine = Engine(db, runner, workers=args.workers)
    engine.start()
    server = make_server(engine, args.host, args.port)
    print(f"Agent Board on http://{args.host}:{args.port} using the {label}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.stop()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
