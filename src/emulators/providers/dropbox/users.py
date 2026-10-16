"""``users/*``, ``check/*`` and ``auth/token/revoke``."""

from __future__ import annotations

from typing import Any

from emulators.providers.dropbox import catalog
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import api_error, tag
from emulators.providers.dropbox.dispatch import route


def team_object(c: m.Call) -> dict[str, Any]:
    team = m.team_info(c.ctx)
    return {
        "id": team["id"],
        "name": team["name"],
        "sharing_policies": catalog.sharing_policies(team["policies"]),
        "office_addin_policy": tag(team["office_addin"]),
        "top_level_content_policy": tag(team["top_level_content_policy"]),
    }


def root_info(acct: dict[str, Any]) -> dict[str, Any]:
    if acct.get("is_team"):
        return {
            ".tag": "team",
            "root_namespace_id": acct["root_ns"],
            "home_namespace_id": acct["home_ns"],
            "home_path": "/" + acct["display_name"],
        }
    return {".tag": "user", "root_namespace_id": acct["root_ns"], "home_namespace_id": acct["home_ns"]}


def full_account(c: m.Call, acct: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {
        "account_id": acct["id"],
        "name": m.name_obj(acct),
        "email": acct["email"],
        "email_verified": acct["email_verified"],
        "disabled": acct["status"] == "suspended" or acct.get("disabled", False),
        "locale": acct["locale"],
        "referral_link": acct["referral_link"],
        "is_paired": False,
        "account_type": tag(acct["account_type"]),
        "root_info": root_info(acct),
    }
    if acct.get("country"):
        d["country"] = acct["country"]
    if acct.get("is_team"):
        d["team"] = team_object(c)
        d["team_member_id"] = acct["team_member_id"]
    return d


def basic_account(c: m.Call, acct: dict[str, Any]) -> dict[str, Any]:
    teammate = bool(acct.get("is_team")) and bool(c.actor.account.get("is_team"))
    d: dict[str, Any] = {
        "account_id": acct["id"],
        "name": m.name_obj(acct),
        "email": acct["email"],
        "email_verified": acct["email_verified"],
        "disabled": acct["status"] == "suspended" or acct.get("disabled", False),
        "is_teammate": teammate,
    }
    if teammate and acct.get("team_member_id"):
        d["team_member_id"] = acct["team_member_id"]
    return d


def used_bytes(c: m.Call, ns_ids: set[str]) -> int:
    total = 0
    for rec in c.tree.entries.values():
        if rec["type"] == "file" and not rec.get("deleted") and rec["ns"] in ns_ids:
            total += rec["revisions"][-1]["size"]
    return total


@route("users/get_current_account", scope="account_info.read", void_arg=True)
def get_current_account(c: m.Call) -> Any:
    return full_account(c, c.actor.account)


@route("users/get_account", scope="sharing.read")
def get_account(c: m.Call) -> Any:
    account_id = c.args.str("account_id", pattern=r"[-_0-9a-zA-Z:]+")
    acct = m.account(c.ctx, account_id)
    if acct is None:
        raise api_error(tag("no_account"))
    return basic_account(c, acct)


@route("users/get_account_batch", scope="sharing.read")
def get_account_batch(c: m.Call) -> Any:
    ids = c.args.list("account_ids")
    out = []
    for account_id in ids:
        acct = m.account(c.ctx, account_id)
        if acct is None:
            raise api_error(tag("no_account", no_account=account_id))
        out.append(basic_account(c, acct))
    return out


@route("users/get_space_usage", scope="account_info.read", void_arg=True)
def get_space_usage(c: m.Call) -> Any:
    acct = c.actor.account
    used = used_bytes(c, {acct["home_ns"]})
    if not acct.get("is_team"):
        return {"used": used, "allocation": tag("individual", allocated=acct["space_allocated"])}
    team_ns = {ns["id"] for ns in c.tree.ns.values() if ns.get("team_id")}
    return {
        "used": used,
        "allocation": tag(
            "team",
            used=used_bytes(c, team_ns),
            allocated=int(m.team_info(c.ctx)["num_licensed_users"]) * 1024**4,
            user_within_team_space_allocated=0,
            user_within_team_space_limit_type=tag("off"),
            user_within_team_space_used_cached=used,
        ),
    }


FEATURE_VALUES = {
    "paper_as_files": ("enabled", True),
    "file_locking": ("enabled", True),
    "team_shared_dropbox": ("enabled", True),
    "distinct_member_home": ("enabled", True),
}


@route("users/features/get_values", scope="account_info.read")
def features_get_values(c: m.Call) -> Any:
    features = c.args.list("features")
    if not features:
        raise api_error(tag("empty_features_list"))
    values = []
    for feature in features:
        name = feature.get(".tag") if isinstance(feature, dict) else feature
        if name in FEATURE_VALUES:
            _, flag = FEATURE_VALUES[name]
            values.append(tag(name, **{name: tag("enabled", enabled=flag)}))
        else:
            values.append(tag("other"))
    return {"values": values}


@route("check/user")
def check_user(c: m.Call) -> Any:
    return {"result": c.args.str("query", "")}


@route("check/app", auth="app")
def check_app(c: m.Call) -> Any:
    return {"result": c.args.str("query", "")}


@route("auth/token/revoke", void_arg=True)
def token_revoke(c: m.Call) -> Any:
    grant = c.actor.grant
    if grant is not None:
        c.ctx.tokens.revoke(grant.token)
        linked = grant.extra.get("refresh")
        if linked:
            c.ctx.tokens.revoke(linked)
            for record in c.ctx.store.collection("_grants"):
                if record.get("extra", {}).get("refresh") == linked:
                    record["revoked"] = True
    return None
