"""The local panel's server: loopback, Origin and CSRF checks and visible errors."""

from __future__ import annotations

import httpx
import pytest

from monthly_close.extraction import extract
from monthly_close.store import Store
from builders import FakeExtractionModel


@pytest.fixture
def panel(tmp_path, start_panel):
    store = Store(tmp_path / "test.sqlite3")
    url = start_panel(store)
    with httpx.Client(base_url=url, trust_env=False) as client:
        yield store, client, url


def test_panel_requires_origin_csrf_and_loopback_host(panel, preferences):
    store, client, url = panel
    state = client.get("/api/state").json()
    assert state["configured"] is False
    assert client.get("/?tab=year&month=2026-10&currency=COP").status_code == 200
    assert client.post("/api/config", json=preferences.to_dict()).status_code == 403
    headers = {"Origin": "https://evil.test", "X-CSRF-Token": state["csrf"]}
    assert client.post("/api/config", headers=headers, json=preferences.to_dict()).status_code == 403
    headers["Origin"] = url
    assert client.post("/api/config", headers=headers, json=preferences.to_dict()).status_code == 200
    assert store.preferences().accounts == ["Tarjeta A"]
    assert client.get("/api/state", headers={"Host": "evil.test"}).status_code == 403


def test_rejected_requests_with_a_body_always_get_their_403(panel):
    # The body of a rejected request is read and dropped: otherwise Windows can reset the
    # connection and the client sees a network error instead of the refusal.
    _, client, _ = panel
    csrf = client.get("/api/state").json()["csrf"]
    body = {"relleno": "x" * 20000}
    headers = {"Origin": "https://evil.test", "X-CSRF-Token": csrf}
    statuses = [client.post("/api/config", headers=headers, json=body).status_code for _ in range(300)]
    assert statuses == [403] * 300


def test_panel_validation_errors_are_visible(panel, preferences):
    _, client, url = panel
    state = client.get("/api/state").json()
    headers = {"Origin": url, "X-CSRF-Token": state["csrf"]}
    response = client.post("/api/decision", headers=headers, json={"id": "missing", "decision": "personal"})
    assert response.status_code == 400
    assert response.json()["error"]
    response = client.post("/api/config", headers=headers, json={})
    assert response.status_code == 400


def test_decision_api_requires_decimal_text_rate_and_saves_estimate(panel, preferences, mail):
    store, client, url = panel
    store.configure(preferences.to_dict())
    event = extract(mail, preferences, FakeExtractionModel())[0]
    store.import_message(mail.id, [event])
    headers = {"Origin": url, "X-CSRF-Token": client.get("/api/state").json()["csrf"]}
    payload = {"id": event["id"], "decision": "personal", "currency": "USD"}
    for rate in (None, 4200.5):
        response = client.post(
            "/api/decision", headers=headers, json={**payload, "exchange_rate": rate},
        )
        assert response.status_code == 400
        assert store.state()["transactions"][0]["decision"] is None
    response = client.post(
        "/api/decision", headers=headers, json={**payload, "exchange_rate": "4200.5"},
    )
    assert response.status_code == 200
    row = client.get("/api/state").json()["transactions"][0]
    assert row["currency"] == "USD"
    assert row["exchange_rate"] == "4200.5"
    assert row["cop_amount_minor"] == 518576928
