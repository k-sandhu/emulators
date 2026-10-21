"""Asana's user-delegated work graph backed by the shared synthetic organization."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext
from emulators.providers.asana import common as c


class AsanaProvider(Provider):
    name = "asana"
    title = "Asana"
    hosts = ("app.asana.com",)
    default_port = 44117
    description = "Asana users, workspaces, teams, engagement projects, tasks, comments, attachments, OAuth and signed webhooks."
    credentials = {
        "pat": "asana-dev-p018 (Maya Chen); asana-dev-p035 (Arjun Mehta)",
        "oauth": "any client_id/client_secret in lenient mode; login_hint=<world employee email>",
        "tokens": "POST /_emulator/tokens with user=<world ID, email, or Asana gid>",
    }

    def __init__(self) -> None:
        super().__init__()
        from emulators.providers.asana.auth import mint

        self.actions["mint_token"] = mint
        self.actions["simulate_task_change"] = simulate_task_change
        self.actions["expire_token"] = expire_token

    def error_body(self, status: int, code: str, message: str) -> Any:
        return {"errors": [{"message": message}]}

    def error_headers_for(self, scope: dict[str, Any] | None, status: int) -> dict[str, str]:
        return {"WWW-Authenticate": "Bearer"} if status == 401 else {}

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.asana.seed import seed

        seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.asana import attachments, auth, directory, events, projects, social, tasks

        for module in (auth, directory, projects, tasks, social, attachments, events):
            module.register(app, ctx)

    def info(self, ctx: ProviderContext) -> dict[str, Any]:
        info = super().info(ctx)
        info["credentials"] = {
            **info["credentials"],
            "workspace_gid": ctx.store.meta["workspace_gid"],
            "maya_gid": c.gid("user", "p018"),
        }
        return info


async def simulate_task_change(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Change a task as a named employee and deliver the normal signed webhook event."""
    from emulators.providers.asana.events import emit
    from emulators.providers.asana.tasks import validate

    uid = c.user_id(ctx, payload.get("user", "p018"))
    if uid is None:
        c.invalid("user: Not a valid user")
    user = ctx.store["users"].get(uid)
    grant = ctx.tokens.resolve("asana-dev-" + user["_world"])
    if grant is None:
        raise c.error(401, "Not Authorized")
    current = c.require(ctx, grant, "tasks", payload.get("task_gid"))
    changes = payload.get("changes", {})
    if not isinstance(changes, dict):
        c.invalid("changes: must be an object")
    row = copy.deepcopy(current)
    validate(ctx, grant, row, changes)
    row["modified_at"] = c.stamp(ctx)
    c.put(ctx, "tasks", row)
    for key in changes:
        if row.get(key) != current.get(key):
            await emit(ctx, grant, row, "changed", field=key, value=row.get(key))
    return {"data": c.public(row)}


def expire_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Revoke a configured synthetic token so clients can exercise authentication recovery."""
    token = c.string(payload.get("token"), "token", empty=False)
    return {"revoked": ctx.tokens.revoke(token)}
