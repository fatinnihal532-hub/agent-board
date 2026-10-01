"""The engine: a worker pool that runs queued tasks and publishes every change to subscribers."""

from __future__ import annotations

import queue
import threading

from .db import DONE, FAILED, QUEUED, REVIEW, Database, Task
from .runners import Runner

_STOP = object()


class EventBus:
    """Fan-out of events to any number of subscribers, each with its own queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # A stalled client must not block the workers; it will resync on reconnect.
                self.unsubscribe(q)


class Engine:
    def __init__(self, db: Database, runner: Runner, workers: int = 2):
        self.db = db
        self.runner = runner
        self.bus = EventBus()
        self._pending: queue.Queue = queue.Queue()
        self._threads = [threading.Thread(target=self._work, daemon=True, name=f"worker-{i}")
                         for i in range(workers)]

    def start(self) -> None:
        self.db.recover()
        for task in self.db.all():
            if task.status == QUEUED:
                self._pending.put(task.id)
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        for _ in self._threads:
            self._pending.put(_STOP)
        for thread in self._threads:
            thread.join(timeout=10)

    def wait_idle(self) -> None:
        """Block until every submitted task has finished running. For tests."""
        self._pending.join()

    # --- actions a person takes on the board -------------------------------------------------

    def submit(self, title: str, agent: str, instructions: str = "", budget: float = 0.0) -> Task:
        task = self.db.create(title, agent, instructions, budget)
        self._changed(task)
        self._pending.put(task.id)
        return task

    def approve(self, task_id: str) -> Task:
        return self._changed(self.db.move(task_id, DONE))

    def reject(self, task_id: str, feedback: str) -> Task:
        """Send a reviewed task back to the agent with feedback; it runs again."""
        task = self.db.move(task_id, QUEUED, feedback=feedback, output="")
        self._changed(task)
        self._pending.put(task.id)
        return task

    def retry(self, task_id: str) -> Task:
        task = self.db.move(task_id, QUEUED, output="")
        self._changed(task)
        self._pending.put(task.id)
        return task

    def delete(self, task_id: str) -> bool:
        deleted = self.db.delete(task_id)
        if deleted:
            self.bus.publish({"type": "deleted", "id": task_id})
        return deleted

    # --- workers -----------------------------------------------------------------------------

    def _changed(self, task: Task) -> Task:
        self.bus.publish({"type": "task", "task": task.to_dict()})
        return task

    def _work(self) -> None:
        while True:
            task_id = self._pending.get()
            try:
                if task_id is _STOP:
                    return
                self._run(task_id)
            finally:
                self._pending.task_done()

    def _run(self, task_id: str) -> None:
        try:
            task = self.db.claim(task_id)
        except KeyError:
            return  # deleted while waiting in the queue
        if task is None:
            return
        self._changed(task)

        def emit(text: str) -> None:
            self.db.append_output(task_id, text)
            self.bus.publish({"type": "output", "id": task_id, "text": text})

        try:
            result = self.runner.run(task, emit)
        except Exception as e:  # a runner failure must never take the worker down
            self._finish(task_id, FAILED, error=f"{type(e).__name__}: {e}")
            return
        self._finish(task_id, REVIEW, cost=task.cost + result.cost,
                     input_tokens=task.input_tokens + result.input_tokens,
                     output_tokens=task.output_tokens + result.output_tokens)

    def _finish(self, task_id: str, status: str, **fields) -> None:
        try:
            self._changed(self.db.move(task_id, status, **fields))
        except KeyError:
            pass  # deleted while it was running
