const STORAGE_KEY = "agent-board/v1";
const COLUMNS = [
  { id: "queued", title: "Queued" },
  { id: "running", title: "Running" },
  { id: "review", title: "Needs review" },
  { id: "done", title: "Done" },
];
const ALL_AGENTS = "";

const board = document.getElementById("board");
const summary = document.getElementById("summary");
const filter = document.getElementById("agent-filter");
const agentOptions = document.getElementById("agents");
const form = document.getElementById("new-task");
const template = document.getElementById("card-template");

let tasks = load();
let activeAgent = ALL_AGENTS;

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
    return Array.isArray(saved) ? saved : [];
  } catch {
    return [];
  }
}

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
  } catch {
    // Storage can be unavailable (private mode, quota); the board still works for this session.
  }
}

function money(amount) {
  return amount.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function move(id, status) {
  const task = tasks.find((t) => t.id === id);
  if (!task || task.status === status) return;
  task.status = status;
  save();
  render();
}

function renderCard(task) {
  const card = template.content.firstElementChild.cloneNode(true);
  const position = COLUMNS.findIndex((c) => c.id === task.status);
  card.dataset.id = task.id;
  card.querySelector("h3").textContent = task.title;
  card.querySelector(".agent").textContent = task.agent;
  card.querySelector(".cost").textContent = task.cost ? money(task.cost) : "";

  const back = card.querySelector(".back");
  const forward = card.querySelector(".forward");
  back.disabled = position === 0;
  forward.disabled = position === COLUMNS.length - 1;
  back.addEventListener("click", () => move(task.id, COLUMNS[position - 1].id));
  forward.addEventListener("click", () => move(task.id, COLUMNS[position + 1].id));
  card.querySelector(".remove").addEventListener("click", () => {
    tasks = tasks.filter((t) => t.id !== task.id);
    save();
    render();
  });

  card.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("text/plain", task.id);
    event.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  return card;
}

function renderColumn(column, visible) {
  const section = document.createElement("section");
  section.className = "column";
  const cards = visible.filter((t) => t.status === column.id);

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

  section.addEventListener("dragover", (event) => {
    event.preventDefault();
    section.classList.add("drop-target");
  });
  section.addEventListener("dragleave", () => section.classList.remove("drop-target"));
  section.addEventListener("drop", (event) => {
    event.preventDefault();
    section.classList.remove("drop-target");
    move(event.dataTransfer.getData("text/plain"), column.id);
  });
  return section;
}

function renderAgents() {
  const agents = [...new Set(tasks.map((t) => t.agent))].sort((a, b) => a.localeCompare(b));
  if (!agents.includes(activeAgent)) activeAgent = ALL_AGENTS;

  filter.replaceChildren(new Option("All agents", ALL_AGENTS), ...agents.map((a) => new Option(a, a)));
  filter.value = activeAgent;
  agentOptions.replaceChildren(...agents.map((a) => new Option(a)));
}

function render() {
  renderAgents();
  const visible = activeAgent ? tasks.filter((t) => t.agent === activeAgent) : tasks;
  board.replaceChildren(...COLUMNS.map((column) => renderColumn(column, visible)));

  const open = visible.filter((t) => t.status !== "done");
  const budget = visible.reduce((total, t) => total + (t.cost || 0), 0);
  summary.textContent = visible.length
    ? `${open.length} open of ${visible.length} tasks · ${money(budget)} budgeted`
    : "No tasks yet. Add one below.";
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const title = document.getElementById("task-title").value.trim();
  const agent = document.getElementById("task-agent").value.trim();
  const cost = Number.parseFloat(document.getElementById("task-cost").value);
  if (!title || !agent) return;

  tasks.push({
    id: crypto.randomUUID(),
    title,
    agent,
    cost: Number.isFinite(cost) && cost > 0 ? cost : 0,
    status: COLUMNS[0].id,
  });
  save();
  form.reset();
  document.getElementById("task-title").focus();
  render();
});

filter.addEventListener("change", () => {
  activeAgent = filter.value;
  render();
});

render();
