import threading
import unittest

from agent_board.db import (DONE, FAILED, QUEUED, REVIEW, RUNNING, Database, InvalidTransition)
from agent_board.engine import Engine, EventBus
from agent_board.runners import (BudgetTooSmall, ClaudeRunner, DemoRunner, Result, build_prompt)


class ScriptedRunner:
    """Emits fixed text, records the tasks it saw, and fails when the title says so."""

    def __init__(self):
        self.seen = []

    def run(self, task, emit):
        self.seen.append(task)
        if "explode" in task.title:
            raise RuntimeError("boom")
        emit("first ")
        emit("second")
        return Result(input_tokens=100, output_tokens=50, cost=0.25)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.runner = ScriptedRunner()
        self.engine = Engine(self.db, self.runner, workers=2)
        self.events = self.engine.bus.subscribe()
        self.engine.start()
        self.addCleanup(self.db.close)
        self.addCleanup(self.engine.stop)

    def drain(self):
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events


class LifecycleTest(EngineCase):
    def test_task_runs_and_lands_in_review_with_output_and_cost(self):
        task = self.engine.submit("Write release notes", "writer", budget=1.0)
        self.engine.wait_idle()

        task = self.db.get(task.id)
        self.assertEqual(task.status, REVIEW)
        self.assertEqual(task.output, "first second")
        self.assertEqual((task.cost, task.input_tokens, task.output_tokens), (0.25, 100, 50))
        self.assertEqual(task.attempts, 1)

    def test_events_tell_the_whole_story_in_order(self):
        task = self.engine.submit("Write release notes", "writer")
        self.engine.wait_idle()

        story = [e["task"]["status"] if e["type"] == "task" else e["text"] for e in self.drain()
                 if e.get("id", e.get("task", {}).get("id")) == task.id]
        self.assertEqual(story, [QUEUED, RUNNING, "first ", "second", REVIEW])

    def test_approve_finishes_the_task(self):
        task = self.engine.submit("Write release notes", "writer")
        self.engine.wait_idle()
        self.assertEqual(self.engine.approve(task.id).status, DONE)

    def test_sending_back_reruns_with_feedback_and_adds_up_cost(self):
        task = self.engine.submit("Write release notes", "writer")
        self.engine.wait_idle()

        self.engine.reject(task.id, "Too long")
        self.engine.wait_idle()

        task = self.db.get(task.id)
        self.assertEqual(task.status, REVIEW)
        self.assertEqual(task.attempts, 2)
        self.assertEqual(task.cost, 0.5)
        self.assertEqual(task.output, "first second")  # not doubled: output restarts each attempt
        self.assertEqual(self.runner.seen[-1].feedback, "Too long")

    def test_runner_failure_is_recorded_and_can_be_retried(self):
        task = self.engine.submit("explode please", "writer")
        self.engine.wait_idle()

        failed = self.db.get(task.id)
        self.assertEqual(failed.status, FAILED)
        self.assertEqual(failed.error, "RuntimeError: boom")

        self.engine.retry(task.id)
        self.engine.wait_idle()
        self.assertEqual(self.db.get(task.id).attempts, 2)

    def test_a_failure_does_not_stop_later_tasks(self):
        self.engine.submit("explode please", "writer")
        ok = self.engine.submit("Write release notes", "writer")
        self.engine.wait_idle()
        self.assertEqual(self.db.get(ok.id).status, REVIEW)

    def test_illegal_moves_are_refused(self):
        task = self.engine.submit("Write release notes", "writer")
        self.engine.wait_idle()
        self.engine.approve(task.id)

        for action in (lambda: self.engine.approve(task.id),
                       lambda: self.engine.reject(task.id, "again"),
                       lambda: self.engine.retry(task.id)):
            with self.assertRaises(InvalidTransition):
                action()

    def test_delete_publishes_and_unknown_ids_raise(self):
        task = self.engine.submit("Write release notes", "writer")
        self.engine.wait_idle()
        self.drain()

        self.assertTrue(self.engine.delete(task.id))
        self.assertEqual(self.drain(), [{"type": "deleted", "id": task.id}])
        self.assertFalse(self.engine.delete(task.id))
        with self.assertRaises(KeyError):
            self.engine.approve(task.id)


