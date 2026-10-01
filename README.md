# agent-board

A kanban board for the work you hand to AI agents: what is queued, what is running,
what is waiting for your review, and how much budget you have committed.

- **No build step, no dependencies** – three static files.
- **Local-first** – tasks live in your browser's `localStorage`; nothing is sent anywhere.
- **Four stages** – Queued → Running → Needs review → Done.
- **Per-agent filter and budget total** – see one agent's load, or everything.
- **Drag and drop, or the arrow buttons** – the buttons work from the keyboard and on touch screens.
- Light and dark theme follow your system setting.

## Run

Open `index.html` through any static server, for example:

```bash
python -m http.server 5173
```

Then visit http://localhost:5173. (Opening the file directly with `file://` does not work,
because browsers block JavaScript modules there.)

## Deploy

It is a static site, so GitHub Pages works as is: in the repository settings choose
**Pages → Deploy from a branch → main / root**.

## Files

| File | Purpose |
|---|---|
| `index.html` | Markup and the card template |
| `style.css` | Layout and themes |
| `app.js` | State, rendering, drag and drop, persistence |
