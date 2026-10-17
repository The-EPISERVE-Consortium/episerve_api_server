"""The curl command the API box copies to the clipboard."""

from app.ui import pages


def test_get_command_carries_the_token():
    assert pages._api_command("https://api.example/models", "GET", "T") == (
        'curl -H "Authorization: Bearer T" https://api.example/models')


def test_post_command_sends_a_json_file():
    assert pages._api_command("https://api.example/model-runs", "POST", "T") == (
        'curl -X POST -H "Authorization: Bearer T" -H "Content-Type: application/json" '
        "-d @run.json https://api.example/model-runs")
