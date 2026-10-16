# Dropbox emulator

Dropbox API v2 -- files, sharing, users, auth and the **Dropbox Business** team endpoints --
projected onto the shared *Halcyon Advisory Group* world. The official `dropbox` Python SDK (and
any hand-written `httpx`/`requests` client) works unchanged: every response shape is checked in
the test-suite against the SDK's own Stone validators.

* package: `src/emulators/providers/dropbox/`
* tests: `tests/providers/dropbox/` (wire level, strict SDK validators), `tests/sdk/test_dropbox_sdk.py`
  (official SDK, `EMULATORS_PROVIDERS=dropbox uv run pytest tests/sdk/test_dropbox_sdk.py`)
* dedicated port: `44104` (`uv run emulators serve`), gateway prefix `http://localhost:44080/dropbox`

## Quick start

```python
import dropbox, requests
from emulators.testing.server import EmulatorServer
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["dropbox"], anchor="2026-10-16")
with EmulatorServer(hub) as server:                      # or the `emulator_intercept` pytest fixture
    token = requests.post(f"{server.url_for('dropbox')}/_emulator/tokens",
                          json={"user": "maya.chen@halcyonadvisory.example"}).json()["access_token"]
    # the official SDK hard-codes https://api.dropboxapi.com: use `emulator_intercept` (patches requests/httpx)
    # or call the gateway directly with plain HTTP:
    r = requests.post(f"{server.url_for('dropbox')}/2/files/list_folder",
                      headers={"Authorization": f"Bearer {token}"}, json={"path": ""})
    print([e["name"] for e in r.json()["entries"]])      # ['Expenses', 'Notes']
```

In tests, `emulator_intercept` (see `tests/sdk/test_dropbox_sdk.py`) routes every Dropbox hostname to the in-process gateway so
`dropbox.Dropbox(oauth2_access_token=token)` and `dropbox.DropboxTeam(team_token)` run unchanged.

## Hosts

| Host | Used for |
| --- | --- |
| `api.dropboxapi.com` | RPC routes (`/2/<namespace>/<route>`), `POST /oauth2/token` |
| `content.dropboxapi.com` | upload / download style routes, temporary upload links (`/apitul/1/<token>`) |
| `notify.dropboxapi.com` | `files/list_folder/longpoll` |
| `www.dropbox.com` | `GET /oauth2/authorize`, shared-link pages (`/scl/fi/...`, `/scl/fo/...`, `/s/...`, `/sh/...`) |
| `api.dropbox.com` | alias of the token endpoint |
| `dl.dropboxusercontent.com`, `*.dl.dropboxusercontent.com` | temporary download links (`/apitl/1/<token>`), shared-link downloads (`/cd/0/get/<token>/<name>`) |

All hosts are served by one app, so any route may be called on any host; absolute URLs in responses
(temporary links, shared links, `preview_url`) use the real hostnames in host-routing mode and the
base URL you are using in prefix / per-port mode.

## Wire conventions (as on the real service)

* **RPC**: `POST /2/<ns>/<route>` with a JSON body (`Content-Type: application/json`); void routes also
  accept an empty body or `null`; `GET ?arg=..&authorization=Bearer ..` works too.
* **Upload**: argument JSON in `Dropbox-API-Arg` (or `?arg=`), body = bytes, `Content-Type: application/octet-stream`.
  **Download**: argument in `Dropbox-API-Arg`, result JSON in `Dropbox-API-Result`, body = bytes, `Range` supported.
  Header JSON is "HTTP header safe": non-ASCII (and DEL) are `\uXXXX`-escaped on output and decoded on input.
* **Errors**: route errors are `409 {"error_summary": "path/not_found/...", "error": {".tag": "path", "path": {".tag": "not_found"}}}`;
  malformed input is `400 text/plain` (`Error in call to API function "files/list_folder": request body: path: ...`);
  auth errors are `401` (`invalid_access_token`, `expired_access_token`, `user_suspended`, `invalid_select_user`,
  `invalid_select_admin`, `route_access_denied`, `missing_scope` with `required_scope`); path-root problems are `422`
  (`invalid_root` / `no_permission`); rate limits are `429 {"error": {"reason": {".tag": "too_many_requests"}, "retry_after": N}}`
  plus `Retry-After` (inject with `/_emulator/faults`). Every response carries `X-Dropbox-Request-Id`.
