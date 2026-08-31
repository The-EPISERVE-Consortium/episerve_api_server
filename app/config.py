import os


class Settings:
    # lakeFS
    lakefs_url: str = os.environ.get("LAKEFS_URL", "https://lake-episerve.zib.de")
    lakefs_access_key: str = os.environ.get("LAKEFS_ACCESS_KEY", "")
    lakefs_secret_key: str = os.environ.get("LAKEFS_SECRET_KEY", "")
    lakefs_raw_repo: str = os.environ.get("LAKEFS_RAW_REPO", "data-raw")
    lakefs_processed_repo: str = os.environ.get("LAKEFS_PROCESSED_REPO", "data-processed")
    lakefs_model_runs_repo: str = os.environ.get("LAKEFS_MODEL_RUNS_REPO", "model-runs")
    lakefs_models_repo: str = os.environ.get("LAKEFS_MODELS_REPO", "models")
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
    prefect_orchestrator_deployment: str = (
        os.environ.get("PREFECT_ORCHESTRATOR_DEPLOYMENT")
        or "blackboard-orchestrator/blackboard-orchestrator"
    )
    # Public UI base for linking to a flow run: <base>/runs/flow-run/<id>.
    # NOT derived from PREFECT_API_URL -- in-cluster that's an internal
    # service address, and this deployment serves its UI under "/v2".
    prefect_ui_url: str = (
        os.environ.get("PREFECT_UI_URL") or "https://prefect.episerve.zib.de/v2"
    ).rstrip("/")

    # DOIP server
    doip_url: str = os.environ.get("DOIP_URL") or "https://doip.episerve.zib.de"

    # Blackboard (agent_blackboard.task_runs -- same MariaDB instance as
    # dataset-downloader's episerve-raw-data, separate DB + scoped user)
    mariadb_host: str = os.environ.get("MARIADB_HOST", "")
    blackboard_db: str = os.environ.get("BLACKBOARD_DB", "agent_blackboard")
    blackboard_user: str = os.environ.get("BLACKBOARD_USER", "")
    blackboard_password: str = os.environ.get("BLACKBOARD_PASSWORD", "")

    # Server
    port: int = int(os.environ.get("PORT", "8000"))

    # Auth
    auth_master_secret: str = os.environ.get("AUTH_MASTER_SECRET", "episerve-dev-secret-change-me")
    auth_username: str = os.environ.get("AUTH_USERNAME", "admin")
    auth_password: str = os.environ.get("AUTH_PASSWORD", "")


settings = Settings()
