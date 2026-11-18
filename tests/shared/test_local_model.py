from __future__ import annotations

import json

import httpx
import pytest

from shared import local_model
from shared.local_model import MAX_RESPONSE_CHARS, LocalModelError, OllamaJsonModel, validate_local_url

URL = "http://127.0.0.1:11434"


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
    ("http://localhost:11434", "http://localhost:11434"),
    ("http://[::1]:11434", "http://[::1]:11434"),
])
def test_only_local_http_urls_are_accepted(url, expected):
    assert validate_local_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:11434", "http://192.168.1.20:11434", "http://ollama.example.com",
    "http://127.0.0.1.example.com:11434", "file:///tmp/ollama", "127.0.0.1:11434",
])
def test_a_url_outside_the_machine_is_refused(url):
    with pytest.raises(ValueError, match="must be local"):
        validate_local_url(url)
    with pytest.raises(ValueError):
        OllamaJsonModel(url, "modelo")


@pytest.fixture
def ollama(monkeypatch):
    """Answers every request with the handler set by the test; records what was sent."""
    state = {"requests": [], "clients": [], "handler": None}

    def handle(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    real_client = httpx.Client

    def client(**kwargs):
        state["clients"].append(kwargs)
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(local_model.httpx, "Client", client)
    return state


def answer(content) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_a_json_object_answer_is_returned_and_the_request_has_no_tools(ollama):
    ollama["handler"] = lambda request: answer('{"total": "100.00"}')
    value = OllamaJsonModel(URL + "/", "modelo-local", timeout_seconds=9).complete_json("sistema", "usuario")
    assert value == {"total": "100.00"}
    [request] = ollama["requests"]
    assert str(request.url) == URL + "/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "modelo-local" and body["format"] == "json" and body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert "tools" not in body
    # No proxy from the environment ever sees the text sent to the model.
    assert ollama["clients"] == [{"timeout": 9, "trust_env": False}]


def test_ollama_not_running_is_reported_without_the_request_text(ollama):
    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    ollama["handler"] = refused
    with pytest.raises(LocalModelError, match="is Ollama running") as error:
        OllamaJsonModel(URL, "modelo").complete_json("sistema", "texto privado de la factura")
    assert "texto privado" not in str(error.value)


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"error": "model crashed"}),
    httpx.Response(404, json={"error": "model not found"}),
    httpx.Response(200, content=b"<html>no es json</html>"),
    httpx.Response(200, json={"message": "texto en lugar de objeto"}),
])
def test_a_failed_or_malformed_http_answer_stops(ollama, response):
    ollama["handler"] = lambda request: response
    with pytest.raises(LocalModelError, match="request failed"):
        OllamaJsonModel(URL, "modelo").complete_json("s", "u")


@pytest.mark.parametrize("body", [
    {"message": {"content": None}},
    {"message": {}},
    {},
    {"message": {"content": 7}},
])
def test_an_answer_without_text_content_is_invalid(ollama, body):
    ollama["handler"] = lambda request: httpx.Response(200, json=body)
    with pytest.raises(LocalModelError, match="invalid response"):
        OllamaJsonModel(URL, "modelo").complete_json("s", "u")


def test_an_oversized_answer_is_refused_before_parsing(ollama):
    ollama["handler"] = lambda request: answer('{"a": "' + "x" * MAX_RESPONSE_CHARS + '"}')
    with pytest.raises(LocalModelError, match="invalid response"):
        OllamaJsonModel(URL, "modelo").complete_json("s", "u")


def test_text_that_is_not_json_is_refused(ollama):
    ollama["handler"] = lambda request: answer("El total es 100")
    with pytest.raises(LocalModelError, match="did not return JSON"):
        OllamaJsonModel(URL, "modelo").complete_json("s", "u")


@pytest.mark.parametrize("content", ['["a", "b"]', '"texto"', "42", "null"])
def test_json_that_is_not_an_object_is_refused(ollama, content):
    ollama["handler"] = lambda request: answer(content)
    with pytest.raises(LocalModelError, match="JSON object"):
        OllamaJsonModel(URL, "modelo").complete_json("s", "u")