* **Unions** use `.tag` everywhere, with Stone's flattening rules (struct payloads are inlined, nested unions live under
  the tag name). Fields typed as the leaf `FileMetadata`/`FolderMetadata` (upload result, `get_temporary_link.metadata`,
  `list_revisions` entries ...) are *untagged*; the polymorphic `Metadata` (list_folder entries, get_metadata,
  move/copy/delete results ...) carries `.tag`.
* Paths are case-insensitive but case-preserving; `""` is the root (a bare `"/"` is `malformed_path`); `id:...`, `rev:...`
  and `ns:<id>/path` forms work wherever the real API accepts them. Missing parent folders are created on upload, move,
  copy and create_folder. Names with `\ < > : " | ? *`, control characters, trailing dot/space, or `.dropbox`,
  `desktop.ini`, `thumbs.db`, `.DS_Store` are `disallowed_name`.
* **Content hash**: Dropbox's 4 MiB-block SHA-256 (`content_hash`), verified when the client sends one
  (`content_hash_mismatch`; the official SDK sends it automatically).

## Auth flows and test credentials

Lenient by default: any `client_id` / `client_secret` pair is accepted.

* **Mint a token** (fastest): `POST /_emulator/tokens` (or `/_emulator/actions/mint_token`)

  ```json
  {"user": "maya.chen@halcyonadvisory.example"}                              // user token (person id, email, display name, account id or dbmid:)
  {"user": "arjun.mehta@halcyonadvisory.example", "team": true}              // team-level token ("Team member file access")
  {"user": "maya.chen@...", "scopes": ["files.metadata.read"], "expires_in": 60, "refresh_token": true, "client_id": "my-app"}
  ```

  Tokens look like `sl.B...`; `scopes: []` (the default) means "all scopes" (a legacy full-access app). A listed scope set is
  enforced route by route (`401 missing_scope`).
* **Authorization code flow**: `GET https://www.dropbox.com/oauth2/authorize?response_type=code&client_id=..&redirect_uri=..&state=..`
  (`token_access_type=offline|online`, PKCE `code_challenge`/`code_challenge_method`, `scope`, `include_granted_scopes`).
  `login_hint=<email>` approves immediately and redirects to `redirect_uri?code=..&state=..`; without it a small account
  picker is shown. Without `redirect_uri` the code is displayed on the page (`<code id='code'>`), like the "no redirect" flow.
  `response_type=token` returns the token in the fragment. Requesting any team scope (`members.read`, `team_info.read`,
  `team_data.member`, ...) yields a **team token**.
* **Token endpoint**: `POST /oauth2/token` (form-encoded; client id/secret in the body or HTTP Basic) with
  `grant_type=authorization_code` (+ `code_verifier`) or `refresh_token`. Response:
  `{"access_token": "sl.B...", "token_type": "bearer", "expires_in": 14400, "refresh_token": ..., "scope": "...", "uid": "...", "account_id": "dbid:...", "team_id": "dbtid:..."}`
  (`refresh_token` only for `token_access_type=offline`; `team_id` for Business members, which the SDK reports as `account_id`).
* `POST /2/auth/token/revoke` revokes the access token and its refresh token. `check/user` and `check/app` (HTTP Basic) echo.
* **Acting as a member** (team tokens): `Dropbox-API-Select-User: dbmid:...` or `Dropbox-API-Select-Admin: dbmid:...`
  (an admin; reaches any team-owned namespace, ignoring access inheritance cuts).
* **Path root**: `Dropbox-API-Path-Root: {".tag": "home"}` (default), `{".tag": "root", "root": "<root_namespace_id>"}` (the
  team space: team folders + your member folder) or `{".tag": "namespace_id", "namespace_id": "..."}`.

## Namespace model

