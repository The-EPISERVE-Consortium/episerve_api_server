from fastapi import FastAPI
from app.routers import datasets, models, model_runs

app = FastAPI(
    title="EPISERVE API",
    description="REST API for browsing datasets and models, and triggering model runs on the EPISERVE platform.",
    version="0.1.0",
)

app.include_router(datasets.router)
app.include_router(models.router)
app.include_router(model_runs.router)


@app.get("/health")
def health():
    return {"status": "ok"}
