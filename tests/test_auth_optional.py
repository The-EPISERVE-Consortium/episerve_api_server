"""optional_auth: True only for a valid daily token, never raises."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import _daily_token, optional_auth

app = FastAPI()


@app.get("/probe")
def probe(authed: bool = Depends(optional_auth)):
    return {"authed": authed}


client = TestClient(app)


def test_optional_auth():
    assert client.get("/probe").json() == {"authed": False}
    assert client.get("/probe", headers={"Authorization": "Bearer wrong"}).json() == {"authed": False}
    assert client.get("/probe", headers={"Authorization": f"Bearer {_daily_token()}"}).json() == {"authed": True}