* **Team root** namespace (`root_namespace_id`) holds mount points for every *team folder* and each *member folder*.
* A member's **home** namespace (`home_namespace_id`, `team_member_folder`) is what the default path root shows
  (`/Notes`, `/Expenses`, ...); in the team-root view it appears as `/<Display Name>`.
* **Team folders** (`team_folder`, id == `shared_folder_id`) grant access through groups (`editor`/`viewer`).
* **Shared folders** (`share_folder`) turn a folder into its own namespace; members must `mount_folder` to see it at a path.
  Team members are not auto-mounted.
* Non-team collaborators (client contacts) have *basic* accounts with their own home; they reach shared content through
  file/folder membership (`list_received_files`, `id:` addressing, mounted shared folders), never through the team root.
* What a caller sees is derived from their path root, so `path_lower`/`path_display` are relative to it and absent for items
  outside it.

## Endpoints

Auth: **U** = user token (or a team token + Select-User/Admin), **T** = team token, **A** = also HTTP Basic (app auth),
**-** = none. Routes marked *deprecated* are served because older SDKs still call them.

### auth / check / users / openid

| Route | Auth | Notes |
| --- | --- | --- |
| `auth/token/revoke` | U | revokes token (+ refresh token and siblings) |
| `check/user`, `check/app` | U / A | echo `{"result": query}` |
| `users/get_current_account` | U | `FullAccount` incl. `root_info` (team: `home_path`), `team`, `team_member_id` |
| `users/get_account`, `users/get_account_batch` | U | `BasicAccount` with `is_teammate` |
| `users/get_space_usage` | U | `team` allocation for members, `individual` otherwise |
| `users/features/get_values` | U | |
| `openid/userinfo` | U | needs `openid` (+ `profile`, `email`) scopes when scopes are listed |

### files

| Route | Auth | Notes |
| --- | --- | --- |
| `files/list_folder` (+`/continue`, `/get_latest_cursor`) | U,A | recursive, include_deleted, include_mounted_folders, include_has_explicit_shared_members, include_non_downloadable_files, limit 1..2000, `shared_link`, `include_property_groups`; cursors flip to *delta* mode after the last page; `reset` after a store reset |
| `files/list_folder/longpoll` | - (notify host) | waits up to `min(timeout, LONGPOLL_MAX_WAIT_S)` (default 2 s) |
| `files/get_metadata` | U | path, `id:`, `rev:`, `include_deleted`; root is `malformed_path` |
| `files/download`, `download_zip`, `export` | U | `Range`; Paper-style files are not downloadable but exportable (`markdown`/`html`) |
| `files/get_temporary_link`, `get_temporary_upload_link` | U | `dl.dropboxusercontent.com/apitl/1/<token>` (4 h); `content.dropboxapi.com/apitul/1/<token>` |
| `files/get_thumbnail_v2` (+v1, `_batch`), `get_preview` | U,A | png / jpeg thumbnails (generated gradients, valid images); preview = PDF for office docs; webp -> `unsupported_output_format` |
| `files/upload`, `alpha/upload` | U | `add` / `overwrite` / `update(rev)`, autorename, strict_conflict, client_modified, content_hash |
| `files/upload_session/start`, `append_v2`, `append`, `finish`, `finish_batch_v2`, `finish_batch` (+`/check`), `start_batch` | U | offsets enforced (`incorrect_offset`), `close`, sequential and concurrent sessions |
| `files/create_folder_v2` (+v1, `_batch`, `_batch/check`) | U | |
| `files/delete_v2` (+v1, `_batch`, `_batch/check`), `permanently_delete` | U | soft delete keeps revisions; `include_deleted` / `list_revisions` / `restore` work on it |
| `files/move_v2`, `files/copy_v2` (+v1, `_batch`, `_batch_v2`, checks) | U | cross-namespace moves, autorename, `cant_move_folder_into_itself`; batches run eagerly and answer with `async_job_id` |
| `files/copy_reference/get`, `copy_reference/save` | U | |
| `files/list_revisions`, `files/restore` | U | one revision per `Document.versions` |
| `files/search_v2` (+`search/continue_v2`), `files/search` *(deprecated)* | U | filename + content (seeded document text), path scope, extensions, categories, highlights |
| `files/lock_file_batch`, `unlock_file_batch`, `get_file_lock_batch` | U | only files in shared/team folders; other users cannot overwrite / delete / move |
| `files/tags/get`, `add`, `remove` | U | |
| `files/save_url` (+`/check_job_status`) | U | **no outbound network**: fetches `data:` URLs and emulator-hosted URLs only, otherwise the job fails with `download_failed` |
| `files/alpha/get_metadata` *(deprecated)* | U | alias of `get_metadata` |

