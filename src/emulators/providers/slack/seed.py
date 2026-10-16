"""Project the shared world into the Slack workspace (deterministic, ~1 s)."""

from __future__ import annotations

from emulators.core.provider import ProviderContext
from emulators.providers.slack.seed_channels import seed_conversations
from emulators.providers.slack.seed_users import (
    seed_app,
    seed_emoji,
    seed_tokens,
    seed_usergroups,
    seed_users,
)


def seed(ctx: ProviderContext) -> None:
    seed_app(ctx)
    mapping = seed_users(ctx)
    names = seed_conversations(ctx, mapping)
    seed_usergroups(ctx, mapping, names)
    seed_emoji(ctx)
    seed_tokens(ctx, mapping)
