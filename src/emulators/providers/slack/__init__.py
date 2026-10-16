"""Slack: Web API, OAuth v2 / Sign in with Slack, Events API, incoming webhooks and files."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext


class SlackProvider(Provider):
    name = "slack"
    title = "Slack"
    hosts = ("slack.com", "*.slack.com", "hooks.slack.com", "api.slack.com", "files.slack.com")
    default_port = 44107
    description = "Slack Web API, OAuth v2, Events API, incoming webhooks and files for one workspace."

    def error_body(self, status: int, code: str, message: str) -> Any:
        from emulators.providers.slack.routes import error_body

        return error_body(status, code, message)

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.slack.seed import seed

        seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.slack.routes import register

        register(app, ctx)


PROVIDER = SlackProvider()
