"""Reject malformed form shapes before resource handlers can mutate state."""

from __future__ import annotations

import re
from typing import Any

from emulators.providers.stripe.wire import Record, boolean, fail, integer

# Stripe forms have typed containers; arbitrary bracket syntax does not turn an ID into a hash.
HASHES = {
    "metadata",
    "address",
    "shipping",
    "invoice_settings",
    "billing_details",
    "card",
    "recurring",
    "product_data",
    "price_data",
    "default_price_data",
    "payment_method_data",
    "automatic_payment_methods",
    "evidence",
    "trial_settings",
    "end_behavior",
    "subscription_details",
    "subscription_data",
    "payment_intent_data",
    "setup_intent_data",
    "pricing",
    "period",
    "pause_collection",
    "flow_data",
    "payment_settings",
    "payment_method_options",
    "automatic_tax",
    "package_dimensions",
    "transfer_data",
    "rendering_options",
    "after_completion",
    "billing_cycle_anchor_config",
    "cancellation_details",
}
STRING_ARRAYS = {
    "expand",
    "payment_method_types",
    "enabled_events",
    "preferred_locales",
    "images",
    "lookup_keys",
    "types",
    "tax_rates",
    "default_tax_rates",
}
OBJECT_ARRAYS = {"items", "line_items", "custom_fields", "discounts", "tax_id_data", "marketing_features"}
UNSETTABLE = {"metadata", "address", "shipping", "pause_collection", "discounts", "tax_rates"}
BOOLEAN_FIELDS = {
    "active",
    "paid",
    "confirm",
    "off_session",
    "cancel_at_period_end",
    "auto_advance",
    "disabled",
    "submit",
    "deleted",
    "transfer_lookup_key",
    "paid_out_of_band",
    "enabled",
    "error_on_requires_action",
}
HASH_FIELDS = {
    "address": {"city", "country", "line1", "line2", "postal_code", "state"},
    "period": {"start", "end"},
    "trial_settings": {"end_behavior"},
    "end_behavior": {"missing_payment_method"},
}
ENUMS = {
    "capture_method": {"automatic", "automatic_async", "manual"},
    "confirmation_method": {"automatic", "manual"},
    "collection_method": {"charge_automatically", "send_invoice"},
    "tax_exempt": {"none", "exempt", "reverse"},
    "tax_behavior": {"unspecified", "inclusive", "exclusive"},
    "allow_redisplay": {"always", "limited", "unspecified"},
    "usage": {"on_session", "off_session"},
    "setup_future_usage": {"", "on_session", "off_session"},
    "payment_behavior": {
        "allow_incomplete",
        "default_incomplete",
        "error_if_incomplete",
        "pending_if_incomplete",
    },
    "proration_behavior": {"none", "create_prorations", "always_invoice"},
    "pending_invoice_items_behavior": {"include", "exclude"},
    "missing_payment_method": {"cancel", "pause", "create_invoice"},
}


def validate_parameters(params: Record, parent: str = "") -> None:
    """Validate recursively, including arrays' entries and nested hashes' scalar leaves."""
    for key, value in params.items():
        param = f"{parent}[{key}]" if parent else key
        container = parent.rsplit("[", 1)[-1].rstrip("]")
        if container in HASH_FIELDS and key not in HASH_FIELDS[container]:
            fail("Received unknown parameter: " + key, param, code="parameter_unknown")
        if key in UNSETTABLE and value == "":
            continue
        if parent.endswith("metadata"):
            if not isinstance(value, str):
                fail("Metadata values must be strings.", param)
            continue
        if key in HASHES or (key == "source" and isinstance(value, dict)):
            if not isinstance(value, dict):
                fail("Invalid hash", param)
            validate_parameters(value, param)
        elif key in STRING_ARRAYS | OBJECT_ARRAYS:
            if not isinstance(value, list):
                fail("Invalid array", param)
            for index, item in enumerate(value):
                location = f"{param}[{index}]"
                if key in STRING_ARRAYS:
                    if not isinstance(item, str):
                        fail("Invalid string", location)
                else:
                    if not isinstance(item, dict):
                        fail("Invalid hash", location)
                    validate_parameters(item, location)
        elif key in {"created", "arrival_date"} and not parent and isinstance(value, dict):
            for operator, bound in value.items():
                if operator not in {"gt", "gte", "lt", "lte"}:
                    fail("Invalid timestamp filter", param)
                integer(bound, f"{param}[{operator}]")
        elif not isinstance(value, str):
            fail("Invalid string", param)
        if key in ENUMS and isinstance(value, str) and value not in ENUMS[key]:
            fail("Invalid enum value", param)
        if key in BOOLEAN_FIELDS and isinstance(value, str):
            boolean(value)
        if key == "currency" and isinstance(value, str):
            if not re.fullmatch("[a-zA-Z]{3}", value):
                fail("Invalid currency", param)
            params[key] = value.lower()


def timestamp(value: Any, param: str) -> int:
    """Keep dates representable by Python's calendar arithmetic and Stripe's timestamp fields."""
    return integer(value, param, minimum=0, maximum=253402300799)
