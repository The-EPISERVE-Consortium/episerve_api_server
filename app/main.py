from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from nicegui import ui
from app.routers import auth, datasets, models, model_runs, benchmarks, specs, lineage
from app.secrets import require_secret
from app.ui.footer import FOOTER_CSS, footer_html
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
app.include_router(benchmarks.router)
app.include_router(specs.router)
app.include_router(lineage.router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    return HTMLResponse(content=_ROOT_HTML.replace("/*FOOTER_CSS*/", FOOTER_CSS).replace("<!--FOOTER_HTML-->", footer_html()))


_ROOT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>EPISERVE API Server</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      color: #333;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }

    /* Hero */
    .hero {
      background: #ede8da;
      text-align: center;
      padding: 5rem 2rem 3.5rem;
    }
    .hero h1 {
      font-size: 3.6rem;
      font-weight: 800;
      color: #1a1a1a;
      line-height: 1.05;
      letter-spacing: -1px;
    }
    .hero h1 span {
      display: block;
      color: #3A833A;
    }
    .hero p {
      margin: 1.5rem auto 0;
      max-width: 560px;
      font-size: 1rem;
      color: #666;
      line-height: 1.6;
    }

    /* Compact cards */
    .options {
      display: flex;
      gap: 1rem;
      justify-content: center;
      margin-top: 2.5rem;
      flex-wrap: wrap;
    }
    .option-card {
      background: #fff;
      border: 1px solid rgba(0,0,0,0.15);
      border-radius: 0.25rem;
      padding: 1rem 1.5rem;
      width: 260px;
      text-decoration: none;
      color: #333;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      transition: box-shadow 0.15s;
    }
    .option-card:hover { box-shadow: 0 4px 14px rgba(0,0,0,0.12); }
    .option-card h2 {
      font-size: 1rem;
      font-weight: 700;
      color: #1a1a1a;
    }
    .option-card p {
      font-size: 0.85rem;
      color: #666;
      line-height: 1.45;
      margin: 0;
    }
    .option-card .cta {
      margin-top: 0.5rem;
      font-size: 0.83rem;
      font-weight: 600;
      color: #3A833A;
    }

    /* Full-width hero image */
    .hero-image {
      width: 100%;
      flex: 1;
      min-height: 0;
      object-fit: cover;
      display: block;
    }

    /*FOOTER_CSS*/

    @media (max-width: 600px) {
      .hero { padding: 2.5rem 1.5rem 2rem; }
      .hero h1 { font-size: 1.8rem; }
      .hero-image { height: 240px; }
      .option-card { width: 100%; max-width: 340px; }
    }
  </style>
</head>
<body>
  <div class="hero">
    <h1>EPISERVE<span>API Server</span></h1>
    <p>Access epidemiological datasets, simulation models, and model run results for infectious disease research and pandemic preparedness in Germany.</p>
    <div class="options">
      <a class="option-card" href="/ui/datasets">
        <h2>Web UI</h2>
        <p>Browse datasets and models, inspect model runs, and explore results interactively.</p>
        <span class="cta">Open the UI →</span>
      </a>
      <a class="option-card" href="/docs">
        <h2>REST API</h2>
        <p>Explore all endpoints and try requests directly in the browser via the API docs.</p>
        <span class="cta">Open API docs →</span>
      </a>
    </div>
  </div>

  <img class="hero-image" src="https://episerve.zib.de/img/hero.png" alt="">

  <!--FOOTER_HTML-->
</body>
</html>"""


@app.get("/health")
def health():
    return {"status": "ok"}


register_pages()
ui.run_with(
    app,
    title="EPISERVE",
    favicon="🔬",
    dark=False,
    storage_secret=require_secret("SESSION_SECRET", dev_default="episerve-dev-secret-change-me"),
)
