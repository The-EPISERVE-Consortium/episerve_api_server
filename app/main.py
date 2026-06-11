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
  <title>EPISERVE API Server</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      background: #f5f7fa;
      color: #333;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }

    /* Navbar */
    nav {
      background: #1b2a35;
      padding: 0 2rem;
      height: 54px;
      display: flex;
      align-items: center;
    }
    nav .brand {
      color: #fff;
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: 0.5px;
      text-decoration: none;
    }

    /* Hero */
    .hero {
      background: #206b82;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 3.5rem 4rem;
      gap: 2rem;
      overflow: hidden;
    }
    .hero-text { max-width: 540px; }
    .hero-text h1 {
      font-size: 2rem;
      font-weight: 700;
      line-height: 1.2;
      margin-bottom: 1rem;
    }
    .hero-text p {
      font-size: 1rem;
      line-height: 1.6;
      opacity: 0.9;
    }
    .hero-img {
      flex-shrink: 0;
      max-width: 260px;
      opacity: 0.9;
    }
    .hero-img img { width: 100%; display: block; }

    /* Cards section */
    .content {
      flex: 1;
      padding: 3rem 4rem;
      display: flex;
      gap: 2rem;
      flex-wrap: wrap;
      align-items: flex-start;
    }
    .card {
      background: #fff;
      border: 1px solid rgba(0,0,0,0.125);
      border-radius: 0.25rem;
      padding: 1.75rem 2rem;
      flex: 1 1 280px;
      max-width: 400px;
      text-decoration: none;
      color: #333;
      display: flex;
      flex-direction: column;
      transition: box-shadow 0.15s;
    }
    .card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.12); }
    .card h2 {
      font-size: 1.15rem;
      font-weight: 700;
      margin-bottom: 0.6rem;
      color: #1b2a35;
    }
    .card p {
      font-size: 0.93rem;
      line-height: 1.6;
      color: #555;
      flex: 1;
    }
    .card .cta {
      display: inline-block;
      margin-top: 1.25rem;
      padding: 0.45rem 1.1rem;
      background: #206b82;
      color: #fff;
      border-radius: 0.25rem;
      font-size: 0.88rem;
      font-weight: 600;
      align-self: flex-start;
      transition: background 0.15s;
    }
    .card:hover .cta { background: #1b5b6f; }

    /* Footer */
    footer {
      background: #1b2a35;
      color: rgba(255,255,255,0.55);
      font-size: 0.82rem;
      text-align: center;
      padding: 1rem 2rem;
    }

    @media (max-width: 680px) {
      .hero { flex-direction: column; padding: 2.5rem 1.5rem; }
      .hero-img { max-width: 180px; align-self: center; }
      .content { padding: 2rem 1.5rem; }
    }
  </style>
</head>
<body>
  <nav>
    <a class="brand" href="/">EPISERVE</a>
  </nav>

  <div class="hero">
    <div class="hero-text">
      <h1>EPISERVE API Server</h1>
      <p>Access epidemiological datasets, simulation models, and model run results for infectious disease research and pandemic preparedness in Germany.</p>
    </div>
    <div class="hero-img">
      <img src="https://episerve.zib.de/img/hero.png" alt="">
    </div>
  </div>

  <div class="content">
    <a class="card" href="/ui/datasets">
      <h2>Web UI</h2>
      <p>Browse datasets and models, inspect model runs, and explore results through an interactive web interface.</p>
      <span class="cta">Open the UI →</span>
    </a>
    <a class="card" href="/docs">
      <h2>REST API</h2>
      <p>Integrate programmatically. The interactive API docs let you explore all endpoints and try requests directly in the browser.</p>
      <span class="cta">Open API docs →</span>
    </a>
  </div>

  <footer>
    &copy; EPISERVE Consortium
  </footer>
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
