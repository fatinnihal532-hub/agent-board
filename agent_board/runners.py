"""Runners do the actual work of a task. The engine does not care which one it is given."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .db import Task

Emit = Callable[[str], None]


@dataclass(frozen=True)
class Result:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


class BudgetTooSmall(Exception):
    pass


class Runner(Protocol):
    def run(self, task: Task, emit: Emit) -> Result:
        """Do the task, calling `emit` with each piece of output as it is produced."""


def build_prompt(task: Task) -> str:
    parts = [f"Task: {task.title}"]
    if task.instructions:
        parts.append(f"Details:\n{task.instructions}")
    if task.feedback:
        parts.append("A reviewer sent your previous attempt back with this feedback. "
                     f"Address it fully:\n{task.feedback}")
    return "\n\n".join(parts)


class DemoRunner:
    """Offline stand-in for a model: streams a canned plan. No network, no cost."""

    def __init__(self, delay: float = 0.4):
        self.delay = delay

    def run(self, task: Task, emit: Emit) -> Result:
        steps = [
            f"[demo runner, attempt {task.attempts}]\n",
            f"Reading the task: {task.title}\n",
            "Drafting a plan...\n",
        ]
        if task.feedback:
            steps.append(f"Applying reviewer feedback: {task.feedback}\n")
        steps.append("Finished. Start the server with ANTHROPIC_API_KEY set to get real output.\n")
        for step in steps:
            time.sleep(self.delay)
            emit(step)
        return Result()


# US dollars per million tokens: (input, output).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
MAX_OUTPUT_TOKENS = 64000
MIN_OUTPUT_TOKENS = 1024


class ClaudeRunner:
    """Runs a task on Claude, streaming the answer and enforcing the task's dollar budget."""

    def __init__(self, model: str = "claude-opus-5-5", client=None):
        if model not in PRICES:
            raise ValueError(f"no price known for {model}; add it to PRICES")
        import anthropic

        self.model = model
        self.client = client or anthropic.Anthropic()

    def output_cap(self, budget: float) -> int:
        """The most output tokens the budget can pay for. No budget means the default cap."""
        if budget <= 0:
            return MAX_OUTPUT_TOKENS
        affordable = int(budget / PRICES[self.model][1] * 1_000_000)
        if affordable < MIN_OUTPUT_TOKENS:
            raise BudgetTooSmall(
                f"a budget of ${budget:.2f} pays for {affordable} output tokens on {self.model}; "
                f"at least {MIN_OUTPUT_TOKENS} are needed")
        return min(MAX_OUTPUT_TOKENS, affordable)

    def run(self, task: Task, emit: Emit) -> Result:
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=self.output_cap(task.budget),
            system=f"You are '{task.agent}', an agent working through a task queue. Complete the "
                   "task you are given and reply with the finished work product, ready for a "
                   "human reviewer to approve or send back.",
            messages=[{"role": "user", "content": build_prompt(task)}],
            # If the model declines on policy grounds, let the API retry on its recommended fallback.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for text in stream.text_stream:
                emit(text)
            message = stream.get_final_message()

        if message.stop_reason == "refusal":
            raise RuntimeError("the model declined this task")
        if message.stop_reason == "max_tokens":
            emit("\n\n[Stopped: the output budget for this task ran out.]")

        price_in, price_out = PRICES[self.model]
        usage = message.usage
        cost = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
        return Result(usage.input_tokens, usage.output_tokens, cost)
