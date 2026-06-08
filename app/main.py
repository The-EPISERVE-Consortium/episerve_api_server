import os
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
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


@app.get("/")
def root():
    return RedirectResponse(url="/ui")


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
