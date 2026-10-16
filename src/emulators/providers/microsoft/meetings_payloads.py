"""Validate meeting settings before any stored meeting is changed."""

from __future__ import annotations

from typing import Any

from emulators.providers.microsoft import common

BOOLEANS = (
    "isEntryExitAnnounced",
    "allowAttendeeToEnableMic",
    "allowAttendeeToEnableCamera",
    "recordAutomatically",
    "allowRecording",
    "allowTranscription",
)
ENUMS = {
    "allowedPresenters": ("everyone", "organization", "roleIsPresenter", "organizer"),
    "allowMeetingChat": ("enabled", "disabled", "limited"),
}


def validate(payload: dict[str, Any]) -> None:
    for key in BOOLEANS:
        if key in payload and not isinstance(payload[key], bool):
            raise common.graph_error(400, "BadRequest", f"{key} must be a Boolean.")
    for key, choices in ENUMS.items():
        if key in payload and payload[key] not in choices:
            raise common.graph_error(400, "BadRequest", f"Invalid {key}.")
    if "lobbyBypassSettings" in payload:
        lobby = payload["lobbyBypassSettings"]
        if not isinstance(lobby, dict):
            raise common.graph_error(400, "BadRequest", "lobbyBypassSettings must be an object.")
        if "scope" in lobby and lobby["scope"] not in (
            "organizer",
            "organization",
            "organizationAndFederated",
            "everyone",
            "invited",
            "organizationExcludingGuests",
        ):
            raise common.graph_error(400, "BadRequest", "Invalid lobby bypass scope.")
        if "isDialInBypassEnabled" in lobby and not isinstance(lobby["isDialInBypassEnabled"], bool):
            raise common.graph_error(400, "BadRequest", "isDialInBypassEnabled must be a Boolean.")
    if "participants" in payload:
        participants = payload["participants"]
        if not isinstance(participants, dict) or not isinstance(participants.get("attendees", []), list):
            raise common.graph_error(400, "BadRequest", "participants.attendees must be an array.")
        for attendee in participants.get("attendees", []):
            if not isinstance(attendee, dict) or attendee.get("role", "attendee") not in (
                "attendee",
                "presenter",
                "coorganizer",
            ):
                raise common.graph_error(400, "BadRequest", "Invalid attendee role.")


def apply_roles(record: dict[str, Any], payload: dict[str, Any]) -> None:
    """Keep explicitly selected presenters when mapping world identities to the wire."""
    source = payload.get("participants", {}).get("attendees", [])
    by_identifier = {}
    for attendee in source:
        identifier = attendee.get("identity", {}).get("user", {}).get("id") or attendee.get("upn")
        by_identifier[identifier] = attendee.get("role", "attendee")
    for attendee in record["participants"]["attendees"]:
        attendee["role"] = by_identifier.get(
            attendee["identity"]["user"]["id"], by_identifier.get(attendee["upn"], "attendee")
        )
