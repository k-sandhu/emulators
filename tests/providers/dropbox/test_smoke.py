"""First contact: auth, current account, listing."""

from __future__ import annotations

from typing import Any


async def test_current_account(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    acct = await dbx.ok(token, "users/get_current_account")
    assert acct["email"] == dbx.MAYA
    assert acct["account_type"] == {".tag": "business"}
    assert acct["root_info"][".tag"] == "team"
    assert acct["root_info"]["home_path"] == "/Maya Chen"
