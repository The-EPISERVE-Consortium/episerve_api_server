"""Register the WebUI routes; rendering belongs to each feature module."""

from nicegui import ui

from app.ui.auth_pages import register_auth_pages
from app.ui.datasets_page import register_datasets_page
from app.ui.models_page import register_models_page
from app.ui.model_runs_page import register_model_runs_page
from app.ui.blackboard_page import register_blackboard_page
from app.ui.trigger_page import register_trigger_page
from app.ui.workflow.pages import register_workflow_pages
from app.ui.benchmarks_page import register_benchmarks_page
from app.ui.leaderboard_page import register_leaderboard_page


def register_pages() -> None:
    """Register the UI entry point and all feature routes."""
    @ui.page("/ui")
    def ui_home():
        """Redirect the UI entry point to the dataset catalog."""
        ui.navigate.to("/ui/datasets")

    register_auth_pages()
    register_datasets_page()
    register_models_page()
    register_model_runs_page()
    register_blackboard_page()
    register_trigger_page()
    register_workflow_pages()
    register_benchmarks_page()
    register_leaderboard_page()
