"""Tests for the OData v4 query engine, using Graph-shaped users, messages, events and driveItems."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest

from emulators.core.odata import (
    ExpandItem,
    ODataError,
    QueryOptions,
    apply_orderby,
    apply_query,
    apply_select,
    compile_filter,
    compile_search,
    parse_expand,
    parse_filter,
    parse_orderby,
    parse_query_options,
    parse_select,
    select_record,
)

# --------------------------------------------------------------------------------------------------
# Fixtures: Graph-shaped records (camelCase, nested complex types, collections)
# --------------------------------------------------------------------------------------------------

SKU_E5 = "c42b9cae-ea4f-4ab7-9717-81576235ccac"
SKU_F3 = "b05e124f-c7cc-45a0-a6aa-8cf78c946968"
U_ALICE = "a1b2c3d4-0000-4000-8000-000000000001"
U_BOB = "b2b2c3d4-0000-4000-8000-000000000002"
U_CAROL = "c3b2c3d4-0000-4000-8000-000000000003"
U_DAVE = "04b2c3d4-0000-4000-8000-000000000004"


@pytest.fixture
def users() -> list[dict[str, Any]]:
    return [
        {
            "id": U_ALICE,
            "displayName": "Alice Anderson",
            "givenName": "Alice",
            "surname": "Anderson",
            "mail": "alice@contoso.com",
            "userPrincipalName": "alice@contoso.com",
            "jobTitle": "Engineer",
            "department": "Engineering",
            "accountEnabled": True,
            "createdDateTime": "2025-03-01T08:30:00Z",
            "proxyAddresses": ["SMTP:alice@contoso.com", "smtp:alice.anderson@contoso.com"],
            "assignedLicenses": [{"skuId": SKU_E5, "disabledPlans": []}],
            "manager": {"id": U_CAROL, "displayName": "Carol O'Brien"},
            "onPremisesExtensionAttributes": {"extensionAttribute1": "Blue"},
        },
        {
            "id": U_BOB,
            "displayName": "bob baker",
            "givenName": "Bob",
            "surname": "Baker",
            "mail": None,
            "userPrincipalName": "bob@contoso.com",
            "jobTitle": None,
            "department": None,
            "accountEnabled": False,
            "createdDateTime": "2024-11-20T17:45:10.1234567Z",
            "proxyAddresses": ["SMTP:bob@contoso.com"],
            "assignedLicenses": [],
        },
        {
            "id": U_CAROL,
            "displayName": "Carol O'Brien",
            "givenName": "Carol",
            "surname": "O'Brien",
            "mail": "carol@contoso.com",
            "userPrincipalName": "carol@contoso.com",
            "jobTitle": "Director",
            "department": "Engineering",
            "accountEnabled": True,
            "createdDateTime": "2023-01-05T00:00:00Z",
            "proxyAddresses": ["SMTP:carol@contoso.com", "smtp:cobrien@contoso.com"],
            "assignedLicenses": [{"skuId": SKU_F3}, {"skuId": SKU_E5}],
        },
        {
            # no jobTitle / createdDateTime keys at all: "missing" must behave like null
            "id": U_DAVE,
            "displayName": "Dave Dubois",
            "mail": "dave@fabrikam.com",
            "userPrincipalName": "dave@fabrikam.com",
            "department": "Sales",
            "accountEnabled": True,
            "proxyAddresses": ["SMTP:dave@fabrikam.com"],
        },
    ]


@pytest.fixture
def messages() -> list[dict[str, Any]]:
    def msg(
        mid: str, subject: str, sender: str, to: list[str], received: str, **extra: Any
    ) -> dict[str, Any]:
        return {
            "id": mid,
            "subject": subject,
            "from": {"emailAddress": {"name": sender.split("@")[0], "address": sender}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "receivedDateTime": received,
            "isRead": False,
            "importance": "normal",
            "hasAttachments": False,
            "categories": [],
            **extra,
        }

    return [
        msg(
            "m1",
            "Quarterly report",
            "alice@contoso.com",
            ["bob@contoso.com", "carol@contoso.com"],
            "2026-10-14T09:15:00Z",
            importance="high",
            hasAttachments=True,
            categories=["Red", "Finance"],
        ),
        msg(
            "m2",
            "Lunch?",
            "bob@contoso.com",
            ["alice@contoso.com"],
            "2026-10-15T07:00:00.0000000Z",
            isRead=True,
        ),
        msg(
            "m3",
            "Re: Quarterly report",
            "carol@contoso.com",
            ["alice@contoso.com"],
            "2026-10-15T12:30:00Z",
            categories=["red"],
        ),
        msg(
            "m4",
            "Offsite agenda",
            "dave@fabrikam.com",
            ["alice@contoso.com"],
            "2026-09-30T23:59:59Z",
            importance="low",
            hasAttachments=True,
        ),
    ]


@pytest.fixture
def events() -> list[dict[str, Any]]:
    def ev(eid: str, subject: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
        return {
            "id": eid,
            "subject": subject,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "isAllDay": False,
            "showAs": "busy",
            **extra,
        }

    return [
        ev("e1", "Standup", "2026-10-15T14:00:00.0000000", "2026-10-15T14:15:00.0000000"),
        ev(
            "e2", "Planning", "2026-10-16T09:00:00.0000000", "2026-10-16T10:00:00.0000000", showAs="tentative"
        ),
        ev("e3", "Holiday", "2026-12-25T00:00:00.0000000", "2026-12-26T00:00:00.0000000", isAllDay=True),
    ]


@pytest.fixture
def drive_items() -> list[dict[str, Any]]:
    return [
        {
            "id": "d1",
            "name": "Budget.xlsx",
            "size": 20480,
            "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            "parentReference": {"id": "folder-finance", "driveId": "drv1", "path": "/drive/root:/Finance"},
            "lastModifiedDateTime": "2026-10-10T10:00:00Z",
            "fields": {"Status": "Approved", "Amount": 12.5},
        },
        {
            "id": "d2",
            "name": "notes.TXT",
            "size": 120,
            "file": {"mimeType": "text/plain"},
            "parentReference": {"id": "folder-finance", "driveId": "drv1"},
            "lastModifiedDateTime": "2026-10-12T08:00:00Z",
            "fields": {"Status": "Draft", "Amount": 0.0},
        },
        {
            "id": "d3",
            "name": "Finance",
            "size": 0,
            "folder": {"childCount": 2},
            "parentReference": {"id": "root-id", "driveId": "drv1"},
            "lastModifiedDateTime": "2026-09-01T00:00:00Z",
        },
    ]


def ids(records: list[Mapping[str, Any]]) -> list[str]:
    return [r["id"] for r in records]


def select_ids(expr: str, records: list[dict[str, Any]], **kwargs: Any) -> list[str]:
    predicate = compile_filter(expr, **kwargs)
    return [r["id"] for r in records if predicate(r)]


# --------------------------------------------------------------------------------------------------
# $filter: literals and comparison operators
# --------------------------------------------------------------------------------------------------


def test_eq_string(users):
    assert select_ids("givenName eq 'Alice'", users) == [U_ALICE]


def test_ne_string_includes_null_and_missing(users):
    # OData: null ne 'x' is true, so rows with a null or absent jobTitle match
    assert select_ids("jobTitle ne 'Engineer'", users) == [U_BOB, U_CAROL, U_DAVE]


def test_eq_bool_literal(users):
    assert select_ids("accountEnabled eq false", users) == [U_BOB]
    assert select_ids("accountEnabled eq true", users) == [U_ALICE, U_CAROL, U_DAVE]


def test_bool_literals_are_case_insensitive(users):
    assert select_ids("accountEnabled eq FALSE", users) == [U_BOB]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("size gt 120", ["d1"]),
        ("size ge 120", ["d1", "d2"]),
        ("size lt 120", ["d3"]),
        ("size le 120", ["d2", "d3"]),
        ("size eq 0", ["d3"]),
        ("size ne 0", ["d1", "d2"]),
    ],
)
def test_numeric_comparisons(drive_items, expr, expected):
    assert select_ids(expr, drive_items) == expected


def test_decimal_and_negative_literals(drive_items):
    assert select_ids("fields/Amount gt 12.4", drive_items) == ["d1"]
    assert select_ids("fields/Amount eq 12.50", drive_items) == ["d1"]
    assert select_ids("size gt -1 and size lt 1", drive_items) == ["d3"]


def test_int_and_float_compare_numerically(drive_items):
    assert select_ids("fields/Amount eq 0", drive_items) == ["d2"]


def test_null_literal_matches_missing_and_none(users):
    assert select_ids("jobTitle eq null", users) == [U_BOB, U_DAVE]
    assert select_ids("jobTitle ne null", users) == [U_ALICE, U_CAROL]
    assert select_ids("manager eq null", users) == [U_BOB, U_CAROL, U_DAVE]


def test_ordering_against_null_is_false(users):
    assert select_ids("jobTitle gt 'A'", users) == [U_ALICE, U_CAROL]
    assert select_ids("jobTitle le 'Z'", users) == [U_ALICE, U_CAROL]


def test_type_mismatch_is_not_equal(drive_items):
    assert select_ids("size eq 'abc'", drive_items) == []
    assert select_ids("size ne 'abc'", drive_items) == ["d1", "d2", "d3"]


def test_single_quote_escaping(users):
    assert select_ids("displayName eq 'Carol O''Brien'", users) == [U_CAROL]
    assert select_ids("surname eq 'O''Brien'", users) == [U_CAROL]
    assert select_ids("manager/displayName eq 'Carol O''Brien'", users) == [U_ALICE]


def test_string_with_keywords_and_punctuation_is_opaque(users):
    # operators/parentheses inside quotes must not be tokenized
    assert select_ids("displayName eq 'a and (b) eq ''c'' or d'", users) == []
    assert select_ids("displayName eq 'Alice Anderson' and jobTitle eq 'Engineer'", users) == [U_ALICE]


def test_guid_literal_is_case_insensitive_even_when_case_sensitive(users):
    literal = U_ALICE.upper()
    assert select_ids(f"id eq {literal}", users) == [U_ALICE]
    assert select_ids(f"id eq {literal}", users, case_sensitive=True) == [U_ALICE]
    assert select_ids(f"id ne {U_ALICE}", users) == [U_BOB, U_CAROL, U_DAVE]


def test_guid_literal_starting_with_digit(users):
    assert select_ids(f"id eq {U_DAVE}", users) == [U_DAVE]


def test_enum_values_compare_as_strings(messages):
    assert select_ids("importance eq 'high'", messages) == ["m1"]
    assert select_ids("importance eq microsoft.graph.importance'LOW'", messages) == ["m4"]


# --------------------------------------------------------------------------------------------------
# $filter: case sensitivity
# --------------------------------------------------------------------------------------------------


def test_eq_is_case_insensitive_by_default(users):
    assert select_ids("displayName eq 'BOB BAKER'", users) == [U_BOB]
    assert select_ids("displayName ne 'BOB BAKER'", users) == [U_ALICE, U_CAROL, U_DAVE]


def test_eq_case_sensitive_option(users):
    assert select_ids("displayName eq 'BOB BAKER'", users, case_sensitive=True) == []
    assert select_ids("displayName eq 'bob baker'", users, case_sensitive=True) == [U_BOB]
    assert select_ids("displayName ne 'bob baker'", users, case_sensitive=True) == [U_ALICE, U_CAROL, U_DAVE]


def test_in_is_case_insensitive_by_default(users):
    assert select_ids("givenName in ('ALICE', 'bob')", users) == [U_ALICE, U_BOB]
    assert select_ids("givenName in ('ALICE', 'bob')", users, case_sensitive=True) == []


def test_string_functions_case_insensitive_by_default(users):
    assert select_ids("startswith(displayName, 'ALICE')", users) == [U_ALICE]
    assert select_ids("endswith(displayName, 'BAKER')", users) == [U_BOB]
    assert select_ids("contains(displayName, 'dub')", users) == [U_DAVE]


def test_string_functions_case_sensitive_option(users):
    assert select_ids("startswith(displayName, 'ALICE')", users, case_sensitive=True) == []
    assert select_ids("endswith(displayName, 'baker')", users, case_sensitive=True) == [U_BOB]
    assert select_ids("contains(displayName, 'dub')", users, case_sensitive=True) == []
    assert select_ids("contains(displayName, 'Dub')", users, case_sensitive=True) == [U_DAVE]


def test_function_and_operator_names_are_case_insensitive(users):
    assert select_ids("startsWith(displayName,'alice') AND accountEnabled EQ true", users) == [U_ALICE]


def test_property_names_are_case_insensitive(users):
    assert select_ids("DisplayName eq 'alice anderson'", users) == [U_ALICE]
    assert select_ids("MANAGER/DISPLAYNAME eq 'carol o''brien'", users) == [U_ALICE]


# --------------------------------------------------------------------------------------------------
# $filter: logic, precedence, in
# --------------------------------------------------------------------------------------------------


def test_and_binds_tighter_than_or(users):
    expr = "givenName eq 'Bob' or department eq 'Engineering' and jobTitle eq 'Director'"
    assert select_ids(expr, users) == [U_BOB, U_CAROL]


def test_parentheses_override_precedence(users):
    expr = "(givenName eq 'Bob' or department eq 'Engineering') and jobTitle eq 'Director'"
    assert select_ids(expr, users) == [U_CAROL]


def test_not_with_parentheses_and_function(users):
    assert select_ids("not (accountEnabled eq true)", users) == [U_BOB]
    assert select_ids("not startswith(displayName, 'a')", users) == [U_BOB, U_CAROL, U_DAVE]


def test_not_without_parentheses_negates_whole_comparison(users):
    assert select_ids("not accountEnabled eq true", users) == [U_BOB]


def test_bare_boolean_property_as_condition(messages):
    assert select_ids("isRead", messages) == ["m2"]
    assert select_ids("not isRead", messages) == ["m1", "m3", "m4"]
    assert select_ids("hasAttachments and not isRead", messages) == ["m1", "m4"]


def test_in_operator(users):
    assert select_ids(f"id in ('{U_ALICE}', '{U_DAVE.upper()}')", users) == [U_ALICE, U_DAVE]


def test_in_operator_with_numbers_and_null_and_empty(drive_items):
    assert select_ids("size in (0, 120)", drive_items) == ["d2", "d3"]
    assert select_ids("fields/Status in ('Draft', null)", drive_items) == ["d2", "d3"]
    assert select_ids("size in ()", drive_items) == []


def test_not_in(users):
    assert select_ids(f"not (id in ('{U_ALICE}', '{U_BOB}'))", users) == [U_CAROL, U_DAVE]


def test_long_or_chain_does_not_overflow_the_stack():
    expr = " or ".join(f"id eq 'x{i}'" for i in range(3000))
    predicate = compile_filter(expr)
    assert predicate({"id": "x2999"}) is True
    assert predicate({"id": "nope"}) is False


# --------------------------------------------------------------------------------------------------
# $filter: property paths
# --------------------------------------------------------------------------------------------------


def test_nested_path_message_from_address(messages):
    assert select_ids("from/emailAddress/address eq 'ALICE@contoso.com'", messages) == ["m1"]


def test_nested_path_drive_item_parent_reference(drive_items):
    assert select_ids("parentReference/id eq 'folder-finance'", drive_items) == ["d1", "d2"]


def test_nested_path_list_item_fields(drive_items):
    assert select_ids("fields/Status eq 'approved'", drive_items) == ["d1"]


def test_missing_nested_path_is_null(users):
    assert select_ids("manager/displayName eq null", users) == [U_BOB, U_CAROL, U_DAVE]
    assert select_ids("no/such/path eq null", users) == [U_ALICE, U_BOB, U_CAROL, U_DAVE]


def test_path_through_scalar_is_null(users):
    assert select_ids("displayName/foo eq null", users) == [U_ALICE, U_BOB, U_CAROL, U_DAVE]


def test_drive_item_file_vs_folder_facets(drive_items):
    assert select_ids("file ne null", drive_items) == ["d1", "d2"]
    assert select_ids("folder ne null", drive_items) == ["d3"]


def test_odata_type_annotation_property():
    records = [{"id": "1", "@odata.type": "#microsoft.graph.fileAttachment"}, {"id": "2"}]
    assert select_ids("@odata.type eq '#microsoft.graph.fileAttachment'", records) == ["1"]


def test_type_cast_segment_is_transparent():
    records = [{"id": "1", "mail": "a@x.com"}, {"id": "2", "mail": "b@x.com"}]
    assert select_ids("microsoft.graph.user/mail eq 'a@x.com'", records) == ["1"]


def test_count_segment_on_collections(users):
    assert select_ids("assignedLicenses/$count eq 0", users) == [U_BOB, U_DAVE]
    assert select_ids("assignedLicenses/$count ge 2", users) == [U_CAROL]


# --------------------------------------------------------------------------------------------------
# $filter: datetimes
# --------------------------------------------------------------------------------------------------


def test_unquoted_datetime_literal_against_seven_digit_fraction_without_offset(events):
    assert select_ids("start/dateTime ge 2026-10-15T14:00:00Z", events) == ["e1", "e2", "e3"]
    assert select_ids("start/dateTime gt 2026-10-15T14:00:00Z", events) == ["e2", "e3"]
    assert select_ids("start/dateTime eq 2026-10-15T14:00:00Z", events) == ["e1"]


def test_quoted_datetime_compares_as_datetime(events):
    assert select_ids("start/dateTime eq '2026-10-15T14:00:00Z'", events) == ["e1"]
    assert select_ids("start/dateTime lt '2026-10-16T00:00:00.0000000'", events) == ["e1"]


def test_datetime_range_with_and(events):
    expr = "start/dateTime ge 2026-10-15T00:00:00Z and end/dateTime le 2026-10-16T10:00:00Z"
    assert select_ids(expr, events) == ["e1", "e2"]


def test_seven_digit_fraction_precision_is_truncated_to_microseconds(messages):
    # 07:00:00.0000000Z (m2) equals 07:00:00Z even though the strings differ
    assert select_ids("receivedDateTime eq 2026-10-15T07:00:00Z", messages) == ["m2"]
    assert select_ids("receivedDateTime gt 2026-10-15T07:00:00.0000001Z", messages) == ["m3"]


def test_datetime_offsets_are_honored_not_compared_lexically():
    records = [{"id": "a", "t": "2026-10-15T16:00:00+02:00"}, {"id": "b", "t": "2026-10-15T09:00:00-07:00"}]
    # 16:00+02:00 is 14:00Z; 09:00-07:00 is 16:00Z. Lexical comparison would get both wrong.
    assert select_ids("t eq 2026-10-15T14:00:00Z", records) == ["a"]
    assert select_ids("t gt 2026-10-15T15:00:00Z", records) == ["b"]


def test_date_only_literal_means_midnight_utc(messages):
    assert select_ids("receivedDateTime ge 2026-10-15", messages) == ["m2", "m3"]
    assert select_ids("receivedDateTime lt 2026-10-01", messages) == ["m4"]


def test_datetime_against_missing_value_is_false(users):
    assert select_ids("createdDateTime gt 2000-01-01T00:00:00Z", users) == [U_ALICE, U_BOB, U_CAROL]


def test_non_datetime_string_does_not_match_datetime_literal():
    assert select_ids("t eq 2026-10-15T14:00:00Z", [{"id": "x", "t": "not a date"}]) == []


def test_invalid_calendar_date_literal_is_rejected():
    with pytest.raises(ODataError, match="Invalid filter clause"):
        compile_filter("t eq 2026-13-45T00:00:00Z")


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("year(receivedDateTime) eq 2026 and month(receivedDateTime) eq 9", ["m4"]),
        ("day(receivedDateTime) eq 15", ["m2", "m3"]),
        ("hour(receivedDateTime) eq 12 and minute(receivedDateTime) eq 30", ["m3"]),
        ("date(receivedDateTime) eq 2026-10-15", ["m2", "m3"]),
        ("date(receivedDateTime) lt 2026-10-15", ["m1", "m4"]),
    ],
)
def test_datetime_part_functions(messages, expr, expected):
    assert select_ids(expr, messages) == expected


def test_datetime_part_of_missing_value_is_null(users):
    assert select_ids("year(createdDateTime) eq null", users) == [U_DAVE]


# --------------------------------------------------------------------------------------------------
# $filter: string functions
# --------------------------------------------------------------------------------------------------


def test_startswith_endswith_contains_on_nested_path(messages):
    assert select_ids("startswith(from/emailAddress/address, 'alice')", messages) == ["m1"]
    assert select_ids("endswith(from/emailAddress/address, '@fabrikam.com')", messages) == ["m4"]
    assert select_ids("contains(subject, 'quarterly')", messages) == ["m1", "m3"]


def test_boolean_functions_are_false_for_null_arguments(users):
    assert select_ids("startswith(jobTitle, 'E')", users) == [U_ALICE]
    assert select_ids("contains(jobTitle, 'x')", users) == []
    assert select_ids("not contains(jobTitle, 'x')", users) == [U_ALICE, U_BOB, U_CAROL, U_DAVE]


def test_tolower_toupper_trim(users):
    assert select_ids("toupper(givenName) eq 'ALICE'", users) == [U_ALICE]
    assert select_ids("tolower(department) eq 'sales'", users) == [U_DAVE]
    assert select_ids("trim(subject) eq 'x'", [{"id": "1", "subject": "  x\t"}]) == ["1"]


def test_length(users):
    assert select_ids("length(givenName) eq 5", users) == [U_ALICE, U_CAROL]
    assert select_ids("length(jobTitle) eq null", users) == [U_BOB, U_DAVE]


def test_indexof(users):
    assert select_ids("indexof(displayName, 'ice') eq 2", users) == [U_ALICE]
    assert select_ids("indexof(displayName, 'zzz') eq -1", users) == [U_ALICE, U_BOB, U_CAROL, U_DAVE]


def test_indexof_is_case_sensitive_on_request(users):
    assert select_ids("indexof(displayName, 'ICE') eq 2", users) == [U_ALICE]
    assert select_ids("indexof(displayName, 'ICE') eq 2", users, case_sensitive=True) == []


def test_substring_with_and_without_length(users):
    assert select_ids("substring(givenName, 1) eq 'lice'", users) == [U_ALICE]
    assert select_ids("substring(givenName, 1, 3) eq 'lic'", users) == [U_ALICE]
    assert select_ids("substring(givenName, 10) eq ''", users) == [U_ALICE, U_BOB, U_CAROL]


def test_concat(users):
    assert select_ids("concat(givenName, surname) eq 'AliceAnderson'", users) == [U_ALICE]
    assert select_ids("concat(concat(givenName, ' '), surname) eq 'Bob Baker'", users) == [U_BOB]


def test_functions_compose_with_comparisons(users):
    assert select_ids("tolower(substring(givenName, 0, 1)) eq 'c'", users) == [U_CAROL]


# --------------------------------------------------------------------------------------------------
# $filter: lambda operators
# --------------------------------------------------------------------------------------------------


def test_any_over_primitive_collection_with_function(users):
    assert select_ids("proxyAddresses/any(x:startswith(x,'smtp:alice'))", users) == [U_ALICE]
    assert select_ids("proxyAddresses/any(p: endswith(p, '@fabrikam.com'))", users) == [U_DAVE]


def test_any_over_primitive_collection_with_eq(messages):
    assert select_ids("categories/any(c:c eq 'Red')", messages) == ["m1", "m3"]  # case-insensitive
    assert select_ids("categories/any(c:c eq 'Red')", messages, case_sensitive=True) == ["m1"]


def test_any_over_object_collection_with_nested_path(messages):
    expr = "toRecipients/any(r:r/emailAddress/address eq 'carol@contoso.com')"
    assert select_ids(expr, messages) == ["m1"]
    assert select_ids("toRecipients/any(r:endswith(r/emailAddress/address, '@contoso.com'))", messages) == [
        "m1",
        "m2",
        "m3",
        "m4",
    ]


def test_any_over_guid_property(users):
    assert select_ids(f"assignedLicenses/any(x:x/skuId eq {SKU_F3})", users) == [U_CAROL]
    assert select_ids(f"assignedLicenses/any(x:x/skuId eq {SKU_E5.upper()})", users) == [U_ALICE, U_CAROL]


def test_bare_any_means_non_empty(users, messages):
    assert select_ids("assignedLicenses/any()", users) == [U_ALICE, U_CAROL]
    assert select_ids("not assignedLicenses/any()", users) == [U_BOB, U_DAVE]
    assert select_ids("categories/any()", messages) == ["m1", "m3"]


def test_all_operator_including_vacuous_truth(users):
    assert select_ids("assignedLicenses/all(x:x/skuId eq " + SKU_E5 + ")", users) == [U_ALICE, U_BOB, U_DAVE]
    assert select_ids("proxyAddresses/all(p:endswith(p,'@contoso.com'))", users) == [U_ALICE, U_BOB, U_CAROL]


def test_lambda_body_can_reference_outer_record_properties(messages):
    # `isRead` is not the lambda variable, so it resolves against the message itself
    expr = "toRecipients/any(r:r/emailAddress/address eq 'alice@contoso.com' and isRead eq true)"
    assert select_ids(expr, messages) == ["m2"]


def test_nested_lambdas():
    records = [
        {"id": "1", "teams": [{"members": ["ann", "bo"]}, {"members": ["cy"]}]},
        {"id": "2", "teams": [{"members": ["di"]}]},
    ]
    assert select_ids("teams/any(t:t/members/any(m:m eq 'cy'))", records) == ["1"]
    assert select_ids("teams/all(t:t/members/any(m:startswith(m,'d')))", records) == ["2"]


def test_lambda_on_missing_collection():
    records = [{"id": "1"}, {"id": "2", "categories": ["x"]}]
    assert select_ids("categories/any(c:c eq 'x')", records) == ["2"]
    assert select_ids("categories/all(c:c eq 'x')", records) == ["1", "2"]


def test_lambda_variable_names_are_free_form(messages):
    assert select_ids("categories/any(item: item eq 'Finance')", messages) == ["m1"]


# --------------------------------------------------------------------------------------------------
# $filter: malformed input
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "(displayName eq 'a'",
        "displayName eq 'a')",
        "((displayName eq 'a')",
        "startswith(displayName, 'a'",
        "id in ('a', 'b'",
    ],
)
def test_unbalanced_parentheses(expr):
    with pytest.raises(ODataError, match=r"^Invalid filter clause: "):
        compile_filter(expr)


def test_unknown_function_names_the_function():
    with pytest.raises(ODataError, match=r"Unknown function 'frobnicate'"):
        compile_filter("frobnicate(displayName) eq 'a'")


@pytest.mark.parametrize(
    "expr", ["displayName eq 'a' 'b'", "displayName eq 'a' junk", "displayName eq 'a')", "a b"]
)
def test_trailing_tokens(expr):
    with pytest.raises(ODataError, match=r"Syntax error at position"):
        compile_filter(expr)


@pytest.mark.parametrize(
    "expr", ["displayName eq", "displayName eq 'a' and", "not", "(", "displayName eq ()"]
)
def test_missing_operand(expr):
    with pytest.raises(ODataError, match=r"^Invalid filter clause: "):
        compile_filter(expr)


@pytest.mark.parametrize("expr", ["", "   ", "\t"])
def test_empty_filter(expr):
    with pytest.raises(ODataError, match="Expression expected"):
        compile_filter(expr)


def test_unterminated_string():
    with pytest.raises(ODataError, match="Unterminated string literal"):
        compile_filter("displayName eq 'oops")


def test_unterminated_string_with_escaped_quote():
    with pytest.raises(ODataError, match="Unterminated string literal"):
        compile_filter("displayName eq 'oops''")


@pytest.mark.parametrize(
    "expr",
    ["startswith(displayName)", "tolower()", "substring(a)", "now(1)", "concat(a, b, c)"],
)
def test_wrong_function_arity(expr):
    with pytest.raises(ODataError, match="argument"):
        compile_filter(f"{expr} eq 'a'")


@pytest.mark.parametrize(
    "expr", ["'abc'", "42", "null", "length(displayName)", "displayName eq 'a' and 'b'", "not 'x'"]
)
def test_non_boolean_expression_is_rejected(expr):
    with pytest.raises(ODataError, match="non-boolean"):
        compile_filter(expr)


def test_bare_property_is_accepted_because_it_may_be_boolean():
    assert compile_filter("accountEnabled")({"accountEnabled": True}) is True
    assert compile_filter("accountEnabled")({"accountEnabled": "yes"}) is False


def test_all_without_lambda_is_rejected():
    with pytest.raises(ODataError, match="'all' requires a lambda expression"):
        compile_filter("categories/all()")


@pytest.mark.parametrize(
    "expr", ["categories/any(c eq 'a')", "categories/any(:c eq 'a')", "categories/any(c:)"]
)
def test_malformed_lambda(expr):
    with pytest.raises(ODataError):
        compile_filter(expr)


@pytest.mark.parametrize("expr", ["a eq 1 $ b", "a eq #", "a ~ b", "a eq 1abc", "a eq 12-13-14"])
def test_unexpected_characters(expr):
    with pytest.raises(ODataError, match="Syntax error"):
        compile_filter(expr)


def test_unsupported_operators_are_syntax_errors():
    with pytest.raises(ODataError):
        compile_filter("size add 1 gt 5")


def test_excessive_nesting_is_rejected_not_crashed():
    expr = "(" * 200 + "a eq 1" + ")" * 200
    with pytest.raises(ODataError, match="nested too deeply"):
        compile_filter(expr)


def test_errors_surface_at_compile_time_not_per_record():
    with pytest.raises(ODataError):
        compile_filter("this is not valid")


def test_odata_error_shape():
    err = ODataError("Invalid filter clause: boom")
    assert err.code == "BadRequest"
    assert err.status_code == 400
    assert str(err) == "Invalid filter clause: boom"
    assert isinstance(err, ValueError)
    assert err.to_dict() == {"error": {"code": "BadRequest", "message": "Invalid filter clause: boom"}}
    assert ODataError("x", code="Request_UnsupportedQuery").code == "Request_UnsupportedQuery"


def test_parse_filter_returns_reusable_immutable_ast():
    ast = parse_filter("a eq 1")
    assert parse_filter("a eq 1") is ast  # cached
    with pytest.raises(AttributeError):
        ast.op = "ne"  # frozen


def test_compiled_filter_is_reusable_across_records(users):
    predicate = compile_filter("accountEnabled eq true")
    assert [predicate(u) for u in users] == [True, False, True, True]
    assert [predicate(u) for u in users] == [True, False, True, True]


def test_filter_does_not_mutate_records(users):
    snapshot = copy.deepcopy(users)
    predicate = compile_filter("proxyAddresses/any(x:startswith(x,'smtp:')) or manager/displayName eq 'x'")
    for user in users:
        predicate(user)
    assert users == snapshot


# --------------------------------------------------------------------------------------------------
# $orderby
# --------------------------------------------------------------------------------------------------


def test_parse_orderby_directions():
    items = parse_orderby("receivedDateTime desc, from/emailAddress/address ASC, subject")
    assert [(i.descending) for i in items] == [True, False, False]


@pytest.mark.parametrize("expr", ["", "subject,", "subject sideways", "subject desc desc", ",subject"])
def test_orderby_errors(expr):
    with pytest.raises(ODataError, match=r"^Invalid orderby clause: "):
        parse_orderby(expr)


def test_orderby_default_is_ascending_and_case_insensitive(users):
    result = apply_orderby(users, "displayName")
    assert ids(result) == [U_ALICE, U_BOB, U_CAROL, U_DAVE]  # 'bob baker' sorts among capitals


def test_orderby_case_sensitive_option_puts_lowercase_last(users):
    result = apply_orderby(users, "displayName", case_sensitive=True)
    assert ids(result) == [U_ALICE, U_CAROL, U_DAVE, U_BOB]


def test_orderby_desc(users):
    assert ids(apply_orderby(users, "displayName desc")) == [U_DAVE, U_CAROL, U_BOB, U_ALICE]


def test_orderby_multi_key_with_mixed_directions_and_nulls(users):
    # asc puts nulls first; desc puts them last
    assert ids(apply_orderby(users, "department desc, displayName asc")) == [U_DAVE, U_ALICE, U_CAROL, U_BOB]
    assert ids(apply_orderby(users, "department asc, displayName desc")) == [U_BOB, U_CAROL, U_ALICE, U_DAVE]


def test_orderby_missing_property_sorts_as_null(users):
    # null and missing first (stable), then Director < Engineer
    assert ids(apply_orderby(users, "jobTitle")) == [U_BOB, U_DAVE, U_CAROL, U_ALICE]


def test_orderby_datetimes_across_formats(users):
    # 7-digit fraction, plain Z and missing values all order chronologically; missing first
    assert ids(apply_orderby(users, "createdDateTime")) == [U_DAVE, U_CAROL, U_BOB, U_ALICE]
    assert ids(apply_orderby(users, "createdDateTime desc")) == [U_ALICE, U_BOB, U_CAROL, U_DAVE]


def test_orderby_datetimes_respect_offsets():
    records = [
        {"id": "a", "t": "2026-10-15T16:00:00+02:00"},  # 14:00Z
        {"id": "b", "t": "2026-10-15T09:00:00-07:00"},  # 16:00Z
        {"id": "c", "t": "2026-10-15T15:00:00Z"},
    ]
    assert ids(apply_orderby(records, "t")) == ["a", "c", "b"]


def test_orderby_numbers_are_numeric_not_lexical(drive_items):
    extra = [*drive_items, {"id": "d4", "name": "big.bin", "size": 1000000}]
    assert ids(apply_orderby(extra, "size desc")) == ["d4", "d1", "d2", "d3"]


def test_orderby_nested_path(messages):
    assert ids(apply_orderby(messages, "from/emailAddress/address desc")) == ["m4", "m3", "m2", "m1"]


def test_orderby_is_stable_for_ties(messages):
    assert ids(apply_orderby(messages, "hasAttachments desc")) == ["m1", "m4", "m2", "m3"]


def test_orderby_mixed_types_do_not_raise():
    records = [{"id": "1", "v": "text"}, {"id": "2", "v": 5}, {"id": "3", "v": True}, {"id": "4"}]
    assert ids(apply_orderby(records, "v")) == ["4", "3", "2", "1"]


def test_orderby_accepts_function_expressions(users):
    assert ids(apply_orderby(users, "length(givenName) desc, givenName"))[:2] == [U_ALICE, U_CAROL]


def test_orderby_does_not_mutate_input_list_or_records(users):
    order_before = ids(users)
    snapshot = copy.deepcopy(users)
    apply_orderby(users, "displayName desc")
    assert ids(users) == order_before
    assert users == snapshot


# --------------------------------------------------------------------------------------------------
# $select
# --------------------------------------------------------------------------------------------------


def test_parse_select_trims_dedupes_and_drops_blanks():
    assert parse_select(" id , displayName,,id , manager/id ") == ["id", "displayName", "manager/id"]
    assert parse_select("  ") is None


def test_select_keeps_requested_and_always_keys(users):
    result = select_record(users[0], ["displayName", "mail"], always=("id", "@odata.type", "@odata.etag"))
    assert result == {"id": U_ALICE, "displayName": "Alice Anderson", "mail": "alice@contoso.com"}


def test_select_always_keys_only_if_present():
    record = {"id": "1", "@odata.etag": 'W/"x"', "a": 1, "b": 2}
    assert select_record(record, ["a"], always=("id", "@odata.type", "@odata.etag")) == {
        "id": "1",
        "@odata.etag": 'W/"x"',
        "a": 1,
    }


def test_select_with_empty_always_drops_id(users):
    assert select_record(users[0], ["mail"], always=()) == {"mail": "alice@contoso.com"}


def test_select_omits_unknown_properties(users):
    assert select_record(users[0], ["nope", "mail"], always=()) == {"mail": "alice@contoso.com"}


def test_select_is_case_insensitive_and_preserves_record_key_case(users):
    assert select_record(users[0], ["DISPLAYNAME"], always=()) == {"displayName": "Alice Anderson"}


def test_select_nested_paths_merge(users):
    result = select_record(users[0], ["manager/displayName", "onPremisesExtensionAttributes"], always=())
    assert result == {
        "manager": {"displayName": "Carol O'Brien"},
        "onPremisesExtensionAttributes": {"extensionAttribute1": "Blue"},
    }
    both = select_record(users[0], ["manager/id", "manager/displayName"], always=())
    assert both == {"manager": {"id": U_CAROL, "displayName": "Carol O'Brien"}}


def test_select_whole_parent_wins_over_nested_child(users):
    result = select_record(users[0], ["manager/id", "manager"], always=())
    assert result == {"manager": users[0]["manager"]}
    result = select_record(users[0], ["manager", "manager/id"], always=())
    assert result == {"manager": users[0]["manager"]}


def test_select_nested_path_through_collection_of_objects(messages):
    result = select_record(messages[0], ["toRecipients/emailAddress"], always=())
    assert result == {
        "toRecipients": [
            {"emailAddress": {"address": "bob@contoso.com"}},
            {"emailAddress": {"address": "carol@contoso.com"}},
        ]
    }
    narrow = select_record(messages[0], ["toRecipients/emailAddress/address"], always=())
    assert narrow["toRecipients"][0] == {"emailAddress": {"address": "bob@contoso.com"}}


def test_select_star_and_none_return_everything(users):
    assert select_record(users[0], None) == users[0]
    assert select_record(users[0], []) == users[0]
    assert select_record(users[0], ["*"]) == users[0]
    assert select_record(users[0], ["id", "*"]) == users[0]


def test_select_does_not_mutate_or_alias_input(users):
    snapshot = copy.deepcopy(users)
    projected = select_record(users[0], ["proxyAddresses", "manager"], always=("id",))
    projected["proxyAddresses"].append("smtp:new@contoso.com")
    projected["manager"]["displayName"] = "changed"
    assert users == snapshot


def test_select_without_projection_is_still_a_deep_copy(users):
    copied = select_record(users[0], None)
    copied["manager"]["displayName"] = "changed"
    copied["proxyAddresses"].clear()
    assert users[0]["manager"]["displayName"] == "Carol O'Brien"
    assert users[0]["proxyAddresses"]


def test_apply_select_over_many_records(users):
    result = apply_select(users, ["givenName"], always=("id",))
    assert result[0] == {"id": U_ALICE, "givenName": "Alice"}
    assert result[3] == {"id": U_DAVE}  # Dave has no givenName


# --------------------------------------------------------------------------------------------------
# $expand
# --------------------------------------------------------------------------------------------------


def test_expand_with_nested_options_and_plain_item():
    parsed = parse_expand("members($select=id,displayName;$top=5),manager")
    assert parsed == [
        ExpandItem(path="members", options={"$select": "id,displayName", "$top": "5"}),
        ExpandItem(path="manager", options={}),
    ]


def test_expand_plain_list_and_whitespace():
    parsed = parse_expand(" manager , directReports ,memberOf")
    assert [i.path for i in parsed] == ["manager", "directReports", "memberOf"]
    assert all(i.options == {} for i in parsed)


def test_expand_option_keys_are_normalized():
    parsed = parse_expand("members(Select=id;$TOP=3)")
    assert parsed[0].options == {"$select": "id", "$top": "3"}


def test_expand_option_values_may_contain_quotes_parens_and_separators():
    parsed = parse_expand("children($filter=startswith(name,'a;b,c)');$orderby=name desc,id),owner")
    assert parsed[0].path == "children"
    assert parsed[0].options == {"$filter": "startswith(name,'a;b,c)')", "$orderby": "name desc,id"}
    assert parsed[1] == ExpandItem("owner", {})


def test_expand_nested_expand_is_kept_raw_and_parseable():
    parsed = parse_expand("children($select=id;$expand=thumbnails($top=1)),parent")
    assert parsed[0].options["$expand"] == "thumbnails($top=1)"
    nested = parse_expand(parsed[0].options["$expand"])
    assert nested == [ExpandItem("thumbnails", {"$top": "1"})]


def test_expand_empty_parentheses_and_levels():
    assert parse_expand("members()") == [ExpandItem("members", {})]
    assert parse_expand("children($levels=max)") == [ExpandItem("children", {"$levels": "max"})]


def test_expand_blank_is_empty_list():
    assert parse_expand("") == []
    assert parse_expand("   ") == []


@pytest.mark.parametrize(
    "value",
    [
        "members($select=id",
        "members)",
        "members($top)",
        "a,,b",
        "a,",
        "(x=1)",
        "members($top=1)junk",
        "a($filter='x)",
    ],
)
def test_expand_errors(value):
    with pytest.raises(ODataError, match=r"^Invalid expand clause: "):
        parse_expand(value)


def test_expand_item_parse_options_reuses_query_option_validation():
    item = parse_expand("members($select=id,displayName;$top=5;$orderby=displayName desc)")[0]
    nested = item.parse_options()
    assert nested.select == ["id", "displayName"]
    assert nested.top == 5
    assert nested.orderby == "displayName desc"
    with pytest.raises(ODataError):
        parse_expand("members($top=abc)")[0].parse_options()


def test_expand_result_is_isolated_from_the_parse_cache():
    first = parse_expand("members($top=1)")
    first[0].options["$top"] = "99"
    assert parse_expand("members($top=1)")[0].options == {"$top": "1"}


# --------------------------------------------------------------------------------------------------
# $top / $skip / $count / parse_query_options
# --------------------------------------------------------------------------------------------------


def test_defaults():
    opts = parse_query_options({})
    assert opts == QueryOptions(
        filter=None,
        orderby=None,
        select=None,
        expand=[],
        top=None,
        skip=0,
        count=False,
        search=None,
        skiptoken=None,
    )


def test_keys_with_and_without_dollar_prefix_any_case():
    with_prefix = parse_query_options({"$top": "5", "$skip": "2", "$count": "true"})
    without = parse_query_options({"top": "5", "skip": "2", "count": "true"})
    mixed = parse_query_options({"$TOP": "5", "Skip": "2", "$Count": "TRUE"})
    assert with_prefix == without == mixed
    assert (with_prefix.top, with_prefix.skip, with_prefix.count) == (5, 2, True)


def test_full_option_parse():
    opts = parse_query_options(
        {
            "$filter": " startswith(displayName,'a') ",
            "$orderby": "displayName desc",
            "$select": "id, displayName",
            "$expand": "manager($select=id)",
            "$top": "10",
            "$skip": "20",
            "$count": "false",
            "$search": '"displayName:ali"',
            "$skiptoken": "abc123",
        }
    )
    assert opts.filter == "startswith(displayName,'a')"
    assert opts.orderby == "displayName desc"
    assert opts.select == ["id", "displayName"]
    assert opts.expand == [ExpandItem("manager", {"$select": "id"})]
    assert (opts.top, opts.skip, opts.count) == (10, 20, False)
    assert opts.search == '"displayName:ali"'
    assert opts.skiptoken == "abc123"


def test_top_zero_is_valid():
    assert parse_query_options({"$top": "0"}).top == 0


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "", " ", "+5", "1e3", "٣"])
def test_invalid_top(bad):
    with pytest.raises(ODataError, match=r"\$top"):
        parse_query_options({"$top": bad})


@pytest.mark.parametrize("bad", ["abc", "-1", "2.0", ""])
def test_invalid_skip(bad):
    with pytest.raises(ODataError, match=r"\$skip"):
        parse_query_options({"$skip": bad})


@pytest.mark.parametrize("bad", ["yes", "1", "", "tru"])
def test_invalid_count(bad):
    with pytest.raises(ODataError, match=r"\$count"):
        parse_query_options({"$count": bad})


def test_unknown_parameters_are_ignored():
    opts = parse_query_options({"api-version": "1.0", "$format": "json", "foo": "bar", "$top": "3"})
    assert opts.top == 3


def test_blank_text_options_are_treated_as_absent():
    opts = parse_query_options(
        {"$filter": "  ", "$orderby": "", "$search": " ", "$select": " ", "$expand": ""}
    )
    assert opts == QueryOptions()


def test_bad_filter_is_reported_even_without_records():
    with pytest.raises(ODataError, match="Invalid filter clause"):
        parse_query_options({"$filter": "displayName eq"})


def test_bad_orderby_expand_and_search_are_reported_eagerly():
    with pytest.raises(ODataError, match="Invalid orderby clause"):
        parse_query_options({"$orderby": "name,"})
    with pytest.raises(ODataError, match="Invalid expand clause"):
        parse_query_options({"$expand": "members($top=1"})
    with pytest.raises(ODataError, match="Invalid search clause"):
        parse_query_options({"$search": '"unterminated'})


def test_multi_valued_parameters_use_the_last_value():
    assert parse_query_options({"$top": ["1", "7"]}).top == 7  # type: ignore[dict-item]


# --------------------------------------------------------------------------------------------------
# $search
# --------------------------------------------------------------------------------------------------

SEARCH_FIELDS = ("displayName", "mail", "proxyAddresses")


def search_ids(expr: str, users: list[dict[str, Any]], fields=SEARCH_FIELDS) -> list[str]:
    predicate = compile_search(expr, fields)
    return [u["id"] for u in users if predicate(u)]


def test_search_quoted_term_is_case_insensitive_substring(users):
    assert search_ids('"ALICE"', users) == [U_ALICE]
    assert search_ids('"o\'brien"', users) == [U_CAROL]
    assert search_ids('"an"', users) == [U_ALICE]  # Anderson; "Dave Dubois" has no "an"


def test_search_unquoted_term(users):
    assert search_ids("bob", users) == [U_BOB]


def test_search_covers_all_fields_including_collections_and_skips_nulls(users):
    assert search_ids('"fabrikam"', users) == [U_DAVE]  # mail and proxyAddresses
    assert search_ids('"cobrien"', users) == [U_CAROL]  # second proxy address only


def test_search_property_scoped_term_restricts_to_that_property(users):
    assert search_ids('"displayName:alice"', users) == [U_ALICE]
    assert search_ids('"mail:alice"', users) == [U_ALICE]
    assert search_ids('"displayName:contoso"', users) == []  # contoso only appears in mail/proxy


def test_search_only_the_first_colon_separates_property_from_term(users):
    assert search_ids('"proxyAddresses:smtp:cobrien"', users) == [U_CAROL]
    assert search_ids('"proxyAddresses:cobrien"', users) == [U_CAROL]


def test_search_property_scoped_term_on_nested_path(messages):
    predicate = compile_search('"from/emailAddress/address:fabrikam"', ("subject",))
    assert [m["id"] for m in messages if predicate(m)] == ["m4"]


def test_search_property_scoped_term_with_quoted_phrase(users):
    assert search_ids('displayName:"bob baker"', users) == [U_BOB]
    assert search_ids('"displayName:bob baker"', users) == [U_BOB]


def test_search_or(users):
    assert search_ids('"displayName:alice" OR "displayName:dave"', users) == [U_ALICE, U_DAVE]


def test_search_and(users):
    assert search_ids('"contoso" AND "carol"', users) == [U_CAROL]
    assert search_ids('"contoso" AND "fabrikam"', users) == []


def test_search_implicit_and_between_adjacent_terms(users):
    assert search_ids('"contoso" "carol"', users) == [U_CAROL]
    assert search_ids("contoso carol", users) == [U_CAROL]


def test_search_not(users):
    assert search_ids('"contoso" AND NOT "alice"', users) == [U_BOB, U_CAROL]
    assert search_ids('NOT "contoso"', users) == [U_DAVE]


def test_search_parentheses_and_precedence(users):
    # AND binds tighter than OR
    assert search_ids('"alice" OR "bob" AND "fabrikam"', users) == [U_ALICE]
    assert search_ids('("alice" OR "bob") AND "contoso"', users) == [U_ALICE, U_BOB]


def test_search_lowercase_and_or_are_plain_terms(users):
    assert search_ids('"alice" or "bob"', users) == []  # implicit AND of three terms: alice, or, bob


def test_search_phrase_with_spaces_and_escaped_quote():
    records = [{"id": "1", "displayName": 'The "Big" Team'}, {"id": "2", "displayName": "Big Team"}]
    predicate = compile_search('"\\"Big\\" Team"', ("displayName",))
    assert [r["id"] for r in records if predicate(r)] == ["1"]
    assert [r["id"] for r in records if compile_search('"BIG\\" TEAM"', ("displayName",))(r)] == ["1"]
    assert [r["id"] for r in records if compile_search('"big team"', ("displayName",))(r)] == ["2"]


def test_search_with_no_fields_matches_nothing_for_unscoped_terms(users):
    assert search_ids('"alice"', users, fields=()) == []
    assert search_ids('"mail:alice"', users, fields=()) == [U_ALICE]


def test_search_numbers_are_searchable_as_text(drive_items):
    predicate = compile_search('"20480"', ("size",))
    assert [d["id"] for d in drive_items if predicate(d)] == ["d1"]


@pytest.mark.parametrize("expr", ["", '""', '"abc', '("a"', '"a" OR', "OR", '"a" AND', ")", "NOT"])
def test_search_errors(expr):
    with pytest.raises(ODataError, match=r"^Invalid search clause: "):
        compile_search(expr)


# --------------------------------------------------------------------------------------------------
# apply_query
# --------------------------------------------------------------------------------------------------


def test_apply_query_no_options_returns_everything_as_copies(users):
    result = apply_query(users, QueryOptions())
    assert result.total == 4
    assert result.items == users
    assert all(a is not b for a, b in zip(result.items, users, strict=True))


def test_apply_query_filter_orderby_paging_select(users):
    opts = parse_query_options(
        {
            "$filter": "accountEnabled eq true",
            "$orderby": "displayName desc",
            "$skip": "1",
            "$top": "1",
            "$select": "displayName",
        }
    )
    result = apply_query(users, opts)
    assert result.total == 3  # Alice, Carol, Dave match the filter; total ignores skip/top
    assert result.items == [{"id": U_CAROL, "displayName": "Carol O'Brien"}]


def test_apply_query_total_is_after_filter_and_search_but_before_paging(users):
    opts = parse_query_options({"$filter": "department eq 'Engineering'", "$search": '"carol"', "$top": "0"})
    result = apply_query(users, opts)
    assert result.total == 1
    assert result.items == []


def test_apply_query_filter_runs_before_search_and_both_apply(users):
    opts = parse_query_options({"$filter": "accountEnabled eq true", "$search": '"contoso"'})
    result = apply_query(users, opts, search_fields=("mail", "displayName"), select_always=("id",))
    assert ids(result.items) == [U_ALICE, U_CAROL]
    assert result.total == 2


def test_apply_query_search_uses_given_fields(users):
    opts = parse_query_options({"$search": '"fabrikam"'})
    assert apply_query(users, opts).total == 0  # default search field is displayName only
    assert apply_query(users, opts, search_fields=("mail",)).total == 1


def test_apply_query_orders_before_paging(users):
    opts = parse_query_options({"$orderby": "displayName", "$top": "2"})
    assert ids(apply_query(users, opts).items) == [U_ALICE, U_BOB]
    opts = parse_query_options({"$orderby": "displayName", "$skip": "3"})
    assert ids(apply_query(users, opts).items) == [U_DAVE]


def test_apply_query_skip_beyond_end_and_top_beyond_end(users):
    assert apply_query(users, parse_query_options({"$skip": "10"})).items == []
    assert apply_query(users, parse_query_options({"$skip": "10"})).total == 4
    assert len(apply_query(users, parse_query_options({"$top": "100"})).items) == 4


def test_apply_query_select_always_keys(users):
    opts = parse_query_options({"$select": "displayName"})
    default = apply_query(users, opts)
    assert default.items[0] == {"id": U_ALICE, "displayName": "Alice Anderson"}
    custom = apply_query(
        [{"id": "1", "@odata.type": "#microsoft.graph.user", "displayName": "x", "mail": "m"}],
        opts,
        select_always=("id", "@odata.type"),
    )
    assert custom.items == [{"id": "1", "@odata.type": "#microsoft.graph.user", "displayName": "x"}]


def test_apply_query_case_sensitive_flag_reaches_filter_and_orderby(users):
    opts = parse_query_options({"$filter": "startswith(displayName,'BOB')", "$orderby": "displayName"})
    assert apply_query(users, opts).total == 1
    assert apply_query(users, opts, case_sensitive=True).total == 0


def test_apply_query_accepts_generators(users):
    result = apply_query((u for u in users), parse_query_options({"$top": "1"}))
    assert ids(result.items) == [U_ALICE]


def test_apply_query_never_mutates_input_records(users, messages, events, drive_items):
    for dataset, params in [
        (
            users,
            {
                "$filter": "proxyAddresses/any(x:startswith(x,'smtp'))",
                "$orderby": "displayName",
                "$select": "mail",
            },
        ),
        (messages, {"$filter": "categories/any(c:c eq 'Red')", "$orderby": "receivedDateTime desc"}),
        (events, {"$filter": "start/dateTime ge 2026-10-15T00:00:00Z", "$select": "start/dateTime"}),
        (drive_items, {"$filter": "file ne null", "$orderby": "size desc", "$select": "parentReference/id"}),
    ]:
        snapshot = copy.deepcopy(dataset)
        result = apply_query(dataset, parse_query_options(params))
        for item in result.items:  # callers annotate results, so mutate them to prove they are copies
            item["@odata.etag"] = "changed"
            for value in item.values():
                if isinstance(value, dict):
                    value["zzz"] = 1
                elif isinstance(value, list):
                    value.append("zzz")
        assert dataset == snapshot


def test_apply_query_realistic_unread_mail_with_attachments_page(messages):
    opts = parse_query_options(
        {
            "$filter": "isRead eq false and hasAttachments eq true and receivedDateTime ge 2026-10-01T00:00:00Z",
            "$orderby": "receivedDateTime desc",
            "$select": "subject,receivedDateTime",
            "$top": "10",
        }
    )
    result = apply_query(messages, opts)
    assert result.total == 1
    assert result.items == [
        {"id": "m1", "subject": "Quarterly report", "receivedDateTime": "2026-10-14T09:15:00Z"}
    ]


def test_apply_query_realistic_calendar_view_window(events):
    opts = parse_query_options(
        {
            "filter": "start/dateTime ge '2026-10-15T00:00:00Z' and start/dateTime lt '2026-10-17T00:00:00Z'",
            "orderby": "start/dateTime desc",
            "select": "subject",
        }
    )
    result = apply_query(events, opts)
    assert result.items == [{"id": "e2", "subject": "Planning"}, {"id": "e1", "subject": "Standup"}]
    assert result.total == 2


def test_apply_query_realistic_drive_children_listing(drive_items):
    opts = parse_query_options({"$filter": "file ne null and size gt 100", "$orderby": "name"})
    result = apply_query(drive_items, opts)
    assert ids(result.items) == ["d1", "d2"]  # 'Budget.xlsx' < 'notes.TXT' case-insensitively
    assert result.items[0]["parentReference"]["id"] == "folder-finance"


def test_apply_query_propagates_odata_error_for_bad_filter(users):
    opts = QueryOptions(filter="displayName eq")
    with pytest.raises(ODataError):
        apply_query(users, opts)


# --------------------------------------------------------------------------------------------------
# Additional edge cases
# --------------------------------------------------------------------------------------------------


def test_unquoted_datetime_literal_with_numeric_offset():
    records = [
        {"id": "a", "t": "2026-10-15T14:00:00.0000000"},
        {"id": "b", "t": "2026-10-15T20:00:00.0000000"},
    ]
    assert select_ids("t eq 2026-10-15T16:00:00+02:00", records) == ["a"]
    assert select_ids("t gt 2026-10-15T09:00:00-05:00", records) == ["b"]


def test_now_function(messages):
    assert select_ids("receivedDateTime lt now()", messages) == ["m1", "m2", "m3", "m4"]
    assert select_ids("receivedDateTime gt now()", messages) == []


def test_non_ascii_strings_fold_case():
    records = [{"id": "z", "displayName": "Zoë Müller"}]
    assert select_ids("displayName eq 'ZOË MÜLLER'", records) == ["z"]
    assert select_ids("startswith(displayName, 'zoë')", records) == ["z"]
    assert select_ids("displayName eq 'ZOË MÜLLER'", records, case_sensitive=True) == []


def test_deeply_chained_not_is_rejected_cleanly():
    with pytest.raises(ODataError, match="nested too deeply"):
        compile_filter("not " * 200 + "a eq 1")


def test_apply_orderby_accepts_preparsed_items(users):
    items = parse_orderby("displayName desc")
    assert ids(apply_orderby(users, items)) == [U_DAVE, U_CAROL, U_BOB, U_ALICE]


def test_apply_orderby_rejects_bad_text(users):
    with pytest.raises(ODataError):
        apply_orderby(users, "displayName,")


def test_apply_query_leaves_expand_to_the_caller(users):
    opts = parse_query_options({"$expand": "manager($select=id)", "$select": "displayName", "$top": "1"})
    result = apply_query(users, opts)
    assert result.items == [{"id": U_ALICE, "displayName": "Alice Anderson"}]
    assert opts.expand == [ExpandItem("manager", {"$select": "id"})]


def test_query_options_are_independent_instances():
    first, second = parse_query_options({}), parse_query_options({})
    first.expand.append(ExpandItem("x", {}))
    assert second.expand == []
