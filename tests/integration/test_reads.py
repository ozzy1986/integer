"""Read endpoints: task history, balance with ledger, catalog, health."""

import json
import uuid
from collections.abc import Iterator

import pytest

from tests.integration.conftest import Harness


async def test_task_history_lists_attempts_and_events_in_order(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    task_id = await harness.start_task(user_id)
    await harness.fail("alpha", (await harness.live_attempt(task_id))["id"], error="moderation")
    await harness.dispatch()

    task = await harness.task(task_id)

    assert [a["number"] for a in task["attempts"]] == [1, 2]
    assert task["current_leg"] == {"attempt": 2, "priority": 2, "provider": "beta", "status": "sent"}
    first, second = task["attempts"]
    assert (first["provider"], first["error"], first["reason"]) == ("alpha", "moderation", "webhook")
    assert first["created_at"] <= first["sent_at"] <= first["finished_at"]
    assert second["sent_at"] < second["deadline_at"]  # the watchdog's clock, shown in the UI
    kinds = [event["kind"] for event in task["events"]]
    assert kinds == [
        "created",
        "charged",
        "attempt_sent",
        "attempt_failed",
        "attempt_created",
        "attempt_sent",
    ]


async def test_unknown_task_is_404(harness: Harness) -> None:
    assert (await harness.client.get(f"/tasks/{uuid.uuid4()}")).status_code == 404
    assert (await harness.client.get("/tasks/not-a-uuid")).status_code == 422


async def test_balance_shows_latest_ledger_rows_first_with_running_balance(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    await harness.generate(user_id)

    balance = await harness.balance(user_id)

    assert balance["balance"] == 8
    rows = [(row["kind"], row["amount"], row["balance_after"]) for row in balance["ledger"]]
    assert rows == [("charge", -2, 8), ("payment", 10, 10)]


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2), (500, None)])
async def test_balance_ledger_limit(harness: Harness, limit: int, expected: int | None) -> None:
    user_id = await harness.new_user(tokens=10)
    await harness.generate(user_id)
    response = await harness.client.get(f"/users/{user_id}/balance", params={"limit": limit})
    if expected is None:
        assert response.status_code == 422
    else:
        assert len(response.json()["ledger"]) == expected


@pytest.mark.parametrize("user_id", ["0", "-5", str(2**63), "99999999999999999999"])
@pytest.mark.parametrize("what", ["balance", "tasks"])
async def test_impossible_user_ids_in_the_path_are_422(harness: Harness, user_id: str, what: str) -> None:
    assert (await harness.client.get(f"/users/{user_id}/{what}")).status_code == 422


async def test_user_task_list_newest_first(harness: Harness) -> None:
    user_id = await harness.new_user(tokens=10)
    first = (await harness.generate(user_id)).json()["task_id"]
    second = (await harness.generate(user_id)).json()["task_id"]

    response = await harness.client.get(f"/users/{user_id}/tasks")

    assert [task["id"] for task in response.json()] == [second, first]


async def test_models_catalog(harness: Harness) -> None:
    models = (await harness.client.get("/models")).json()
    assert [(m["slug"], m["price"], [leg["provider"] for leg in m["legs"]]) for m in models] == [
        ("image-fast", 2, ["alpha", "beta"]),
        ("video-long", 25, ["beta", "alpha"]),
    ]


async def test_health_reports_database_and_invariants(harness: Harness) -> None:
    assert (await harness.client.get("/health")).json() == {"status": "ok"}
    assert (await harness.client.get("/health/invariants")).json() == {"balance_drift": 0}


async def test_webhooks_are_documented_with_their_body_and_signature(harness: Harness) -> None:
    # The body is read raw for the signature check, so FastAPI cannot infer it: it is described by hand.
    spec = (await harness.client.get("/openapi.json")).json()
    expected = {
        "/webhooks/payments": {"payment_id", "user_id", "tokens", "status"},
        "/webhooks/providers/{name}": {"attempt_id", "external_id", "status", "result_url", "error_type"},
    }
    for path, fields in expected.items():
        operation = spec["paths"][path]["post"]
        assert set(operation["requestBody"]["content"]["application/json"]["schema"]["properties"]) == fields
        assert "X-Signature" in [p["name"] for p in operation["parameters"] if p["in"] == "header"]
        assert {"200", "401", "422"} <= set(operation["responses"])
    assert "#/$defs/" not in json.dumps(spec)  # every reference resolves inside the document
    assert not {"gt", "ge", "lt", "le"} & set(_keywords(spec))  # JSON Schema says exclusiveMinimum etc.
    assert "404" in spec["paths"]["/tasks/{task_id}"]["get"]["responses"]


def _keywords(node: object) -> Iterator[str]:
    """Every key in the document except property names, which are data rather than keywords."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            children = value.values() if key == "properties" and isinstance(value, dict) else [value]
            for child in children:
                yield from _keywords(child)
    elif isinstance(node, list):
        for item in node:
            yield from _keywords(item)


async def test_security_headers_are_set(harness: Harness) -> None:
    headers = (await harness.client.get("/health")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in headers["content-security-policy"]


async def test_demo_endpoints_are_off_by_default(harness: Harness) -> None:
    response = await harness.client.post("/demo/topup", json={"user_id": 1, "tokens": 5})
    assert response.status_code in (404, 405)


class TestWebUi:
    async def test_index_page_is_served_with_a_strict_csp(self, harness: Harness) -> None:
        response = await harness.client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<title>Integer" in response.text
        csp = response.headers["content-security-policy"]
        assert "script-src 'self';" in csp
        assert "unsafe-inline" not in csp

    @pytest.mark.parametrize(
        "path", ["/js/app.js", "/js/api.js", "/css/app.css", "/fonts/onest-cyrillic-wght-normal.woff2", "/favicon.svg"]
    )
    async def test_assets_are_served(self, harness: Harness, path: str) -> None:
        assert (await harness.client.get(path)).status_code == 200

    async def test_api_routes_are_not_shadowed(self, harness: Harness) -> None:
        assert (await harness.client.get("/docs")).status_code == 200
        assert (await harness.client.get("/models")).headers["content-type"] == "application/json"
        assert (await harness.client.get("/no-such-page")).status_code == 404

    async def test_ui_never_uses_inner_html(self) -> None:
        from app.main import STATIC_DIR

        for script in (STATIC_DIR / "js").glob("*.js"):
            source = script.read_text(encoding="utf-8")
            assert "innerHTML" not in source, script.name
            assert "insertAdjacentHTML" not in source, script.name


async def test_provider_client_ignores_proxy_environment(harness: Harness) -> None:
    # Signed webhooks and provider calls must never be routed through an HTTP(S)_PROXY from the environment.
    assert harness.services.http.trust_env is False
