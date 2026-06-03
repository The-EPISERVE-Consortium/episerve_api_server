import os


class Settings:
    # lakeFS
    lakefs_url: str = os.environ.get("LAKEFS_URL", "https://lake-episerve.zib.de")
    lakefs_access_key: str = os.environ.get("LAKEFS_ACCESS_KEY", "")
    lakefs_secret_key: str = os.environ.get("LAKEFS_SECRET_KEY", "")
    lakefs_raw_repo: str = os.environ.get("LAKEFS_RAW_REPO", "data-raw")
    lakefs_processed_repo: str = os.environ.get("LAKEFS_PROCESSED_REPO", "data-processed")
    lakefs_model_runs_repo: str = os.environ.get("LAKEFS_MODEL_RUNS_REPO", "model-runs")
    lakefs_branch: str = os.environ.get("LAKEFS_BRANCH", "main")

    # CKAN
    ckan_url: str = os.environ.get("CKAN_URL") or "https://data.episerve.zib.de"
    ckan_api_token: str = os.environ.get("CKAN_API_TOKEN", "")

    # Prefect
    prefect_api_url: str = os.environ.get("PREFECT_API_URL", "")
    prefect_api_key: str = os.environ.get("PREFECT_API_KEY", "")
    prefect_model_runner_deployment: str = (
        os.environ.get("PREFECT_MODEL_RUNNER_DEPLOYMENT") or "model-pipeline/model-runner"
    )

    # DOIP server
    doip_url: str = os.environ.get("DOIP_URL") or "https://doip.episerve.zib.de"

    # Server
    port: int = int(os.environ.get("PORT", "8000"))


settings = Settings()
