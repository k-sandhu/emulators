"""Adversarial HTTP regressions for authorization, malformed forms and state consistency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from tests.providers.stripe.conftest import get, post


@pytest.fixture
async def write_paths(client: httpx.AsyncClient, stripe_hub: EmulatorHub) -> list[str]:
    ctx = stripe_hub.context("stripe")
    ctx.webhooks.mode = "manual"
    ids = {
        name: ctx.store.collection(name).values()[0]["id"]
        for name in (
            "customers",
            "payment_methods",
            "payment_intents",
            "subscriptions",
            "subscription_items",
            "products",
            "prices",
            "charges",
            "refunds",
            "disputes",
            "invoices",
            "payouts",
        )
    }
    for name, data in {
        "setup_intents": {},
        "invoiceitems": {"customer": ids["customers"], "amount": 100, "currency": "usd"},
        "checkout/sessions": {"mode": "setup"},
        "webhook_endpoints": {"url": "https://consumer.example/hook", "enabled_events[]": "*"},
        "test_helpers/test_clocks": {"frozen_time": 1792108800},
    }.items():
        ids[name] = (await post(client, name, data))["id"]
    source = await post(client, f"customers/{ids['customers']}/sources", {"source": "tok_visa"})
    tax = await post(client, f"customers/{ids['customers']}/tax_ids", {"type": "ca_bn", "value": "123456789"})
    adjustment = await post(
        client,
        f"customers/{ids['customers']}/balance_transactions",
        {
            "amount": -100,
            "currency": "usd",
        },
    )
    paths = [*ids, "tokens", "files", "billing_portal/sessions", "invoices/create_preview"]
    paths += [f"{name}/{record_id}" for name, record_id in ids.items()]
    for name, operations in {
        "payment_intents": ("confirm", "capture", "cancel", "increment_authorization"),
        "setup_intents": ("confirm", "cancel"),
        "payment_methods": ("attach", "detach"),
        "subscriptions": ("resume",),
        "invoices": ("finalize", "pay", "send", "void", "mark_uncollectible"),
        "disputes": ("close",),
        "payouts": ("cancel",),
        "checkout/sessions": ("expire",),
        "test_helpers/test_clocks": ("advance",),
    }.items():
        paths += [f"{name}/{ids[name]}/{operation}" for operation in operations]
    for name, record in (("sources", source), ("tax_ids", tax), ("balance_transactions", adjustment)):
        paths += [
            f"customers/{ids['customers']}/{name}",
            f"customers/{ids['customers']}/{name}/{record['id']}",
        ]
    return paths


@pytest.mark.parametrize(
    "payload,content_type",
    [
        ("expand[0][unexpected]=x", "application/x-www-form-urlencoded"),
        ('{"unexpected": []}', "application/json"),
        ("plain text", "text/plain"),
        (b"\xff", "application/x-www-form-urlencoded"),
        (b"name=%ff", "application/x-www-form-urlencoded"),
    ],
)
async def test_every_write_rejects_malformed_input_without_mutation(
    client: httpx.AsyncClient,
    stripe_hub: EmulatorHub,
    write_paths: list[str],
    payload: str | bytes,
    content_type: str,
) -> None:
    ctx = stripe_hub.context("stripe")
    before = ctx.store.snapshot()
    for path in write_paths:
        response = await client.post("/v1/" + path, content=payload, headers={"Content-Type": content_type})
        assert response.status_code == 400, (path, response.text)
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert response.headers["request-id"].startswith("req_")
    assert ctx.store.snapshot() == before


@pytest.mark.parametrize(
    "data",
    [
        {"payment_method[bad]": "x"},
        {"capture_method[bad]": "x"},
        {"amount": "9" * 5000},
        {"currency": "not-a-currency"},
        {"confirmation_method": "invalid"},
        {"payment_method_types[0][bad]": "x"},
    ],
)
async def test_invalid_intent_parameter_types_and_enums(
    client: httpx.AsyncClient, data: dict[str, str]
) -> None:
    response = await client.post("/v1/payment_intents", data={"amount": "100", "currency": "usd", **data})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "path,data",
    [
        ("products", {"name": "Audit", "default_price_data": "invalid"}),
        ("invoiceitems", {"pricing": "invalid"}),
        ("subscriptions", {"trial_settings[end_behavior]": "invalid"}),
        ("checkout/sessions", {"mode[bad]": "invalid"}),
        ("customers/search", {"query[bad]": "invalid"}),
    ],
)
async def test_nested_parameter_shapes(client: httpx.AsyncClient, path: str, data: dict[str, str]) -> None:
    response = await client.request("GET" if path.endswith("search") else "POST", "/v1/" + path, params=data)
    assert response.status_code == 400


async def test_publishable_key_cannot_list_or_modify_merchant_objects(client: httpx.AsyncClient) -> None:
    key = (await client.get("/_emulator/info")).json()["credentials"]["publishable_key"]
    headers = {"Authorization": "Bearer " + key}
    customer = await post(client, "customers")
    intent = await post(client, "payment_intents", {"amount": 100, "currency": "usd"})
    setup = await post(client, "setup_intents")
    for resource in ("payment_intents", "setup_intents"):
        for suffix in ("", "/search"):
            response = await client.get(
                "/v1/" + resource + suffix, params={"client_secret": "anything"}, headers=headers
            )
            assert response.status_code == 403
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": "tok_visa"}, headers=headers)
    for path, data in (
        (f"payment_methods/{pm['id']}", {"metadata[stolen]": "yes"}),
        (f"payment_methods/{pm['id']}/attach", {"customer": customer["id"]}),
        (f"payment_methods/{pm['id']}/detach", {}),
    ):
        assert (await client.post("/v1/" + path, data=data, headers=headers)).status_code == 403
    for resource, obj in (("payment_intents", intent), ("setup_intents", setup)):
        assert (
            await client.get(
                f"/v1/{resource}/{obj['id']}", params={"client_secret": "wrong"}, headers=headers
            )
        ).status_code == 400
        assert (
            await client.get(
                f"/v1/{resource}/{obj['id']}", params={"client_secret": obj["client_secret"]}, headers=headers
            )
        ).status_code == 200


@pytest.mark.parametrize("detach", [False, True])
async def test_confirmation_rechecks_stored_payment_method_ownership(
    client: httpx.AsyncClient, detach: bool
) -> None:
    customer = await post(client, "customers")
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": "tok_visa"})
    intent = await post(
        client, "payment_intents", {"amount": 100, "currency": "usd", "payment_method": pm["id"]}
    )
    await post(client, f"payment_methods/{pm['id']}/attach", {"customer": customer["id"]})
    if detach:
        await post(client, f"payment_methods/{pm['id']}/detach")
    response = await client.post(f"/v1/payment_intents/{intent['id']}/confirm")
    assert response.status_code == 400
    assert (await get(client, "payment_intents/" + intent["id"]))["latest_charge"] is None


async def test_intent_customer_update_and_payment_method_unset(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers")
    intent = await post(
        client, "payment_intents", {"amount": 100, "currency": "usd", "payment_method": "pm_card_visa"}
    )
    updated = await post(
        client, "payment_intents/" + intent["id"], {"customer": customer["id"], "payment_method": ""}
    )
    assert updated["customer"] == customer["id"]
    assert updated["payment_method"] is None and updated["status"] == "requires_payment_method"
    assert (
        await client.post("/v1/payment_intents/" + intent["id"], data={"customer": "cus_missing"})
    ).status_code == 404


async def test_deleted_customer_tombstone_and_mutation_rejection(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers")
    await client.delete("/v1/customers/" + customer["id"])
    assert (await get(client, "customers/" + customer["id"])) == {
        "id": customer["id"],
        "object": "customer",
        "deleted": True,
    }
    for path, data in (
        ("customers/" + customer["id"], {"name": "Changed"}),
        ("payment_intents", {"customer": customer["id"], "amount": 100, "currency": "usd"}),
        ("setup_intents", {"customer": customer["id"]}),
        ("invoices", {"customer": customer["id"]}),
    ):
        assert (await client.post("/v1/" + path, data=data)).status_code == 404


async def test_default_payment_instruments_cannot_cross_customers(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    existing = stripe_hub.context("stripe").store.collection("customers").values()[0]
    customer = await post(client, "customers")
    pm_id = existing["invoice_settings"]["default_payment_method"]
    price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    subscription = await post(
        client, "subscriptions", {"customer": customer["id"], "items[0][price]": price["id"]}
    )
    for path, data in (
        ("customers", {"invoice_settings[default_payment_method]": pm_id}),
        ("invoices", {"customer": customer["id"], "default_payment_method": pm_id}),
        (
            "subscriptions/" + subscription["id"],
            {"default_payment_method": pm_id},
        ),
    ):
        response = await client.post("/v1/" + path, data=data)
        assert response.status_code == 400
    source = await post(client, f"customers/{existing['id']}/sources", {"source": "tok_visa"})
    assert (
        await client.post("/v1/customers/" + customer["id"], data={"default_source": source["id"]})
    ).status_code == 400


async def test_cancel_clears_pending_authentication(client: httpx.AsyncClient) -> None:
    intent = await post(
        client,
        "payment_intents",
        {
            "amount": 100,
            "currency": "usd",
            "payment_method": "pm_card_threeDSecure2Required",
            "confirm": "true",
        },
    )
    assert intent["next_action"]
    canceled = await post(
        client, f"payment_intents/{intent['id']}/cancel", {"cancellation_reason": "abandoned"}
    )
    assert canceled["next_action"] is None and canceled["status"] == "canceled"


async def test_price_updates_preserve_uniqueness_and_immutable_fields(client: httpx.AsyncClient) -> None:
    product = await post(client, "products", {"name": "Audit catalog"})
    prices = [
        await post(client, "prices", {"product": product["id"], "currency": "usd", "unit_amount": amount})
        for amount in (100, 200)
    ]
    await post(
        client, "prices/" + prices[0]["id"], {"lookup_key": "audit_catalog", "tax_behavior": "inclusive"}
    )
    assert (
        await client.post("/v1/prices/" + prices[1]["id"], data={"lookup_key": "audit_catalog"})
    ).status_code == 400
    await post(
        client, "prices/" + prices[1]["id"], {"lookup_key": "audit_catalog", "transfer_lookup_key": "true"}
    )
    assert (await get(client, "prices/" + prices[0]["id"]))["lookup_key"] is None
    for data in ({"unit_amount": 300}, {"tax_behavior": "exclusive"}):
        assert (await client.post("/v1/prices/" + prices[0]["id"], data=data)).status_code == 400
    other_product = await post(client, "products", {"name": "Other product"})
    assert (
        await client.post("/v1/products/" + other_product["id"], data={"default_price": prices[0]["id"]})
    ).status_code == 400
    for interval, count in (("month", 37), ("year", 4), ("day", 1096), ("week", 157)):
        assert (
            await client.post(
                "/v1/prices",
                data={
                    "product": product["id"],
                    "currency": "usd",
                    "unit_amount": 100,
                    "recurring[interval]": interval,
                    "recurring[interval_count]": count,
                },
            )
        ).status_code == 400


async def test_subscription_price_change_updates_plan_and_resets_quantity(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers")
    price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    other_price = await post(
        client,
        "prices",
        {"product": price["product"], "currency": "usd", "unit_amount": 200, "recurring[interval]": "month"},
    )
    subscription = await post(
        client,
        "subscriptions",
        {"customer": customer["id"], "items[0][price]": price["id"], "items[0][quantity]": 3},
    )
    item_id = subscription["items"]["data"][0]["id"]
    item = await post(client, "subscription_items/" + item_id, {"price": other_price["id"]})
    assert item["quantity"] == 1 and item["plan"]["id"] == other_price["id"] and item["plan"]["amount"] == 200
    changed = await post(
        client,
        "subscriptions/" + subscription["id"],
        {"items[0][id]": item_id, "items[0][price]": price["id"], "items[0][quantity]": 4},
    )
    assert changed["items"]["data"][0]["quantity"] == 4
    assert changed["items"]["data"][0]["plan"]["id"] == price["id"]
    cad = await post(
        client,
        "prices",
        {"product": price["product"], "currency": "cad", "unit_amount": 100, "recurring[interval]": "month"},
    )
    assert (
        await client.post("/v1/subscription_items/" + item_id, data={"price": cad["id"]})
    ).status_code == 400
    assert (
        await client.post(
            "/v1/subscription_items", data={"subscription": subscription["id"], "price": cad["id"]}
        )
    ).status_code == 400


async def test_month_end_billing_anchor_survives_short_month(client: httpx.AsyncClient) -> None:
    start = int(datetime(2024, 1, 31, tzinfo=UTC).timestamp())
    clock = await post(client, "test_helpers/test_clocks", {"frozen_time": start})
    customer = await post(client, "customers", {"test_clock": clock["id"]})
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": "tok_visa"})
    await post(client, f"payment_methods/{pm['id']}/attach", {"customer": customer["id"]})
    price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    subscription = await post(
        client,
        "subscriptions",
        {"customer": customer["id"], "default_payment_method": pm["id"], "items[0][price]": price["id"]},
    )
    await post(
        client,
        f"test_helpers/test_clocks/{clock['id']}/advance",
        {"frozen_time": int(datetime(2024, 3, 1, tzinfo=UTC).timestamp())},
    )
    item = (await get(client, "subscriptions/" + subscription["id"]))["items"]["data"][0]
    assert item["current_period_end"] == int(datetime(2024, 3, 31, tzinfo=UTC).timestamp())


async def test_event_snapshot_retains_its_original_api_version(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers")
    invoice = await post(client, "invoices", {"customer": customer["id"]})
    event = (await get(client, "events", params={"type": "invoice.created"}))["data"][0]
    retrieved = await get(client, "events/" + event["id"], headers={"Stripe-Version": "2024-06-20"})
    assert retrieved["data"]["object"] == event["data"]["object"]
    assert retrieved["data"]["object"]["id"] == invoice["id"]


async def test_invalid_creation_does_not_enqueue_ghost_events(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    ctx = stripe_hub.context("stripe")
    ctx.webhooks.mode = "manual"
    await post(client, "webhook_endpoints", {"url": "https://consumer.example/hook", "enabled_events[]": "*"})
    before = len(ctx.webhooks.deliveries)
    for path, data in (
        ("customers", {"source": "tok_missing"}),
        ("payment_intents", {"amount": 100, "currency": "usd", "confirm": "true"}),
        ("payment_intents", {"amount": 100, "currency": "usd", "confirm": "invalid"}),
    ):
        assert (await client.post("/v1/" + path, data=data)).status_code in {400, 404}
        assert len(ctx.webhooks.deliveries) == before


async def test_card_token_converts_to_modern_payment_method_shape(client: httpx.AsyncClient) -> None:
    token = await post(
        client, "tokens", {"card[number]": "4242424242424242", "card[exp_month]": 12, "card[exp_year]": 2030}
    )
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": token["id"]})
    assert pm["card"]["networks"]["available"] == ["visa"]
    assert pm["card"]["checks"]["address_line1_check"] is None
    assert "object" not in pm["card"] and "address_zip" not in pm["card"]
    assert (
        await client.post("/v1/payment_methods/" + pm["id"], data={"card[exp_month]": 13})
    ).status_code == 400
    assert (await client.post("/v1/payment_methods", data={"card[token]": "tok_visa"})).status_code == 400


async def test_invoice_item_quantity_and_period_propagate_to_lines(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers")
    invoice = await post(client, "invoices", {"customer": customer["id"]})
    item = await post(
        client,
        "invoiceitems",
        {
            "customer": customer["id"],
            "invoice": invoice["id"],
            "unit_amount": 200,
            "quantity": 2,
            "currency": "usd",
            "period[start]": 1700000000,
            "period[end]": 1700001000,
        },
    )
    assert item["amount"] == 400 and item["period"]["start"] == 1700000000
    updated = await post(client, "invoiceitems/" + item["id"], {"quantity": 3, "period[end]": 1700002000})
    assert updated["amount"] == 600
    line = (await get(client, f"invoices/{invoice['id']}/lines"))["data"][0]
    assert line["quantity"] == 3 and line["amount"] == 600 and line["period"] == updated["period"]
    assert (await get(client, "invoices/" + invoice["id"]))["total"] == 600
    assert (await client.post("/v1/invoiceitems/" + item["id"], data={"period[end]": 1})).status_code == 400
    assert (
        await client.post("/v1/invoiceitems", data={"customer": customer["id"], "currency": "usd"})
    ).status_code == 400


async def test_malformed_idempotent_query_and_failed_validation_retry(client: httpx.AsyncClient) -> None:
    headers = {"Idempotency-Key": "audit-malformed"}
    response = await client.post("/v1/customers?bad[=x", data={"name": "Example"}, headers=headers)
    assert response.status_code == 400 and response.headers["request-id"]
    assert (await client.post("/v1/customers", data={"name": "雪 ☃"}, headers=headers)).status_code == 200
    assert (
        await client.post(
            "/v1/customers",
            content="metadata[x]=a&metadata=b",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    ).status_code == 400


async def test_unicode_file_download_and_invalid_multipart(client: httpx.AsyncClient) -> None:
    uploaded = await client.post(
        "https://files.stripe.com/v1/files",
        data={"purpose": "dispute_evidence"},
        files={"file": ("雪.pdf", b"%PDF-1.4 audit", "application/pdf")},
    )
    assert uploaded.status_code == 200
    downloaded = await client.get(uploaded.json()["url"])
    assert downloaded.content == b"%PDF-1.4 audit"
    assert "filename*=UTF-8''%E9%9B%AA.pdf" in downloaded.headers["content-disposition"]
    bad = await client.post(
        "https://files.stripe.com/v1/files",
        files={"purpose": (None, b"\xff"), "file": ("evidence.pdf", b"%PDF")},
    )
    assert bad.status_code == 400 and bad.json()["error"]["param"] == "purpose"
    empty = await client.post(
        "https://files.stripe.com/v1/files",
        data={"purpose": "dispute_evidence"},
        files={"file": ("empty.pdf", b"")},
    )
    assert empty.status_code == 400


async def test_checkout_currency_mode_and_expiration_validation(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    product = await post(client, "products", {"name": "Checkout audit"})
    prices = [
        await post(client, "prices", {"product": product["id"], "currency": currency, "unit_amount": 100})
        for currency in ("usd", "cad")
    ]
    recurring = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    for data in (
        {"mode": "payment", "line_items[0][price]": recurring["id"]},
        {"mode": "payment", "line_items[0][price]": prices[0]["id"], "line_items[1][price]": prices[1]["id"]},
        {"mode": "setup", "expires_at": stripe_hub.clock.timestamp() + 86401},
    ):
        assert (await client.post("/v1/checkout/sessions", data=data)).status_code == 400


async def test_webhook_update_validation(client: httpx.AsyncClient) -> None:
    endpoint = await post(
        client,
        "webhook_endpoints",
        {"url": "https://consumer.example/hook", "enabled_events[]": "invoice.paid"},
    )
    for data in ({"url": "invalid"}, {"url": "http://[invalid"}, {"enabled_events[0][bad]": "x"}):
        assert (await client.post("/v1/webhook_endpoints/" + endpoint["id"], data=data)).status_code == 400
    assert (await get(client, "webhook_endpoints/" + endpoint["id"]))["url"] == endpoint["url"]


async def test_extra_path_segments_and_preview_methods_do_not_mutate(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    ctx = stripe_hub.context("stripe")
    sub = ctx.store.collection("subscriptions").values()[0]
    before = ctx.store.snapshot()
    assert (await client.delete(f"/v1/subscriptions/{sub['id']}/extra")).status_code == 404
    assert (await client.post("/v1/payment_intents/pi_missing/confirm/extra")).status_code == 404
    assert (await client.get("/v1/invoices/create_preview")).status_code == 404
    assert (await client.post("/v1/invoices/upcoming")).status_code == 404
    await client.get("/v1/payment_methods/pm_card_visa")
    assert {k: v for k, v in ctx.store.snapshot()["collections"].items() if v} == {
        k: v for k, v in before["collections"].items() if v
    }


async def test_time_inputs_outside_timestamp_range_are_rejected(client: httpx.AsyncClient) -> None:
    for value in (-1, 253402300800, "9" * 5000):
        assert (
            await client.post("/v1/test_helpers/test_clocks", data={"frozen_time": value})
        ).status_code == 400
    clock = await post(client, "test_helpers/test_clocks", {"frozen_time": 1700000000})
    assert (
        await client.post(
            f"/v1/test_helpers/test_clocks/{clock['id']}/advance", data={"frozen_time": 1900000000}
        )
    ).status_code == 400


async def test_create_customer_attaches_and_selects_default_payment_method(client: httpx.AsyncClient) -> None:
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": "tok_visa"})
    customer = await post(
        client,
        "customers",
        {"payment_method": pm["id"], "invoice_settings[default_payment_method]": pm["id"]},
    )
    assert customer["invoice_settings"]["default_payment_method"] == pm["id"]
    assert (await get(client, "payment_methods/" + pm["id"]))["customer"] == customer["id"]


async def test_setup_intent_state_errors_use_setup_error_codes(client: httpx.AsyncClient) -> None:
    intent = await post(client, "setup_intents")
    assert (
        await client.post(
            f"/v1/setup_intents/{intent['id']}/cancel", data={"cancellation_reason": "fraudulent"}
        )
    ).status_code == 400
    await post(client, f"setup_intents/{intent['id']}/cancel")
    response = await client.post(
        f"/v1/setup_intents/{intent['id']}/confirm", data={"payment_method": "pm_card_visa"}
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "setup_intent_unexpected_state"


async def test_expansions_enforce_scopes_and_publishable_visibility(client: httpx.AsyncClient) -> None:
    customer = await post(
        client,
        "customers",
        {"payment_method": "pm_card_visa", "invoice_settings[default_payment_method]": "pm_card_visa"},
    )
    token = (
        await client.post("/_emulator/tokens", json={"app_only": True, "scopes": ["customers:read"]})
    ).json()["access_token"]
    response = await client.get(
        "/v1/customers/" + customer["id"],
        params={"expand[]": "invoice_settings.default_payment_method"},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 403
    intent = await post(
        client, "payment_intents", {"amount": 100, "currency": "usd", "customer": customer["id"]}
    )
    public = (await client.get("/_emulator/info")).json()["credentials"]["publishable_key"]
    response = await client.get(
        "/v1/payment_intents/" + intent["id"],
        params={"client_secret": intent["client_secret"], "expand[]": "customer"},
        headers={"Authorization": "Bearer " + public},
    )
    assert response.status_code == 400


async def test_payment_history_survives_customer_deletion(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers")
    intent = await post(
        client,
        "payment_intents",
        {
            "amount": 100,
            "currency": "usd",
            "customer": customer["id"],
            "payment_method": "pm_card_visa",
            "confirm": "true",
        },
    )
    await client.delete("/v1/customers/" + customer["id"])
    retrieved = await get(client, "payment_intents/" + intent["id"], params={"expand[]": "customer"})
    assert retrieved["customer"] == {"id": customer["id"], "object": "customer", "deleted": True}
    assert retrieved["status"] == "succeeded"


async def test_injected_fault_echoes_requested_api_version(client: httpx.AsyncClient) -> None:
    await client.post("/_emulator/faults", json={"path": "/v1/customers", "status": 429})
    response = await client.get("/v1/customers", headers={"Stripe-Version": "2024-06-20"})
    assert response.status_code == 429 and response.headers["stripe-version"] == "2024-06-20"
    assert response.headers["retry-after"] == "2" and response.headers["request-id"]


def test_seed_relationship_chronology(stripe_hub: EmulatorHub) -> None:
    store = stripe_hub.context("stripe").store
    for collection in ("refunds", "disputes"):
        for record in store.collection(collection):
            assert store.collection("charges").get(record["charge"])["created"] <= record["created"]
    for sub in store.collection("subscriptions"):
        for item in sub["items"]["data"]:
            assert store.collection("prices").get(item["price"]["id"])["created"] <= sub["created"]


async def oauth_connection(client: httpx.AsyncClient, scope: str = "read_write") -> dict[str, Any]:
    response = await client.get(
        "https://connect.stripe.com/oauth/authorize",
        params={
            "client_id": "ca_audit",
            "response_type": "code",
            "scope": scope,
            "redirect_uri": "https://halcyonadvisory.example/oauth",
        },
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)
    token = await client.post(
        "https://connect.stripe.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
        },
    )
    assert token.status_code == 200
    return {**token.json(), "code": query["code"][0]}


async def test_oauth_requires_authentication_and_valid_authorize_parameters(
    client: httpx.AsyncClient,
) -> None:
    async with httpx.AsyncClient(
        transport=client._transport, base_url="https://connect.stripe.com"
    ) as unauthenticated:
        response = await unauthenticated.post(
            "/oauth/token", data={"grant_type": "authorization_code", "code": "missing"}
        )
        assert response.status_code == 401 and response.json()["error"] == "invalid_client"
    valid = {"client_id": "ca_audit", "redirect_uri": "https://halcyonadvisory.example/oauth"}
    for change, error in (
        ({}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"response_type": "code", "scope": "admin"}, "invalid_scope"),
    ):
        response = await client.get("https://connect.stripe.com/oauth/authorize", params=valid | change)
        assert response.status_code == 400 and response.json()["error"] == error
    response = await client.get(
        "https://connect.stripe.com/oauth/authorize", params=valid | {"response_type": "code"}
    )
    assert parse_qs(urlsplit(response.headers["location"]).query)["scope"] == ["read_only"]


async def test_oauth_refresh_revokes_same_scope_and_prevents_scope_escalation(
    client: httpx.AsyncClient,
) -> None:
    connection = await oauth_connection(client, "read_only")
    denied = await client.post(
        "https://connect.stripe.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": connection["refresh_token"],
            "scope": "read_write",
        },
    )
    assert denied.status_code == 400 and denied.json()["error"] == "invalid_scope"
    refreshed = await client.post(
        "https://connect.stripe.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": connection["refresh_token"],
        },
    )
    assert refreshed.status_code == 200
    assert (
        await client.get("/v1/customers", headers={"Authorization": "Bearer " + connection["access_token"]})
    ).status_code == 401
    assert (
        await client.get(
            "/v1/customers", headers={"Authorization": "Bearer " + refreshed.json()["access_token"]}
        )
    ).status_code == 200
    # Stripe refresh grants remain reusable; only same-scope access tokens are replaced.
    assert (
        await client.post(
            "https://connect.stripe.com/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": connection["refresh_token"]},
        )
    ).status_code == 200


async def test_oauth_code_reuse_revokes_connection_and_codes_expire_in_five_minutes(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    connection = await oauth_connection(client)
    reused = await client.post(
        "https://connect.stripe.com/oauth/token",
        data={"grant_type": "authorization_code", "code": connection["code"]},
    )
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    assert (
        await client.get("/v1/customers", headers={"Authorization": "Bearer " + connection["access_token"]})
    ).status_code == 401
    response = await client.get(
        "https://connect.stripe.com/oauth/authorize?"
        + urlencode(
            {
                "client_id": "ca_audit",
                "response_type": "code",
                "redirect_uri": "https://halcyonadvisory.example/oauth",
            }
        )
    )
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    stripe_hub.clock.advance(timedelta(minutes=5))
    expired = await client.post(
        "https://connect.stripe.com/oauth/token", data={"grant_type": "authorization_code", "code": code}
    )
    assert expired.status_code == 400 and expired.json()["error"] == "invalid_grant"
