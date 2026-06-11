import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from nicegui import ui
from app.routers import auth, datasets, models, model_runs
from app.ui.pages import register_pages

app = FastAPI(
    title="EPISERVE API",
    description="REST API for browsing datasets and models, and triggering model runs on the EPISERVE platform.",
    version="0.1.0",
)

app.include_router(auth.router)
app.include_router(datasets.router)
app.include_router(models.router)
app.include_router(model_runs.router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>EPISERVE</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f7fa;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 2rem;
      color: #1a1a2e;
    }
    header { text-align: center; margin-bottom: 3rem; }
    header h1 { font-size: 2.2rem; font-weight: 700; letter-spacing: -0.5px; }
    header p { margin-top: 0.6rem; font-size: 1.05rem; color: #555; }
    .cards {
      display: flex;
      gap: 2rem;
      flex-wrap: wrap;
      justify-content: center;
      max-width: 800px;
      width: 100%;
    }
    .card {
      background: #fff;
      border: 1px solid #e0e4ec;
      border-radius: 12px;
      padding: 2rem 2.5rem;
      flex: 1 1 300px;
      max-width: 360px;
      text-decoration: none;
      color: inherit;
      transition: box-shadow 0.15s, transform 0.15s;
    }
    .card:hover { box-shadow: 0 8px 24px rgba(0,0,0,0.10); transform: translateY(-2px); }
    .card .icon { font-size: 2.2rem; margin-bottom: 1rem; }
    .card h2 { font-size: 1.25rem; font-weight: 600; margin-bottom: 0.5rem; }
    .card p { font-size: 0.95rem; color: #555; line-height: 1.55; }
    .card .link-hint {
      display: inline-block;
      margin-top: 1.2rem;
      font-size: 0.9rem;
      font-weight: 600;
      color: #1a6fb5;
    }
  </style>
</head>
<body>
  <header>
    <h1>🔬 EPISERVE</h1>
    <p>Epidemiological data, models, and model runs — all in one platform.</p>
  </header>
  <div class="cards">
    <a class="card" href="/ui/datasets">
      <div class="icon">🖥️</div>
      <h2>Web UI</h2>
      <p>Browse datasets and models, inspect model runs, and explore results through an interactive web interface.</p>
      <span class="link-hint">Open the UI →</span>
    </a>
    <a class="card" href="/docs">
      <div class="icon">📄</div>
      <h2>REST API</h2>
      <p>Integrate programmatically. The interactive API docs let you explore all endpoints and try requests directly in the browser.</p>
      <span class="link-hint">Open API docs →</span>
    </a>
  </div>
</body>
</html>""")


@app.get("/health")
def health():
    return {"status": "ok"}


register_pages()
ui.run_with(
    app,
    title="EPISERVE",
    favicon="🔬",
    dark=False,
    storage_secret=os.environ.get("STORAGE_SECRET", "episerve-dev-secret-change-me"),
)
