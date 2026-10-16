from __future__ import annotations

import httpx
import pytest


async def setup_records(client: httpx.AsyncClient) -> None:
    for i, (name, revenue, stage) in enumerate(
        [
            ("ALPHA Logistics", "100", "lead"),
            ("Beta Advisory", "200", "customer"),
            ("Gamma Logistics", "300", "opportunity"),
            ("Delta Partners", None, "lead"),
        ]
    ):
        props = {
            "name": name,
            "domain": f"search-{i}.example",
            "lifecyclestage": stage,
            "city": "SearchFixture",
        }
        if revenue:
            props["annualrevenue"] = revenue
        assert (await client.post("/crm/v3/objects/companies", json={"properties": props})).status_code == 201


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("EQ", {"value": "200"}, {"Beta Advisory"}),
        ("NEQ", {"value": "200"}, {"ALPHA Logistics", "Gamma Logistics"}),
        ("LT", {"value": "200"}, {"ALPHA Logistics"}),
        ("LTE", {"value": "200"}, {"ALPHA Logistics", "Beta Advisory"}),
        ("GT", {"value": "200"}, {"Gamma Logistics"}),
        ("GTE", {"value": "200"}, {"Beta Advisory", "Gamma Logistics"}),
        ("BETWEEN", {"value": "100", "highValue": "200"}, {"ALPHA Logistics", "Beta Advisory"}),
        ("IN", {"values": ["100", "300"]}, {"ALPHA Logistics", "Gamma Logistics"}),
        ("NOT_IN", {"values": ["100", "300"]}, {"Beta Advisory"}),
        ("HAS_PROPERTY", {}, {"ALPHA Logistics", "Beta Advisory", "Gamma Logistics"}),
        ("NOT_HAS_PROPERTY", {}, {"Delta Partners"}),
    ],
)
async def test_numeric_and_presence_filters(
    client: httpx.AsyncClient, operator: str, value: dict, expected: set[str]
) -> None:
    await setup_records(client)
    result = await client.post(
        "/crm/v3/objects/companies/search",
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "city", "operator": "EQ", "value": "SearchFixture"},
                        {"propertyName": "annualrevenue", "operator": operator, **value},
                    ]
                }
            ],
            "properties": ["name"],
        },
    )
    assert result.status_code == 200, result.text
    assert {r["properties"]["name"] for r in result.json()["results"]} == expected
    assert result.json()["total"] == len(expected)


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("CONTAINS_TOKEN", "logistics", {"ALPHA Logistics", "Gamma Logistics"}),
        ("CONTAINS_TOKEN", "adv*", {"Beta Advisory"}),
        ("NOT_CONTAINS_TOKEN", "*logistics", {"Beta Advisory", "Delta Partners"}),
        ("EQ", "alpha logistics", {"ALPHA Logistics"}),
    ],
)
async def test_text_token_and_case_insensitive_filters(
    client: httpx.AsyncClient, operator: str, value: str, expected: set[str]
) -> None:
    await setup_records(client)
    result = await client.post(
        "/crm/v3/objects/companies/search",
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "city", "operator": "EQ", "value": "SearchFixture"},
                        {"propertyName": "name", "operator": operator, "value": value},
                    ]
                }
            ],
            "properties": ["name"],
        },
    )
    assert result.status_code == 200
    assert {r["properties"]["name"] for r in result.json()["results"]} == expected


async def test_or_groups_query_sort_paging_archived_and_date(client: httpx.AsyncClient) -> None:
    await setup_records(client)
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "annualrevenue", "operator": "EQ", "value": "100"},
                    {"propertyName": "city", "operator": "EQ", "value": "SearchFixture"},
                ]
            },
            {"filters": [{"propertyName": "name", "operator": "EQ", "value": "Gamma Logistics"}]},
        ],
        "query": "LOGISTICS",
        "properties": ["name", "annualrevenue"],
        "sorts": [{"propertyName": "annualrevenue", "direction": "DESCENDING"}],
        "limit": 1,
    }
    first = await client.post("/crm/v3/objects/companies/search", json=payload)
    assert first.status_code == 200 and first.json()["total"] == 2
    assert first.json()["results"][0]["properties"]["annualrevenue"] == "300"
    second = await client.post(
        "/crm/v3/objects/companies/search", json={**payload, "after": first.json()["paging"]["next"]["after"]}
    )
    assert second.json()["results"][0]["properties"]["annualrevenue"] == "100"
    assert "paging" not in second.json()
    await client.delete("/crm/v3/objects/companies/" + first.json()["results"][0]["id"])
    assert (await client.post("/crm/v3/objects/companies/search", json=payload)).json()["total"] == 1
    dates = await client.post(
        "/crm/v3/objects/deals/search",
        json={
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "closedate",
                            "operator": "BETWEEN",
                            "value": "2025-01-01T00:00:00Z",
                            "highValue": "2027-01-01T00:00:00Z",
                        }
                    ]
                }
            ],
            "sorts": ["-closedate"],
        },
    )
    assert dates.status_code == 200 and dates.json()["total"] > 0
    assert [r["properties"]["closedate"] for r in dates.json()["results"]] == sorted(
        [r["properties"]["closedate"] for r in dates.json()["results"]], reverse=True
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 201},
        {"after": 10000},
        {"after": -1},
        {"limit": True},
        {"filterGroups": [{"filters": [{"propertyName": "name", "operator": "LIKE", "value": "x"}]}]},
        {"filterGroups": [{"filters": [{"propertyName": "absent", "operator": "EQ", "value": "x"}]}]},
        {"filterGroups": [{"filters": [{"propertyName": "name", "operator": "IN", "value": "x"}]}]},
        {"filterGroups": [{"filters": [{"propertyName": "name", "operator": "EQ"}]}]},
        {
            "filterGroups": [
                {"filters": [{"propertyName": "annualrevenue", "operator": "EQ", "value": "NaN"}]}
            ]
        },
        {"sorts": ["unknown"]},
        {"sorts": ["name", "domain"]},
        {"query": {}},
        {"filterGroups": {}},
    ],
)
async def test_search_validation_before_evaluation(client: httpx.AsyncClient, payload: dict) -> None:
    result = await client.post("/crm/v3/objects/companies/search", json=payload)
    assert result.status_code == 400, result.text
