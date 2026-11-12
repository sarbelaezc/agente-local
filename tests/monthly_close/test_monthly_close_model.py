import json

import httpx
import pytest

from mail_invoices.local_model import LocalModelError
from monthly_close.model import SCHEMA, OllamaJsonModel

USER = json.dumps({
    "categories": ["Compras"], "accounts": ["Tarjeta A"], "excluded_accounts": [],
    "default_currency": "COP",
    "untrusted_email": "Compra del 01/03/2026 por $12.345,67 y COP 800.00.",
})

def test_model_requires_boolean_and_all_event_fields_without_proxy(monkeypatch):
    seen = []
    real_client = httpx.Client

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"message": {"content": '{"events":[]}'}})

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    assert OllamaJsonModel("http://127.0.0.1:11434", "test").complete_json("system", USER) == {
        "events": [],
    }
    schema = seen[0]["format"]
    event = schema["properties"]["events"]["items"]
    assert event["properties"]["savings"] == {"type": "boolean"}
    assert set(event["required"]) == set(event["properties"])
    assert "unknown" in event["properties"]["kind"]["enum"]
    assert event["properties"]["category"]["enum"] == ["Compras"]
    assert event["properties"]["account"]["enum"] == ["Tarjeta A", "Desconocida"]
    assert event["properties"]["currency"]["enum"] == ["COP"]
    assert event["properties"]["date_text"]["enum"] == ["", "01/03/2026"]
    assert event["properties"]["amount_text"]["enum"] == ["$12.345,67", "COP 800.00"]
    assert "enum" not in SCHEMA["properties"]["events"]["items"]["properties"]["amount_text"]
    assert "enum" not in SCHEMA["properties"]["events"]["items"]["properties"]["category"]
    assert "tools" not in seen[0]


@pytest.mark.parametrize("payload", [
    {}, {"message": None}, {"message": {"content": "not-json"}},
    {"message": {"content": "[]"}}, {"message": {"content": "x" * 20001}},
])
def test_model_rejects_bad_envelopes(monkeypatch, payload):
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kwargs,
    ))
    with pytest.raises(LocalModelError):
        OllamaJsonModel("http://127.0.0.1:11434", "test").complete_json("system", USER)


@pytest.mark.parametrize("source,expected", [
    ("Total $ 1.234,56; aporte COP$800.00.", ["$ 1.234,56", "COP$800.00"]),
    ("Total €12,50 y £ 50.", ["£ 50", "€12,50"]),
    ("Total $1 234,56.", ["$1 234,56"]),
    ("Importe 1234,56. Fecha 01/03/2026. Cuenta 123456.", None),
])
def test_amount_constraints_only_use_literal_currency_marked_amounts(
    monkeypatch, source, expected,
):
    seen = []
    real_client = httpx.Client

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"events":[]}'}})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    context = json.loads(USER)
    context["untrusted_email"] = source
    OllamaJsonModel("http://127.0.0.1:11434", "test").complete_json(
        "system", json.dumps(context),
    )
    properties = seen[0]["format"]["properties"]["events"]["items"]["properties"]
    assert properties["amount_text"].get("enum") == expected