### file_requests / contacts

`file_requests/{create,get,list,list_v2,list/continue,count,update,delete,delete_all_closed}` -- the request page
(`https://www.dropbox.com/request/<id>`) is a stub; simulate an outsider's upload with the `simulate_file_request_upload` action.
`contacts/delete_manual_contacts` and `_batch` succeed without effect.

### file_properties

`file_properties/templates/{add,get,list,remove,update}_for_{user,team}` and
`file_properties/properties/{add,overwrite,update,remove,search,search/continue}`; groups appear in metadata with
`include_property_groups: {".tag": "filter_some", ...}`. Team templates are visible to every member.

### sharing

| Route | Notes |
| --- | --- |
| `sharing/share_folder` (+`check_share_job_status`), `list_folders`, `list_mountable_folders` (+`/continue`), `get_folder_metadata` | `SharedFolderMetadata` incl. policy, permissions (when `actions` given), `access_inheritance`; `force_async` supported |
| `sharing/list_folder_members` (+`/continue`), `add_folder_member`, `remove_folder_member` (+`check_remove_member_job_status`), `update_folder_member` | users / groups / invitees; `member_policy: team` blocks outsiders; team folders are managed by admins (`Dropbox-API-Select-Admin`) |
| `sharing/mount_folder`, `unmount_folder`, `unshare_folder`, `relinquish_folder_membership`, `transfer_folder`, `update_folder_policy`, `set_access_inheritance`, `check_job_status` | |
| `sharing/list_file_members` (+`/continue`, `/batch`), `add_file_member`, `remove_file_member_2`, `remove_file_member` *(deprecated)*, `update_file_member`, `unshare_file`, `relinquish_file_membership` | explicit file members from `Document.shares`; inherited users/groups from the containing shared/team folder |
| `sharing/list_received_files` (+`/continue`), `get_file_metadata` (+`/batch`) | |
| `sharing/create_shared_link_with_settings`, `list_shared_links`, `get_shared_link_metadata`, `get_shared_link_file`, `modify_shared_link_settings`, `revoke_shared_link` | `requested_visibility` public/team_only/password, `audience`, `access`, `expires`, `allow_download`, `link_password`; second create -> `shared_link_already_exists` with metadata; `files/list_folder` + `shared_link` lists a folder link |
| `sharing/create_shared_link`, `get_shared_links` *(deprecated)* | legacy `/s/<id>/<name>` URLs |

Shared links: `https://www.dropbox.com/scl/fi/<id>/<name>?rlkey=<key>&dl=0` (files) and `/scl/fo/<id>/<hash>?rlkey=...` (folders).
`GET` on the URL shows a small HTML page; `?dl=1` / `?raw=1` redirect to `uc<hash>.dl.dropboxusercontent.com/cd/0/get/<token>/<name>`
(folders: a zip); password links accept `?password=`; team-only / no-one links answer `403` to anonymous browsers.

### team (Business)

