"""Slack: Web API, OAuth v2 / Sign in with Slack, Events API, incoming webhooks and files.

One workspace (Halcyon Advisory Group, ``halcyonadvisory.slack.com``) projected from the shared
world. Module map: ``m_*`` hold the Web API methods, ``seed*`` the data projection, ``events``
the Events API, ``incoming`` the webhook endpoint, ``actions`` the control-plane simulations.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext


class SlackProvider(Provider):
    name = "slack"
    title = "Slack"
    hosts = ("slack.com", "*.slack.com", "hooks.slack.com", "api.slack.com", "files.slack.com")
    default_port = 44107
    description = (
        "Slack Web API, OAuth v2 and Sign in with Slack, Events API, incoming webhooks and files "
        "for the Halcyon Advisory Group workspace."
    )
    credentials = {
        "workspace": "https://halcyonadvisory.slack.com/ (team id from the world)",
        "tokens": "see 'tokens' below: seeded xoxb-/xoxp-/xapp- tokens, or POST /_emulator/tokens",
        "oauth": "any client_id/client_secret works (lenient); authorize with login_hint=<email>",
    }

    def __init__(self) -> None:
        super().__init__()
        from emulators.providers.slack.actions import ACTIONS
        from emulators.providers.slack.interactivity import ACTIONS as INTERACTIVITY_ACTIONS

        self.actions.update(ACTIONS)
        self.actions.update(INTERACTIVITY_ACTIONS)

    def error_body(self, status: int, code: str, message: str) -> Any:
        from emulators.providers.slack.routes import error_body

        return error_body(status, code, message)

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.slack.seed import seed

        seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.slack.routes import register

        register(app, ctx)

    def info(self, ctx: ProviderContext) -> dict[str, Any]:
        from emulators.providers.slack import access
        from emulators.providers.slack.seed_users import seeded_tokens

        info = super().info(ctx)
        app = access.app_config(ctx)
        info["actions"] = {
            name: (inspect.getdoc(fn) or "").split("\n\n")[0] for name, fn in sorted(self.actions.items())
        }
        info["credentials"] = {
            **self.credentials,
            "team_id": access.team_id(ctx),
            "app_id": app["app_id"],
            "bot_user_id": app["bot_user_id"],
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "signing_secret": app["signing_secret"],
            "verification_token": app["verification_token"],
            "tokens": seeded_tokens(ctx),
            "incoming_webhooks": [
                f"https://hooks.slack.com/services/{hook['id']}" for hook in access.col(ctx, access.WEBHOOKS)
            ],
        }
        return info


PROVIDER = SlackProvider()
