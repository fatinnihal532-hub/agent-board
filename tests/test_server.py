import http.client
import json
import threading
import unittest

from agent_board.db import Database
from agent_board.engine import Engine
from agent_board.runners import Result
from agent_board.server import make_server


class QuickRunner:
    def run(self, task, emit):
        emit("done: " + task.title)
        return Result(cost=0.5)


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.engine = Engine(self.db, QuickRunner(), workers=1)
        self.engine.start()
        self.server = make_server(self.engine, port=0)  # port 0: the OS picks a free one
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.db.close)
        self.addCleanup(self.engine.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        connection.request(method, path, body=data,
                           headers={"Content-Type": "application/json"} if data else {})
        response = connection.getresponse()
        text = response.read().decode()
        return response.status, (json.loads(text) if text and "json" in response.getheader("Content-Type", "") else text)

    def create(self, **overrides):
        body = {"title": "Write docs", "agent": "writer", **overrides}
        status, task = self.call("POST", "/api/tasks", body)
        self.assertEqual(status, 201)
        self.engine.wait_idle()
        return task


class ApiTest(ServerCase):
    def test_create_run_approve(self):
        task = self.create(budget=2)

        status, tasks = self.call("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([(t["status"], t["output"], t["cost"], t["budget"]) for t in tasks],
                         [("review", "done: Write docs", 0.5, 2.0)])

        status, approved = self.call("POST", f"/api/tasks/{task['id']}/approve")
        self.assertEqual((status, approved["status"]), (200, "done"))

    def test_send_back_needs_feedback_and_reruns(self):
        task = self.create()
        status, body = self.call("POST", f"/api/tasks/{task['id']}/reject", {"feedback": "  "})
        self.assertEqual(status, 400)
        self.assertIn("feedback", body["error"])

        status, _ = self.call("POST", f"/api/tasks/{task['id']}/reject", {"feedback": "Shorter"})
        self.assertEqual(status, 200)
        self.engine.wait_idle()
        _, tasks = self.call("GET", "/api/tasks")
        self.assertEqual((tasks[0]["attempts"], tasks[0]["feedback"], tasks[0]["cost"]), (2, "Shorter", 1.0))

    def test_validation(self):
        for body in ({"agent": "a"}, {"title": " ", "agent": "a"}, {"title": "t"},
                     {"title": "t", "agent": "a", "budget": -1},
                     {"title": "t", "agent": "a", "budget": "lots"},
                     {"title": "t", "agent": "a", "budget": True},
                     {"title": "t", "agent": "a", "instructions": 5}):
            status, payload = self.call("POST", "/api/tasks", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)
        self.assertEqual(self.call("POST", "/api/tasks", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/tasks", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.call("GET", "/api/tasks")[1], [])

    def test_wrong_state_is_a_conflict_and_unknown_task_is_not_found(self):
        task = self.create()
        self.call("POST", f"/api/tasks/{task['id']}/approve")
        self.assertEqual(self.call("POST", f"/api/tasks/{task['id']}/approve")[0], 409)
        self.assertEqual(self.call("POST", f"/api/tasks/{task['id']}/retry")[0], 409)

        missing = "0" * 32
        self.assertEqual(self.call("POST", f"/api/tasks/{missing}/approve")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/tasks/{missing}")[0], 404)
        self.assertEqual(self.call("POST", "/api/tasks/not-an-id/approve")[0], 404)

    def test_delete(self):
        task = self.create()
        self.assertEqual(self.call("DELETE", f"/api/tasks/{task['id']}")[0], 204)
        self.assertEqual(self.call("GET", "/api/tasks")[1], [])


class StaticTest(ServerCase):
    def test_serves_the_front_end(self):
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Agent Board</title>", page)
        self.assertEqual(self.call("GET", "/app.js")[0], 200)

    def test_cannot_escape_the_static_folder(self):
        for path in ("/../db.py", "/..%2Fdb.py", "/../../README.md", "/missing.css"):
            self.assertEqual(self.call("GET", path)[0], 404, path)


class EventStreamTest(ServerCase):
    def test_changes_are_pushed_to_a_connected_client(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/events")
        response = connection.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")

        task = self.create()

        seen = []
        while not seen or seen[-1] != ("task", "review"):
            line = response.readline().decode().strip()
            if line.startswith("data: "):
                event = json.loads(line[len("data: "):])
                seen.append((event["type"], event.get("task", {}).get("status", event.get("text"))))
        self.assertEqual(seen, [("task", "queued"), ("task", "running"),
                                ("output", "done: Write docs"), ("task", "review")])
        self.assertEqual(task["status"], "queued")


if __name__ == "__main__":
    unittest.main()