| Route | Notes |
| --- | --- |
| `team/get_info`, `team/token/get_authenticated_admin`, `team/features/get_values` | |
| `team/members/list_v2` (+`/continue_v2`), `list` (+`/continue`), `get_info_v2`, `get_info`, `get_available_team_member_roles` | `profile` incl. `team_member_id`, `groups`, `member_folder_id`, `root_folder_id`; departed employees are `suspended` |
| `team/members/add_v2`, `suspend`, `unsuspend`, `remove` (+`/job_status/get`), `recover`, `set_admin_permissions_v2`, `set_profile_v2`, `send_welcome_email` | write routes need a token whose admin has a *Team admin* / *User management admin* role (`401 route_access_denied` otherwise) |
| `team/groups/list` (+`/continue`), `get_info`, `members/list` (+`/continue`), `create`, `update`, `delete`, `members/add`, `members/remove`, `job_status/get` | one company-managed group per world group; changes alter team-folder access immediately |
| `team/team_folder/list` (+`/continue`), `get_info`, `create`, `rename`, `archive` (+`/check`), `activate`, `permanently_delete`, `update_sync_settings` | archived folders disappear for members |
| `team/namespaces/list` (+`/continue`) | `team_member_folder` per member + `team_folder`s + team-owned shared folders |
| `team/devices/list_members_devices`, `list_team_devices`, `list_member_devices`, `revoke_device_session` (+`_batch`) | one desktop client and one phone per active member (deterministic); revocation removes them |
| `team/linked_apps/list_members_linked_apps`, `list_team_linked_apps`, `list_member_linked_apps`, `revoke_linked_app` (+`_batch`) | one app per OAuth `client_id` with a live token; revoking kills the member's tokens for that app |
| `team_log/get_events` (+`/continue`) | `file_add/edit/delete/move/copy/rename`, `login_success`, `shared_folder_create`, `shared_link_create`, `group_create`, `member_change_status`; seeded from recent document edits |

## Seeded data (deterministic, ~0.3 s)

* Team `dbtid:AAD94f565a6beeb52a733f8bef7d77b04` -- "Halcyon Advisory Group"; **47 members** (45 active, Paul Henderson and Yuki Tanaka
  *suspended*), **86 client / vendor contacts** as basic accounts, **17 groups** (`g-partners` -> `g:127b75401da1654bce56af8f2156769a`, ...).
* Admins: Arjun Mehta (`arjun.mehta@halcyonadvisory.example`, *Team admin*), Sarah Johansson (*User management admin*).
* Example people: `maya.chen@halcyonadvisory.example` (account `dbid:AAj7rOZU6LBJzBOkeu_aYl-bHqupJEZRY4t`, member
  `dbmid:AAVDZUH1Tq791ahWelTLoW0jPCV_igCfMmzTVWj`, home namespace `1600365475`), `ryan.murphy@halcyonadvisory.example`,
  client contact `christopher.ingram@northwindlogistics.example` (basic account, file member of the Northwind PBC sheet).
* Team root namespace `7811983619`; **team folders** (id == shared folder id): Client Engagements `3390204882`, Finance & Operations
  `7770591070`, People & Culture `7515113319`, Technology `5913236337`, Marketing & BD `6609964232`, Knowledge Base `6140259346`, Partner Group
  `8722852746`. Access comes from `Space.members` through groups (`owner` is mapped to `editor`; team folders have no user owner).
* **359 documents** -> files with real DOCX/XLSX/PPTX/PDF/CSV/MD/PNG bytes, `Document.versions` revisions, `client_modified` = world
  modified time (server time +7 s). Duplicate world paths get Dropbox's ` (1)` suffix. Personal spaces fill each member's home
  (`/Notes`, `/Expenses`, `/Drafts`).
* `Document.shares` -> **file members** (roles: editor -> `editor`, commenter -> `viewer`, viewer -> `viewer_no_comment`;
  expiry honoured: the Northwind "Draft Financial Statements.pdf" share for Christopher expires 2026-11-15); link shares ->
  **2 public shared links** (the two Prospects proposals).
* `Document.restricted` -> `no_inherit`: inherited team-folder access is removed for that single file, so only the owner, explicit
  members and admins (Select-Admin) see it (e.g. `/Finance & Operations/Payroll/Payroll Calendar 2026.xlsx`). Real Dropbox cuts
  inheritance on *folders*; the emulator applies it per file.
* Vendor extras: two Dropbox **Paper** docs (`/Knowledge Base/Paper/*.paper`, not downloadable, `export_info`) and three PNG photos in
  `/Marketing & BD/Brand Assets/Photos`.
