const COLUMNS = [
  { id: "queued", title: "Queued", statuses: ["queued"] },
  { id: "running", title: "Running", statuses: ["running"] },
  { id: "review", title: "Needs you", statuses: ["review", "failed"] },
  { id: "done", title: "Done", statuses: ["done"] },
];

const board = document.getElementById("board");
const summary = document.getElementById("summary");
const connection = document.getElementById("connection");
const agentOptions = document.getElementById("agents");
const form = document.getElementById("new-task");
const formError = document.getElementById("form-error");
const template = document.getElementById("card-template");

const tasks = new Map();
// UI state that must survive a re-render.
const openOutputs = new Set();
const feedbackDrafts = new Map();

function money(amount) {
  return amount.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 4 });
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 204) return null;
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function button(label, onClick, className = "") {
  const el = document.createElement("button");
  el.type = "button";
  el.textContent = label;
  el.className = className;
  el.addEventListener("click", async () => {
    el.disabled = true;
    try {
      await onClick();
    } catch (error) {
      alert(error.message);
      el.disabled = false;
    }
  });
  return el;
}

function renderCard(task) {
  const card = template.content.firstElementChild.cloneNode(true);
  card.dataset.id = task.id;
  card.classList.toggle("failed", task.status === "failed");
  card.querySelector("h3").textContent = task.title;
  card.querySelector(".agent").textContent =
    task.attempts > 1 ? `${task.agent} · attempt ${task.attempts}` : task.agent;

  let cost = task.cost ? money(task.cost) : "";
  if (task.budget) cost = task.cost ? `${cost} of ${money(task.budget)}` : `budget ${money(task.budget)}`;
  card.querySelector(".cost").textContent = cost;

  const problem = card.querySelector(".problem");
  if (task.status === "failed") {
    problem.textContent = `Failed: ${task.error}`;
    problem.hidden = false;
  } else if (task.feedback && task.status !== "done") {
    problem.textContent = `Sent back: ${task.feedback}`;
    problem.hidden = false;
  }

  const details = card.querySelector("details");
  details.hidden = !task.output;
  details.open = openOutputs.has(task.id) || task.status === "running";
  details.querySelector("pre").textContent = task.output;
  details.addEventListener("toggle", () => {
    if (details.open) openOutputs.add(task.id);
    else openOutputs.delete(task.id);
  });

  const actions = card.querySelector(".main-actions");
  const feedback = card.querySelector(".feedback");
  const draft = feedback.querySelector("textarea");
  if (task.status === "review") {
    actions.append(
      button("Approve", () => api("POST", `/api/tasks/${task.id}/approve`), "primary"),
      button("Send back…", () => {
        feedbackDrafts.set(task.id, feedbackDrafts.get(task.id) ?? "");
        render();
      }),
    );
    if (feedbackDrafts.has(task.id)) {
      feedback.hidden = false;
      actions.hidden = true;
      draft.value = feedbackDrafts.get(task.id);
      draft.addEventListener("input", () => feedbackDrafts.set(task.id, draft.value));
      feedback.querySelector(".cancel").addEventListener("click", () => {
        feedbackDrafts.delete(task.id);
        render();
      });
      feedback.addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
          await api("POST", `/api/tasks/${task.id}/reject`, { feedback: draft.value });
          feedbackDrafts.delete(task.id);
        } catch (error) {
          alert(error.message);
        }
      });
    }
  }
  if (task.status === "failed") {
    actions.append(button("Retry", () => api("POST", `/api/tasks/${task.id}/retry`), "primary"));
  }
  if (task.status !== "running") {
    actions.append(button("Delete", () => api("DELETE", `/api/tasks/${task.id}`), "remove"));
  }
  return card;
}

function renderColumn(column, all) {
  const section = document.createElement("section");
  section.className = "column";
  const cards = all.filter((t) => column.statuses.includes(t.status));

  const heading = document.createElement("h2");
  const name = document.createElement("span");
  const count = document.createElement("span");
  name.textContent = column.title;
  count.textContent = cards.length;
  heading.append(name, count);
  section.append(heading);

  if (cards.length) {
    section.append(...cards.map(renderCard));
  } else {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Nothing here.";
    section.append(empty);
  }
  return section;
}

function render() {
  const all = [...tasks.values()].sort((a, b) => a.created_at - b.created_at);
  // Re-rendering replaces the feedback box, so put the cursor back where the person was typing.
  const focused = document.activeElement?.closest?.(".card")?.dataset.id;
  const typing = document.activeElement?.tagName === "TEXTAREA";

  board.replaceChildren(...COLUMNS.map((column) => renderColumn(column, all)));
  if (focused && typing) {
    board.querySelector(`.card[data-id="${focused}"] .feedback textarea`)?.focus();
  }

  const agents = [...new Set(all.map((t) => t.agent))].sort((a, b) => a.localeCompare(b));
  agentOptions.replaceChildren(...agents.map((a) => new Option(a)));

  const waiting = all.filter((t) => t.status === "review" || t.status === "failed").length;
  const spent = all.reduce((total, t) => total + t.cost, 0);
  summary.textContent = all.length
    ? `${all.length} ${all.length === 1 ? "task" : "tasks"} · ${waiting} waiting for you · ${money(spent)} spent`
    : "No tasks yet. Add one below.";
}

function onEvent(event) {
  if (event.type === "task") {
    tasks.set(event.task.id, event.task);
    render();
  } else if (event.type === "deleted") {
    tasks.delete(event.id);
    render();
  } else if (event.type === "output") {
    const task = tasks.get(event.id);
    if (!task) return;
    task.output += event.text;
    // Update in place so streaming text does not rebuild the whole board.
    const details = board.querySelector(`.card[data-id="${event.id}"] details`);
    if (details) {
      details.hidden = false;
      details.querySelector("pre").textContent = task.output;
    }
  }
}

async function loadAll() {
  const all = await api("GET", "/api/tasks");
  tasks.clear();
  for (const task of all) tasks.set(task.id, task);
  render();
}

function connect() {
  const source = new EventSource("/api/events");
  source.onopen = () => {
    connection.textContent = "";
    // Anything that changed while disconnected is picked up here.
    loadAll().catch((error) => (connection.textContent = error.message));
  };
  source.onmessage = (message) => onEvent(JSON.parse(message.data));
  source.onerror = () => {
    connection.textContent = "Connection lost. Reconnecting…";
  };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  const budget = Number.parseFloat(document.getElementById("task-budget").value);
  try {
    await api("POST", "/api/tasks", {
      title: document.getElementById("task-title").value,
      agent: document.getElementById("task-agent").value,
      instructions: document.getElementById("task-instructions").value,
      budget: Number.isFinite(budget) ? budget : 0,
    });
    form.reset();
    document.getElementById("task-title").focus();
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
  }
});

connect();
