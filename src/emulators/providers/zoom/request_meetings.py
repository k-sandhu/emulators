"""Request constraints from Zoom's API Hub OpenAPI schemas (audited 2026-10-16).

Source: https://developers.zoom.us/api-hub/meetings/methods/endpoints.json
Descriptions/examples are omitted; only validation constraints for implemented writes remain.
"""

from __future__ import annotations

from typing import Any

SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/v2/users/{user_id}/meetings"): {
        "type": "object",
        "properties": {
            "agenda": {"maxLength": 2000, "type": "string"},
            "default_password": {"type": "boolean"},
            "duration": {"maximum": 1440, "minimum": 1, "type": "integer"},
            "password": {"maxLength": 10, "type": "string"},
            "pre_schedule": {"type": "boolean"},
            "recurrence": {
                "required": ["type"],
                "type": "object",
                "properties": {
                    "end_date_time": {"type": "string", "format": "date-time"},
                    "end_times": {"maximum": 60, "type": "integer"},
                    "monthly_day": {"type": "integer"},
                    "monthly_week": {"type": "integer", "enum": [-1, 1, 2, 3, 4]},
                    "monthly_week_day": {"type": "integer", "enum": [1, 2, 3, 4, 5, 6, 7]},
                    "repeat_interval": {"type": "integer"},
                    "type": {"type": "integer", "enum": [1, 2, 3]},
                    "weekly_days": {"type": "string"},
                },
            },
            "schedule_for": {"type": "string"},
            "settings": {
                "type": "object",
                "properties": {
                    "additional_data_center_regions": {"type": "array", "items": {"type": "string"}},
                    "allow_multiple_devices": {"type": "boolean"},
                    "alternative_hosts": {"type": "string"},
                    "alternative_hosts_email_notification": {"type": "boolean"},
                    "approval_type": {"type": "integer", "enum": [0, 1, 2]},
                    "approved_or_denied_countries_or_regions": {
                        "type": "object",
                        "properties": {
                            "approved_list": {"type": "array", "items": {"type": "string"}},
                            "denied_list": {"type": "array", "items": {"type": "string"}},
                            "enable": {"type": "boolean"},
                            "method": {"type": "string", "enum": ["approve", "deny"]},
                        },
                    },
                    "audio": {"type": "string", "enum": ["both", "telephony", "voip", "thirdParty"]},
                    "audio_conference_info": {"maxLength": 2048, "type": "string"},
                    "authentication_domains": {"type": "string"},
                    "authentication_exception": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string", "format": "email"},
                                "name": {"type": "string"},
                            },
                        },
                    },
                    "authentication_option": {"type": "string"},
                    "auto_recording": {"type": "string", "enum": ["local", "cloud", "none"]},
                    "host_pause_stop_recording": {"type": "boolean"},
                    "auto_add_recording_to_video_management": {
                        "required": ["enable"],
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "channels": {
                                "maxItems": 5,
                                "minItems": 1,
                                "type": "array",
                                "items": {
                                    "required": ["channel_id"],
                                    "type": "object",
                                    "properties": {
                                        "channel_id": {"type": "string"},
                                        "name": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "breakout_room": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "rooms": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "participants": {"type": "array", "items": {"type": "string"}},
                                    },
                                },
                            },
                        },
                    },
                    "calendar_type": {"type": "integer", "enum": [1, 2]},
                    "close_registration": {"type": "boolean"},
                    "cn_meeting": {"type": "boolean"},
                    "contact_email": {"type": "string"},
                    "contact_name": {"type": "string"},
                    "email_notification": {"type": "boolean"},
                    "encryption_type": {"type": "string", "enum": ["enhanced_encryption", "e2ee"]},
                    "focus_mode": {"type": "boolean"},
                    "global_dial_in_countries": {"type": "array", "items": {"type": "string"}},
                    "host_video": {"type": "boolean"},
                    "in_meeting": {"type": "boolean"},
                    "jbh_time": {"type": "integer", "enum": [0, 5, 10, 15]},
                    "join_before_host": {"type": "boolean"},
                    "question_and_answer": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "allow_submit_questions": {"type": "boolean"},
                            "allow_anonymous_questions": {"type": "boolean"},
                            "question_visibility": {"type": "string", "enum": ["answered", "all"]},
                            "attendees_can_comment": {"type": "boolean"},
                            "attendees_can_upvote": {"type": "boolean"},
                        },
                    },
                    "language_interpretation": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "interpreters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "email": {"type": "string", "format": "email"},
                                        "languages": {"type": "string"},
                                        "interpreter_languages": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "sign_language_interpretation": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "interpreters": {
                                "maximum": 20,
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "email": {"type": "string", "format": "email"},
                                        "sign_language": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "meeting_authentication": {"type": "boolean"},
                    "meeting_invitees": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"email": {"type": "string", "format": "email"}},
                        },
                    },
                    "mute_upon_entry": {"type": "boolean"},
                    "participant_video": {"type": "boolean"},
                    "private_meeting": {"type": "boolean"},
                    "registrants_confirmation_email": {"type": "boolean"},
                    "registrants_email_notification": {"type": "boolean"},
                    "registration_type": {"type": "integer", "enum": [1, 2, 3]},
                    "show_share_button": {"type": "boolean"},
                    "show_join_info": {"type": "boolean"},
                    "use_pmi": {"type": "boolean"},
                    "waiting_room": {"type": "boolean"},
                    "waiting_room_options": {
                        "required": ["mode"],
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["follow_setting", "custom"]},
                            "who_goes_to_waiting_room": {
                                "type": "string",
                                "enum": [
                                    "everyone",
                                    "users_not_in_account",
                                    "users_not_in_account_or_whitelisted_domains",
                                    "users_not_on_invite",
                                    "users_not_in_org",
                                ],
                            },
                        },
                    },
                    "watermark": {"type": "boolean"},
                    "host_save_video_order": {"type": "boolean"},
                    "alternative_host_update_polls": {"type": "boolean"},
                    "alternative_host_manage_meeting_summary": {"type": "boolean"},
                    "alternative_host_manage_cloud_recording": {"type": "boolean"},
                    "internal_meeting": {"type": "boolean"},
                    "continuous_meeting_chat": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "auto_add_invited_external_users": {"type": "boolean"},
                            "auto_add_meeting_participants": {"type": "boolean"},
                        },
                    },
                    "participant_focused_meeting": {"type": "boolean"},
                    "push_change_to_calendar": {"type": "boolean"},
                    "resources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "resource_type": {"type": "string", "enum": ["whiteboard"]},
                                "resource_id": {"type": "string"},
                                "permission_level": {
                                    "type": "string",
                                    "enum": ["editor", "commenter", "viewer"],
                                },
                            },
                        },
                    },
                    "auto_start_meeting_summary": {"type": "boolean"},
                    "who_will_receive_summary": {"type": "integer", "enum": [1, 2, 3, 4]},
                    "auto_start_ai_companion_questions": {"type": "boolean"},
                    "who_can_ask_questions": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "summary_template_id": {"type": "string"},
                    "device_testing": {"type": "boolean"},
                    "request_permission_to_unmute_participants": {"type": "boolean"},
                    "allow_host_control_participant_mute_state": {"type": "boolean"},
                    "disable_participant_video": {"type": "boolean"},
                    "email_in_attendee_report": {"type": "boolean"},
                    "auto_start_deepfake_detection": {"type": "boolean"},
                    "prevent_screen_capture": {"type": "boolean"},
                    "disable_recording_control": {"type": "string", "enum": ["none", "all"]},
                },
            },
            "start_time": {"type": "string", "format": "date-time"},
            "sensitivity_label_id": {"type": "string"},
            "template_id": {"type": "string"},
            "timezone": {"type": "string"},
            "topic": {"maxLength": 200, "type": "string"},
            "tracking_fields": {
                "type": "array",
                "items": {
                    "required": ["field"],
                    "type": "object",
                    "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
                },
            },
            "type": {"type": "integer", "enum": [1, 2, 3, 8, 10]},
        },
    },
    ("PATCH", "/v2/meetings/{meeting_id}"): {
        "type": "object",
        "properties": {
            "agenda": {"maxLength": 2000, "type": "string"},
            "duration": {"maximum": 1440, "minimum": 1, "type": "integer"},
            "password": {"maxLength": 10, "type": "string"},
            "pre_schedule": {"type": "boolean"},
            "schedule_for": {"type": "string"},
            "recurrence": {
                "required": ["type"],
                "type": "object",
                "properties": {
                    "end_date_time": {"type": "string", "format": "date-time"},
                    "end_times": {"maximum": 60, "type": "integer"},
                    "monthly_day": {"type": "integer"},
                    "monthly_week": {"type": "integer", "enum": [-1, 1, 2, 3, 4]},
                    "monthly_week_day": {"type": "integer", "enum": [1, 2, 3, 4, 5, 6, 7]},
                    "repeat_interval": {"type": "integer"},
                    "type": {"type": "integer", "enum": [1, 2, 3]},
                    "weekly_days": {"type": "string"},
                },
            },
            "settings": {
                "type": "object",
                "properties": {
                    "allow_multiple_devices": {"type": "boolean"},
                    "alternative_hosts": {"type": "string"},
                    "alternative_hosts_email_notification": {"type": "boolean"},
                    "alternative_host_update_polls": {"type": "boolean"},
                    "alternative_host_manage_meeting_summary": {"type": "boolean"},
                    "alternative_host_manage_cloud_recording": {"type": "boolean"},
                    "approval_type": {"type": "integer", "enum": [0, 1, 2]},
                    "approved_or_denied_countries_or_regions": {
                        "type": "object",
                        "properties": {
                            "approved_list": {"type": "array", "items": {"type": "string"}},
                            "denied_list": {"type": "array", "items": {"type": "string"}},
                            "enable": {"type": "boolean"},
                            "method": {"type": "string", "enum": ["approve", "deny"]},
                        },
                    },
                    "audio": {"type": "string", "enum": ["both", "telephony", "voip", "thirdParty"]},
                    "audio_conference_info": {"maxLength": 2048, "type": "string"},
                    "authentication_domains": {"type": "string"},
                    "authentication_exception": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string", "format": "email"},
                                "name": {"type": "string"},
                                "join_url": {"type": "string"},
                            },
                        },
                    },
                    "authentication_name": {"type": "string"},
                    "authentication_option": {"type": "string"},
                    "auto_recording": {"type": "string", "enum": ["local", "cloud", "none"]},
                    "host_pause_stop_recording": {"type": "boolean"},
                    "auto_add_recording_to_video_management": {
                        "required": ["enable"],
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "channels": {
                                "maxItems": 5,
                                "minItems": 1,
                                "type": "array",
                                "items": {
                                    "required": ["channel_id"],
                                    "type": "object",
                                    "properties": {
                                        "channel_id": {"type": "string"},
                                        "name": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "breakout_room": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "rooms": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "participants": {"type": "array", "items": {"type": "string"}},
                                    },
                                },
                            },
                        },
                    },
                    "calendar_type": {"type": "integer", "enum": [1, 2]},
                    "close_registration": {"type": "boolean"},
                    "cn_meeting": {"type": "boolean"},
                    "contact_email": {"type": "string"},
                    "contact_name": {"type": "string"},
                    "custom_keys": {
                        "maxItems": 10,
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"maxLength": 64, "type": "string"},
                                "value": {"maxLength": 256, "type": "string"},
                            },
                        },
                    },
                    "email_notification": {"type": "boolean"},
                    "encryption_type": {"type": "string", "enum": ["enhanced_encryption", "e2ee"]},
                    "enforce_login": {"type": "boolean"},
                    "enforce_login_domains": {"type": "string"},
                    "focus_mode": {"type": "boolean"},
                    "global_dial_in_countries": {"type": "array", "items": {"type": "string"}},
                    "global_dial_in_numbers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "city": {"type": "string"},
                                "country": {"type": "string"},
                                "country_name": {"type": "string"},
                                "number": {"type": "string"},
                                "type": {"type": "string", "enum": ["toll", "tollfree"]},
                            },
                        },
                    },
                    "host_video": {"type": "boolean"},
                    "in_meeting": {"type": "boolean"},
                    "jbh_time": {"type": "integer", "enum": [0, 5, 10, 15]},
                    "join_before_host": {"type": "boolean"},
                    "question_and_answer": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "allow_submit_questions": {"type": "boolean"},
                            "allow_anonymous_questions": {"type": "boolean"},
                            "question_visibility": {"type": "string", "enum": ["answered", "all"]},
                            "attendees_can_comment": {"type": "boolean"},
                            "attendees_can_upvote": {"type": "boolean"},
                        },
                    },
                    "language_interpretation": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "interpreters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "email": {"type": "string", "format": "email"},
                                        "languages": {"type": "string"},
                                        "interpreter_languages": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "sign_language_interpretation": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "interpreters": {
                                "maximum": 20,
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "email": {"type": "string", "format": "email"},
                                        "sign_language": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "meeting_authentication": {"type": "boolean"},
                    "meeting_invitees": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"email": {"type": "string", "format": "email"}},
                        },
                    },
                    "mute_upon_entry": {"type": "boolean"},
                    "participant_video": {"type": "boolean"},
                    "private_meeting": {"type": "boolean"},
                    "registrants_confirmation_email": {"type": "boolean"},
                    "registrants_email_notification": {"type": "boolean"},
                    "registration_type": {"type": "integer", "enum": [1, 2, 3]},
                    "show_share_button": {"type": "boolean"},
                    "use_pmi": {"type": "boolean"},
                    "waiting_room": {"type": "boolean"},
                    "waiting_room_options": {
                        "required": ["mode"],
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["follow_setting", "custom"]},
                            "who_goes_to_waiting_room": {
                                "type": "string",
                                "enum": [
                                    "everyone",
                                    "users_not_in_account",
                                    "users_not_in_account_or_whitelisted_domains",
                                    "users_not_on_invite",
                                    "users_not_in_org",
                                ],
                            },
                        },
                    },
                    "watermark": {"type": "boolean"},
                    "host_save_video_order": {"type": "boolean"},
                    "internal_meeting": {"type": "boolean"},
                    "continuous_meeting_chat": {
                        "type": "object",
                        "properties": {
                            "enable": {"type": "boolean"},
                            "auto_add_invited_external_users": {"type": "boolean"},
                            "auto_add_meeting_participants": {"type": "boolean"},
                        },
                    },
                    "participant_focused_meeting": {"type": "boolean"},
                    "push_change_to_calendar": {"type": "boolean"},
                    "resources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "resource_type": {"type": "string", "enum": ["whiteboard"]},
                                "resource_id": {"type": "string"},
                                "permission_level": {
                                    "type": "string",
                                    "enum": ["editor", "commenter", "viewer"],
                                },
                            },
                        },
                    },
                    "auto_start_meeting_summary": {"type": "boolean"},
                    "who_will_receive_summary": {"type": "integer", "enum": [1, 2, 3, 4]},
                    "auto_start_ai_companion_questions": {"type": "boolean"},
                    "who_can_ask_questions": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "summary_template_id": {"type": "string"},
                    "device_testing": {"type": "boolean"},
                    "request_permission_to_unmute_participants": {"type": "boolean"},
                    "allow_host_control_participant_mute_state": {"type": "boolean"},
                    "disable_participant_video": {"type": "boolean"},
                    "email_in_attendee_report": {"type": "boolean"},
                    "auto_start_deepfake_detection": {"type": "boolean"},
                    "prevent_screen_capture": {"type": "boolean"},
                    "disable_recording_control": {"type": "string", "enum": ["none", "all"]},
                },
            },
            "start_time": {"type": "string", "format": "date-time"},
            "sensitivity_label_id": {"type": "string"},
            "template_id": {"type": "string"},
            "timezone": {"type": "string"},
            "topic": {"maxLength": 200, "type": "string"},
            "tracking_fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
                },
            },
            "type": {"type": "integer", "enum": [1, 2, 3, 8, 10]},
        },
    },
    ("PUT", "/v2/meetings/{meeting_id}/status"): {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["end", "recover"]}},
    },
    ("POST", "/v2/meetings/{meeting_id}/registrants"): {
        "allOf": [
            {
                "required": ["email", "first_name"],
                "type": "object",
                "properties": {
                    "first_name": {"maxLength": 64, "type": "string"},
                    "last_name": {"maxLength": 64, "type": "string"},
                    "email": {"maxLength": 128, "type": "string", "format": "email"},
                    "address": {"type": "string"},
                    "city": {"type": "string"},
                    "state": {"type": "string"},
                    "zip": {"type": "string"},
                    "country": {"type": "string"},
                    "phone": {"type": "string"},
                    "comments": {"type": "string"},
                    "custom_questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "value": {"maxLength": 128, "type": "string"},
                            },
                        },
                    },
                    "industry": {"type": "string"},
                    "job_title": {"type": "string"},
                    "no_of_employees": {
                        "type": "string",
                        "enum": [
                            "",
                            "1-20",
                            "21-50",
                            "51-100",
                            "101-500",
                            "500-1,000",
                            "1,001-5,000",
                            "5,001-10,000",
                            "More than 10,000",
                        ],
                    },
                    "org": {"type": "string"},
                    "purchasing_time_frame": {
                        "type": "string",
                        "enum": [
                            "",
                            "Within a month",
                            "1-3 months",
                            "4-6 months",
                            "More than 6 months",
                            "No timeframe",
                        ],
                    },
                    "role_in_purchase_process": {
                        "type": "string",
                        "enum": ["", "Decision Maker", "Evaluator/Recommender", "Influencer", "Not involved"],
                    },
                },
            },
            {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": [
                            "en-US",
                            "de-DE",
                            "es-ES",
                            "fr-FR",
                            "jp-JP",
                            "pt-PT",
                            "ru-RU",
                            "zh-CN",
                            "zh-TW",
                            "ko-KO",
                            "it-IT",
                            "vi-VN",
                            "pl-PL",
                            "Tr-TR",
                        ],
                    }
                },
            },
            {"type": "object", "properties": {"auto_approve": {"type": "boolean"}}},
        ]
    },
    ("PUT", "/v2/meetings/{meeting_id}/registrants/status"): {
        "required": ["action"],
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["approve", "cancel", "deny"]},
            "registrants": {
                "maximum": 30,
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"email": {"type": "string"}, "id": {"type": "string"}},
                },
            },
        },
    },
    ("PATCH", "/v2/meetings/{meeting:path}/recordings/settings"): {
        "type": "object",
        "properties": {
            "approval_type": {"type": "integer", "enum": [0, 1, 2]},
            "authentication_domains": {"type": "string"},
            "authentication_option": {"type": "string"},
            "on_demand": {"type": "boolean"},
            "password": {"maxLength": 10, "minLength": 8, "type": "string"},
            "recording_authentication": {"type": "boolean"},
            "send_email_to_host": {"type": "boolean"},
            "share_recording": {"type": "string", "enum": ["publicly", "internally", "none"]},
            "show_social_share_buttons": {"type": "boolean"},
            "topic": {"type": "string"},
            "viewer_download": {"type": "boolean"},
            "auto_delete": {"type": "boolean"},
        },
    },
    ("PUT", "/v2/meetings/{meeting:path}/recordings/{recording_id}/status"): {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["recover"]}},
    },
    ("PUT", "/v2/meetings/{meeting:path}/recordings/status"): {
        "type": "object",
        "required": ["action"],
        "properties": {"action": {"type": "string", "enum": ["recover"]}},
    },
}
