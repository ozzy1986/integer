"""Webhook authentication and validation, payment semantics."""

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.signing import SIGNATURE_HEADER, sign
from tests.integration.conftest import PAYMENTS_SECRET, PROVIDER_SECRETS, Harness


class TestProviderWebhookSecurity:
    async def test_missing_or_wrong_signature_is_401(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        attempt = await harness.live_attempt(task_id)
        body = json.dumps({"attempt_id": attempt["id"], "status": "completed", "result_url": "https://x.example/r"})

        no_signature = await harness.client.post("/webhooks/providers/alpha", content=body)
        wrong = await harness.client.post(
            "/webhooks/providers/alpha",
            content=body,
            headers={SIGNATURE_HEADER: sign("x" * 40, body.encode())},
        )

        assert (no_signature.status_code, wrong.status_code) == (401, 401)
        assert (await harness.task(task_id))["status"] == "in_progress"

    async def test_one_provider_cannot_finish_another_providers_attempt(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        alpha = await harness.live_attempt(task_id)

        response = await harness.complete("beta", alpha["id"])  # validly signed by beta

        assert response.status_code == 404
        assert (await harness.task(task_id))["status"] == "in_progress"

    async def test_unknown_provider_is_404(self, harness: Harness) -> None:
        response = await harness.post_signed("/webhooks/providers/gamma", {"x": 1}, PROVIDER_SECRETS["alpha"])
        assert response.status_code == 404

    async def test_unknown_attempt_is_404(self, harness: Harness) -> None:
        assert (await harness.complete("alpha", str(uuid.uuid4()))).status_code == 404

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": "completed"},  # no result_url
            {"status": "failed"},  # no error_type
            {"status": "failed", "error_type": "cosmic-rays"},
            {"status": "exploded"},
            {"status": "completed", "result_url": ""},
            {"status": "completed", "result_url": "https://x.example/\x00"},
        ],
    )
    async def test_malformed_payload_is_422(self, harness: Harness, payload: dict[str, str]) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        attempt = await harness.live_attempt(task_id)
        assert (await harness.webhook("alpha", attempt["id"], **payload)).status_code == 422

    async def test_a_result_link_is_not_required_to_be_http(self, harness: Harness) -> None:
        # Rejecting a signed s3:// result with 422 would make the provider stop retrying: the job is done,
        # but the watchdog would later refund it as if it had failed.
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        attempt = await harness.live_attempt(task_id)

        response = await harness.complete("alpha", attempt["id"], url="s3://results/r.png")

        assert response.json()["outcome"] == "task_succeeded"
        assert (await harness.task(task_id))["result_url"] == "s3://results/r.png"

    async def test_invalid_json_with_valid_signature_is_422(self, harness: Harness) -> None:
        body = b"{not json"
        response = await harness.client.post(
            "/webhooks/providers/alpha",
            content=body,
            headers={SIGNATURE_HEADER: sign(PROVIDER_SECRETS["alpha"], body)},
        )
        assert response.status_code == 422

    async def test_oversized_body_is_413_before_any_processing(self, harness: Harness) -> None:
        body = b"x" * (65 * 1024)
        response = await harness.client.post(
            "/webhooks/payments", content=body, headers={SIGNATURE_HEADER: sign(PAYMENTS_SECRET, body)}
        )
        assert response.status_code == 413