* Time: everything uses the hub clock (`POST /_emulator/clock` on the gateway to freeze or advance). The world is anchored at
  `2026-10-16`, so a *live* clock past 2026-11-15 expires the seeded Northwind share -- freeze the clock for date-sensitive tests.
* CORS: `OPTIONS` preflights and `Access-Control-Allow-Origin: *` are served so the browser SDK (`text/plain; charset=dropbox-cors-hack`) works.
* Anchor date `2026-10-16`; the first list_folder cursor of a fresh hub starts at change sequence 0.

## Webhooks

Webhooks are per app (`app_key` = OAuth `client_id`; `*` = any app). Register through the control plane:

```
POST /_emulator/actions/configure_webhook   {"url": "https://consumer.test/hook", "app_secret": "s3cret", "app_key": "my-app"}
```

The emulator verifies the URL like Dropbox does: `GET <url>?challenge=<random>` must answer `200` with the challenge as the body
(`400` otherwise). After any file change it `POST`s

```json
{"list_folder": {"accounts": ["dbid:..."]}, "delta": {"users": [12345678]}}
```

with header `X-Dropbox-Signature` = hex `HMAC-SHA256(app_secret, raw body)`. Accounts are those that have access to the changed namespace
**and** authorised the app (a live token with that `client_id`; a team token covers every team member). Notifications are debounced per
request and per app: a batch call produces one delivery. Deliveries use `ctx.webhooks` (modes `async`/`sync`/`manual`) and appear in
`GET /_emulator/webhooks`. Like the real service the payload is only a hint -- call `list_folder/continue` with your cursor.

## `/_emulator/actions/*`

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `{user, team, scopes, expires_in, refresh_token, client_id}` | token response like `/oauth2/token` |
| `configure_webhook` | `{url, app_secret, app_key}` | verify + register a webhook |
| `remove_webhook` | `{app_key}` | |
| `list_webhooks` | `{}` | registered webhooks (without secrets) |
| `simulate_file_request_upload` | `{id, name, content}` | store a file in the request's destination (fails when the request is closed) and notify webhooks |
| `set_quota` | `{user, allocated}` | cap a person's storage; uploads that would exceed it fail with `path/insufficient_space` |
| `trigger_webhook` | `{accounts: [email\|person id\|account id], app_key}` | deliver a notification now |

Settings (`EMULATORS_DROPBOX_<NAME>` env vars or hub `settings`): `LONGPOLL_MAX_WAIT_S` (default `2`), `ASYNC_JOB_POLLS` (number of
`in_progress` answers before async jobs complete, default `0`), `SHARED_LINKS_PAGE` (page size of `list_shared_links`, default `100`).

## Known gaps

* Not implemented: `paper/*`, `account/*` (profile photos), `team/reports/*`, `team/legal_holds/*`, `team/member_space_limits/*`, `team/sharing_allowlist/*`, `auth/token/from_oauth1`,
  `files/paper/*`, `files/upload_session/append_batch`, `sharing/update_file_policy`, `sharing/relinquish_access`.
* Thumbnails are generated gradients (PNG and a tiny baseline JPEG) of the requested size, not renderings of the source image;
  `webp` output is `unsupported_output_format`. Previews of office documents are PDFs built from the seeded text.
* `Document.restricted` cuts inheritance per file instead of per folder; folder-level `no_inherit` on shared folders is stored and
  reported (`access_inheritance`) but only the world's restricted files change visibility.
* Older revisions share the latest revision's bytes (only `rev`, timestamps and `modified_by` differ).
* Search matches whole-word prefixes in file names and any substring in indexed text (seeded document text, text-like uploads); there is no
  ranking beyond name-before-content, and no stemming.
* `files/save_url` never touches the network; `shared_link_policy`/`member_policy` are enforced, other folder policies are only stored.
* `leave_a_copy` on member removal / unshare is accepted but no copy is made; `files/copy_batch` (v1) fails the whole job on the first error.
* Admin consoles, billing, SSO/domain verification, device approvals and real e-mail invitations (invitees stay *pending* forever) are out of scope.
* Team routes accept any team token for read routes; write routes need a Team/User-management admin. Rate limiting is only available through
  fault injection.