class ConcurrencyTest(unittest.TestCase):
    def test_each_task_runs_exactly_once_across_workers(self):
        db = Database(":memory:")
        runner = ScriptedRunner()
        engine = Engine(db, runner, workers=4)
        engine.start()
        self.addCleanup(db.close)
        self.addCleanup(engine.stop)

        ids = [engine.submit(f"Task {i}", "writer").id for i in range(40)]
        engine.wait_idle()

        self.assertEqual(sorted(t.id for t in runner.seen), sorted(ids))
        self.assertTrue(all(t.status == REVIEW and t.attempts == 1 for t in db.all()))

    def test_a_task_can_only_be_claimed_once(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        task = db.create("Task", "writer")
        claims = []
        threads = [threading.Thread(target=lambda: claims.append(db.claim(task.id))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_workers_really_run_in_parallel(self):
        both_started = threading.Barrier(2, timeout=5)

        class Meeting:
            def run(self, task, emit):
                both_started.wait()  # only passes if two tasks are running at the same time
                return Result()

        db = Database(":memory:")
        engine = Engine(db, Meeting(), workers=2)
        engine.start()
        self.addCleanup(db.close)
        self.addCleanup(engine.stop)
        engine.submit("A", "x")
        engine.submit("B", "x")
        engine.wait_idle()
        self.assertEqual([t.status for t in db.all()], [REVIEW, REVIEW])


class RecoveryTest(unittest.TestCase):
    def test_tasks_left_running_by_a_crash_are_requeued_and_finished_on_start(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        task = db.create("Interrupted", "writer")
        db.claim(task.id)  # simulate a process that died mid-run
        db.append_output(task.id, "half an ans")

        engine = Engine(db, ScriptedRunner(), workers=1)
        engine.start()
        self.addCleanup(engine.stop)
        engine.wait_idle()

        task = db.get(task.id)
        self.assertEqual(task.status, REVIEW)
        self.assertEqual(task.attempts, 2)


class EventBusTest(unittest.TestCase):
    def test_a_stalled_subscriber_is_dropped_instead_of_blocking(self):
        bus = EventBus()
        stalled = bus.subscribe()
        for i in range(stalled.maxsize + 5):
            bus.publish({"n": i})  # must not block
        healthy = bus.subscribe()
        bus.publish({"n": "last"})
        self.assertEqual(healthy.get_nowait(), {"n": "last"})
        self.assertEqual(stalled.qsize(), stalled.maxsize)


class RunnerTest(unittest.TestCase):
    def task(self, **overrides):
        db = Database(":memory:")
        self.addCleanup(db.close)
        task = db.create("Summarise the changelog", "writer", "Keep it under 100 words", 0.0)
        return db.move(task.id, RUNNING, **overrides) if overrides else task

    def test_prompt_includes_details_and_feedback(self):
        prompt = build_prompt(self.task(feedback="Mention the breaking change"))
        self.assertIn("Summarise the changelog", prompt)
        self.assertIn("Keep it under 100 words", prompt)
        self.assertIn("Mention the breaking change", prompt)

    def test_demo_runner_streams_text_and_costs_nothing(self):
        chunks = []
        result = DemoRunner(delay=0).run(self.task(), chunks.append)
        self.assertEqual(result, Result())
        self.assertIn("Summarise the changelog", "".join(chunks))

    def test_budget_caps_output_tokens(self):
        runner = ClaudeRunner.__new__(ClaudeRunner)  # skip __init__: no SDK or key needed here
        runner.model = "claude-opus-5-5"
        self.assertEqual(runner.output_cap(0), 64000)      # no budget: default cap
        self.assertEqual(runner.output_cap(0.10), 5000)    # $0.10 at $20 per million tokens
        self.assertEqual(runner.output_cap(100), 64000)    # never above the default cap
        with self.assertRaises(BudgetTooSmall):
            runner.output_cap(0.01)


if __name__ == "__main__":
    unittest.main()