class TestWebhookByExternalId:
    """A provider knows its job by the external_id from its 202: a webhook may name the attempt by that alone."""

    @staticmethod
    async def post(harness: Harness, provider: str, **payload: Any) -> httpx.Response:
        return await harness.post_signed(f"/webhooks/providers/{provider}", payload, PROVIDER_SECRETS[provider])

    async def test_completed_by_external_id_finishes_the_task(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        alpha = await harness.live_attempt(task_id)

        response = await self.post(
            harness, "alpha", external_id=alpha["external_id"], status="completed", result_url="https://cdn.example/r"
        )

        assert response.json()["outcome"] == "task_succeeded"
        assert (await harness.task(task_id))["result_url"] == "https://cdn.example/r"

    async def test_ten_concurrent_failures_by_external_id_on_the_last_leg_refund_once(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        await harness.fail("alpha", (await harness.live_attempt(task_id))["id"])
        await harness.dispatch()
        beta = await harness.live_attempt(task_id)

        responses = await asyncio.gather(
            *(
                self.post(harness, "beta", external_id=beta["external_id"], status="failed", error_type="technical")
                for _ in range(10)
            )
        )

        assert sorted(r.json()["outcome"] for r in responses) == ["ignored"] * 9 + ["task_failed"]
        assert await harness.ledger_kinds(user_id) == ["payment", "charge", "refund"]

    async def test_only_the_senders_own_attempts_are_found(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        alpha = await harness.live_attempt(task_id)

        response = await self.post(
            harness, "beta", external_id=alpha["external_id"], status="completed", result_url="https://x.example/r"
        )

        assert response.status_code == 404
        assert (await harness.task(task_id))["status"] == "in_progress"

    async def test_unknown_external_id_is_404(self, harness: Harness) -> None:
        response = await self.post(
            harness, "alpha", external_id="never-issued", status="completed", result_url="https://x.example/r"
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("ids", [{}, {"external_id": ""}, {"external_id": "a\x00b"}])
    async def test_a_webhook_that_names_no_attempt_is_422(self, harness: Harness, ids: dict[str, str]) -> None:
        response = await self.post(harness, "alpha", status="completed", result_url="https://x.example/r", **ids)
        assert response.status_code == 422


class TestPayments:
    async def test_payment_signature_is_required(self, harness: Harness) -> None:
        body = json.dumps({"payment_id": "p", "user_id": 1, "tokens": 5, "status": "succeeded"})
        response = await harness.client.post("/webhooks/payments", content=body)
        assert response.status_code == 401

    async def test_provider_secret_cannot_sign_payments(self, harness: Harness) -> None:
        payment = {"payment_id": f"p-{uuid.uuid4()}", "user_id": 1, "tokens": 5, "status": "succeeded"}
        response = await harness.post_signed("/webhooks/payments", payment, PROVIDER_SECRETS["alpha"])
        assert response.status_code == 401

    async def test_only_succeeded_payments_are_credited(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=0)
        payment = {"payment_id": f"p-{uuid.uuid4()}", "user_id": user_id, "tokens": 5}

        for state in ("pending", "failed", "canceled"):
            response = await harness.post_signed("/webhooks/payments", {**payment, "status": state}, PAYMENTS_SECRET)
            assert response.json() == {"credited": False}
        assert (await harness.balance(user_id))["balance"] == 0

        response = await harness.post_signed("/webhooks/payments", {**payment, "status": "succeeded"}, PAYMENTS_SECRET)
        assert response.json() == {"credited": True}
        assert (await harness.balance(user_id))["balance"] == 5

    @pytest.mark.parametrize(
        ("sent", "stored", "credited"),
        [
            ("paid", "succeeded", True),
            ("SUCCESS", "succeeded", True),
            ("completed", "succeeded", True),
            ("processing", "pending", False),
            ("cancelled", "canceled", False),
            ("declined", "failed", False),
        ],
    )
    async def test_common_status_spellings_are_understood(
        self, harness: Harness, sent: str, stored: str, credited: bool
    ) -> None:
        # The task names no statuses; a success under another spelling must not be rejected and lost.
        user_id = await harness.new_user(tokens=0)
        payment = {"payment_id": f"p-{uuid.uuid4()}", "user_id": user_id, "tokens": 5, "status": sent}

        response = await harness.post_signed("/webhooks/payments", payment, PAYMENTS_SECRET)

        assert response.json() == {"credited": credited}
        async with harness.services.pool.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment["payment_id"]) == stored

    async def test_late_status_after_success_does_not_undo_the_credit(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=0)
        payment = {"payment_id": f"p-{uuid.uuid4()}", "user_id": user_id, "tokens": 5}
        await harness.post_signed("/webhooks/payments", {**payment, "status": "succeeded"}, PAYMENTS_SECRET)
        await harness.post_signed("/webhooks/payments", {**payment, "status": "pending"}, PAYMENTS_SECRET)
        assert (await harness.balance(user_id))["balance"] == 5

    async def test_same_payment_id_with_different_amount_or_user_is_409(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=0)
        payment_id = f"p-{uuid.uuid4()}"
        assert (await harness.pay(user_id, 5, payment_id=payment_id)).status_code == 200

        assert (await harness.pay(user_id, 500, payment_id=payment_id)).status_code == 409
        assert (await harness.pay(user_id + 1, 5, payment_id=payment_id)).status_code == 409
        assert (await harness.balance(user_id))["balance"] == 5

    async def test_concurrent_different_payments_all_count(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=0)
        await asyncio.gather(*(harness.pay(user_id, 1) for _ in range(15)))
        assert (await harness.balance(user_id))["balance"] == 15

    @pytest.mark.parametrize(
        "shape",
        [
            lambda unique: unique,  # a number, as many payment systems send it
            lambda unique: f"pay/2026#{unique}",
            lambda unique: f"заказ {unique}",
            lambda unique: f"{{{uuid.UUID(int=unique)}}}",
        ],
        ids=["number", "slash-and-hash", "spaces-and-cyrillic", "braces"],
    )
    async def test_a_payment_id_of_any_shape_is_credited_once(
        self, harness: Harness, shape: Callable[[int], object]
    ) -> None:
        # The task fixes no format for payment_id. Rejecting one with 422 would make the provider stop
        # retrying, and the user's money would never arrive.
        user_id = await harness.new_user(tokens=0)
        payment = {"payment_id": shape(user_id), "user_id": user_id, "tokens": 5, "status": "succeeded"}

        responses = await asyncio.gather(
            *(harness.post_signed("/webhooks/payments", payment, PAYMENTS_SECRET) for _ in range(20))
        )

        assert sorted(r.json()["credited"] for r in responses) == [False] * 19 + [True]
        balance = await harness.balance(user_id)
        assert (balance["balance"], balance["ledger"][0]["payment_id"]) == (5, str(payment["payment_id"]))

    async def test_a_number_and_its_text_are_the_same_payment(self, harness: Harness) -> None:
        user_id = await harness.new_user(tokens=0)
        for payment_id in (user_id, str(user_id)):
            payment = {"payment_id": payment_id, "user_id": user_id, "tokens": 5, "status": "succeeded"}
            assert (await harness.post_signed("/webhooks/payments", payment, PAYMENTS_SECRET)).status_code == 200
        assert (await harness.balance(user_id))["balance"] == 5

    @pytest.mark.parametrize(
        "change",
        [
            {"tokens": 0},
            {"tokens": -5},
            {"tokens": 10**12},
            {"payment_id": ""},
            {"payment_id": "a\x00b"},  # PostgreSQL text cannot hold NUL
            {"payment_id": "x" * 129},
            {"payment_id": True},
            {"payment_id": 1.5},
            {"user_id": 0},
            {"status": "maybe"},
        ],
    )
    async def test_invalid_payment_is_422(self, harness: Harness, change: dict[str, object]) -> None:
        payment = {
            "payment_id": f"p-{uuid.uuid4()}",
            "user_id": 1,
            "tokens": 5,
            "status": "succeeded",
            **change,
        }
        assert (await harness.post_signed("/webhooks/payments", payment, PAYMENTS_SECRET)).status_code == 422
