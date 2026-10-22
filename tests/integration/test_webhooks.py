"""Webhook authentication and validation, payment semantics."""

import asyncio
import json
import uuid

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
            {"status": "completed", "result_url": "ftp://x.example/r"},
        ],
    )
    async def test_malformed_payload_is_422(self, harness: Harness, payload: dict[str, str]) -> None:
        user_id = await harness.new_user(tokens=10)
        task_id = await harness.start_task(user_id)
        attempt = await harness.live_attempt(task_id)
        assert (await harness.webhook("alpha", attempt["id"], **payload)).status_code == 422

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
        "change",
        [
            {"tokens": 0},
            {"tokens": -5},
            {"tokens": 10**12},
            {"payment_id": ""},
            {"payment_id": "a b"},
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
