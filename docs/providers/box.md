# Box Content Cloud API v2.0

A wire-compatible emulator of the Box Content API: OAuth 2.0 (authorization code, client
credentials, JWT, token exchange), users and groups, folders and files (simple, versioned and
chunked uploads, 302 downloads, versions, thumbnails, locks, trash), collaborations and shared
links, search, events (streams, `admin_logs`, long polling), webhooks v2 with signed deliveries,
metadata, comments, tasks, favourites and more. The official `box-sdk-gen` SDK runs unchanged
against it (`tests/sdk/test_box_sdk.py`).

* Provider name `box`, gateway prefix `/box`, dedicated port `44103`.
* Wire fidelity: field names, id formats (numeric strings), Box timestamps
  (`2026-10-15T09:12:44-07:00`, Pacific time), error envelopes, pagination styles, `fields=`
  semantics (mini representation plus the requested fields) and `etag`/`If-Match` all follow the
  Box reference.

## Hosts

| Host | Used for |
| --- | --- |
| `api.box.com` | `/2.0/...` REST API, `/oauth2/token`, `/oauth2/revoke`, `OPTIONS /2.0/files/content` |
| `upload.box.com` | `/api/2.0/files/content`, `/api/2.0/files/{id}/content`, chunked upload sessions |
| `account.box.com` | `GET /api/oauth2/authorize` |
| `app.box.com`, `*.app.box.com` | shared link landing pages (`/s/<hash>`), static downloads (`/shared/static/<hash>.<ext>`), avatars (`/api/avatar/large/<id>`) |
| `dl.boxcloud.com`, `*.boxcloud.com` | file bytes (`/d/1/<token>/download`) and zip downloads (`/2.0/zip_downloads/<id>/content`) |
| `public.boxcloud.com` | representation content (`/api/2.0/internal_files/.../content/...`) |
| `realtime*.box.com` | the long-poll endpoint (`/subscribe`) |

Everything is one app; hostnames only matter for the URLs the emulator generates, which come from
`url_for_host` so they stay on `https://dl.boxcloud.com/...` in host mode and on
`http://localhost:44080/box/...` in prefix mode.

```python
from emulators.core.hub import EmulatorHub
import httpx

hub = EmulatorHub(providers=["box"], anchor="2026-10-16", webhook_mode="sync")
async with httpx.AsyncClient(transport=hub.async_transport(), follow_redirects=True) as client:
    auth = {"Authorization": "Bearer box-dev-p018"}          # Maya Chen
    me = await client.get("https://api.box.com/2.0/users/me", headers=auth)
    root = await client.get("https://api.box.com/2.0/folders/0/items", headers=auth)
```

With the official SDK (via the `emulator_intercept` fixture, or by pointing `BaseUrls` at an
`EmulatorServer`):

```python
client = BoxClient(auth=BoxDeveloperTokenAuth(token="box-dev-p035"))
auth = BoxCCGAuth(CCGConfig(client_id="any", client_secret="any", enterprise_id="336533359"))
```

## Authentication and test credentials

Any `client_id` / `client_secret` pair works (lenient client registry). The seeded client is
`halcyon-box-app` / `box-client-secret`.

| Credential | Value |
| --- | --- |
| Enterprise id | `336533359` ("Halcyon Advisory Group") |
| Developer tokens | `box-dev-<person id>` for every employee, e.g. `box-dev-p035` (Arjun Mehta, **admin**), `box-dev-p036` (Sarah Johansson, **co-admin**), `box-dev-p018` (Maya Chen, partner), `box-dev-p044` (Rebecca Stein, growth). Client contacts that collaborate on files also have one (`box-dev-c001-1` ...). Tokens never expire. |
| Service account | `box-dev-enterprise` - "Halcyon Box Automation", `AutomationUser_1982346_<hash>@boxdevedition.com` |
| Minting | `POST /_emulator/tokens {"user": "<box id \| person id \| email>", "expires_in": 3600}` (omit `user` or pass `"enterprise"` for the service account). Add `"refresh": true` through the `mint_token` action for a refresh token. |
| Webhook signature keys | primary `box-primary-signature-key`, secondary `box-secondary-signature-key` |

### OAuth endpoints

* `GET https://account.box.com/api/oauth2/authorize?response_type=code&client_id=...&redirect_uri=...&state=...`
  - with `login_hint=<email>` the emulator approves immediately and redirects with `code` and
  `state`; without it a small account picker lists the active users. Errors redirect with
  `error` / `error_description`.
* `POST https://api.box.com/oauth2/token` (form encoded):
  * `authorization_code` - returns `access_token` (32 alphanumerics), `expires_in`, `restricted_to: []`,
    `refresh_token` (64 alphanumerics), `token_type: "bearer"`. Codes are single use.
  * `refresh_token` - **single use and rotating**; replaying an old refresh token gives
    `400 {"error": "invalid_grant", "error_description": "Refresh token has expired"}`, and the
    access token it was paired with dies too.
  * `client_credentials` (CCG) - `box_subject_type=enterprise` + `box_subject_id=336533359` acts as the
    service account; `box_subject_type=user` + a user id acts as that managed user. A wrong enterprise or
    unknown/external user is `400 invalid_grant`. No refresh token is returned. (Omitting the subject
    defaults to the enterprise.)
  * `urn:ietf:params:oauth:grant-type:jwt-bearer` - the assertion's claims `iss`, `sub` and
    `box_sub_type` (`enterprise`|`user`) are required; the signature is not verified.
  * `urn:ietf:params:oauth:grant-type:token-exchange` (downscoping) - `subject_token`, `scope`
    (space separated: `item_preview`, `item_download`, `item_upload`, `item_readwrite`, `item_share`,
    `item_delete`, `base_explorer`, ...), `resource` (a file or folder URL) or `box_shared_link`.
    Returns the usual body plus `issued_token_type` and a populated `restricted_to`. **The scoping is
    enforced**: the token only sees the resource (and its subtree for folders) and only the operations the
    scopes allow.
* `POST /oauth2/revoke` - revokes the access token and the refresh token paired with it.

Token errors use `{"error", "error_description"}`; API errors use Box's envelope
`{"type": "error", "status", "code", "help_url", "message", "request_id"}` (`context_info.errors` for
validation problems, `context_info.conflicts` for `409 item_name_in_use`). A bad or expired token is
`401` with `WWW-Authenticate: Bearer realm="Service", error="invalid_token", ...`.

**As-User**: admins, co-admins and the service account may send `As-User: <user id>`; the request is then
evaluated as that user (permissions, root folder, events). Anyone else gets
`403 access_denied_insufficient_permissions`; an unknown target is `404`.

## Permissions model

Box has no way to break inheritance, so effective access to an item is the union of

1. **ownership** (the item lives in the user's own tree),
2. **accepted, unexpired collaborations** on the item or any ancestor, granted directly or through a group
   the user belongs to, and
3. an **item-scoped shared link** presented in the `BoxApi: shared_link=<url>&shared_link_password=<pw>`
   header (open links work anonymously; `company` links need a managed user's token).

Roles map to capabilities as in Box: `co-owner`/`owner` everything; `editor` upload, download, rename,
delete, share, invite (not co-owners); `viewer uploader` upload+download+preview+comment+share;
`previewer uploader` upload+preview; `viewer` download+preview+comment+share; `previewer` preview;
`uploader` upload only.

* Items the caller cannot see answer `404 not_found` (Box hides existence); visible-but-not-allowed
  operations answer `403 access_denied_insufficient_permissions`; trashed items `404 trashed`.
* A user's `/folders/0/items` is their own root plus the items collaborated with them whose parent they
  cannot otherwise reach (Box shows those at the top level). For such items `parent` is `null` and
  `path_collection` starts at "All Files" and contains only the folders the user can reach.
* Admins are not omniscient: `Arjun Mehta` sees only what he owns or is invited to. The one cross-account
  view is `GET /search?scope=enterprise_content` (admin only) and the `admin_logs` event stream.
* Group membership changes, collaboration changes, expiry (`expires_at`) and pending invitations take
  effect immediately. Pending invitations grant nothing until accepted.
* Moving an item into another owner's tree changes its `owned_by`. Items uploaded by a collaborator are owned
  by the folder owner (`created_by` is the uploader).
* Downscoped tokens (`restricted_to`) intersect the above with their scopes.

## Endpoints

All paths below are under `https://api.box.com/2.0` unless noted. `fields=` is honoured everywhere it is
in Box. Offset pagination (`offset`, `limit`) and marker pagination (`usemarker=true`, `marker`) behave as
in Box (default limits 100, folder items/users/groups up to 1000, search 30/200, events 100/500).

### Users and groups

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/users/me`, `/users/{id}` | full user object; non-admins may read colleagues but not outsiders they did not invite |
| GET | `/users` | admin/co-admin/service account; `filter_term`, `user_type=all\|managed\|external`, offset or marker paging |
| POST, PUT, DELETE | `/users`, `/users/{id}` | admin; `DELETE` needs `force=true` when the user owns files; deactivating revokes tokens |
| PUT | `/users/{id}/folders/0` | content transfer (`{"owned_by": {"id"}}`): moves the user's content into "<name>'s Files and Folders" of the new owner |
| GET | `/users/{id}/avatar`, `https://app.box.com/api/avatar/large/{id}` | deterministic PNG |
| GET | `/users/{id}/memberships` | group memberships |
| POST, GET | `/invites`, `/invites/{id}` | admin; invite an e-mail/Box account to join the enterprise (stays `pending`; an external placeholder user is created) |
| GET, POST | `/groups` | admin only; names are unique |
| GET, PUT, DELETE | `/groups/{id}` | members may read their group |
| GET | `/groups/{id}/memberships`, `/groups/{id}/collaborations` | |
| POST | `/group_memberships` | `role` member\|admin; 409 if already a member |
| GET, PUT, DELETE | `/group_memberships/{id}` | |

### Folders

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/folders/{id}` | `0` = the caller's root ("All Files"); `item_collection` in the standard fields; `If-None-Match` -> 304 |
| GET | `/folders/{id}/items` | `fields`, `limit` (default 100, max 1000), `offset`, `usemarker`/`marker`, `sort=id\|name\|date\|size`, `direction`; folders first then name by default |
| POST | `/folders` | `{name, parent: {id}}`; 409 `item_name_in_use` with `context_info.conflicts` (a list); name validation |
| PUT | `/folders/{id}` | rename, move (`parent`), `description`, `tags`, `shared_link`, `folder_upload_email`, `collections`; `If-Match` -> 412; cannot move into a descendant |
| DELETE | `/folders/{id}?recursive=true` | moves to trash; non-empty without `recursive` is 400 `folder_not_empty` |
| POST | `/folders/{id}/copy` | deep copy; 409 on name clash |
| GET | `/folders/{id}/collaborations` | |
| GET | `/folders/trash/items` | the caller's trash (items they own), same paging/sorting |
| GET, POST, DELETE | `/folders/{id}/trash`, `/folders/{id}` (restore), `/folders/{id}/trash` (purge) | restore takes optional `{name, parent}`; 409 on clash, 403 when the old parent is gone |
| GET, POST, PUT, DELETE | `/folders/{id}/metadata/{scope}/{template}`, `/folders/{id}/watermark` | |
| POST, GET, DELETE | `/folder_locks`, `/folder_locks?folder_id=`, `/folder_locks/{id}` | freeze move/delete of a folder (owner/co-owner) |

### Files

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/files/{id}` | standard fields; `fields=` adds `extension`, `version_number`, `comment_count`, `permissions`, `tags`, `lock`, `has_collaborations`, `is_externally_owned`, `collections`, `metadata.<scope>.<template>`, `classification`, `watermark_info`, `representations` ... |
| PUT | `/files/{id}` | rename, move, `description` (<= 256), `tags`, `lock` (`{access: "lock", expires_at, is_download_prevented}` or `null`), `shared_link`, `collections`; `If-Match` |
| DELETE | `/files/{id}` | to trash; 204; `If-Match`; blocked by another user's lock |
| GET | `/files/{id}/content` | `302` to `https://dl.boxcloud.com/d/1/b1!<token>/download` (valid 15 minutes, no auth needed, supports `Range`, `HEAD`); `?version=<id>`; `?access_token=`; honours `BoxApi`, locks with `is_download_prevented` |
| POST | `/files/{id}/copy` | `{parent, name, version}` |
| GET, POST, DELETE | `/files/{id}/trash`, `/files/{id}` (restore), `/files/{id}/trash` (purge) | |
| GET | `/files/{id}/versions`, `/files/{id}/versions/{vid}` | previous versions, newest first (the current version is `file_version` on the file); historical bytes are rendered lazily |
| POST | `/files/{id}/versions/current` | promote an old version (creates a new version) |
| PUT, DELETE | `/files/{id}/versions/{vid}` | restore (`{trashed_at: null}`) / trash a version; the current version cannot be deleted |
| GET | `/files/{id}/thumbnail.png`, `.jpg` | PNG for both; sizes 32/64/128/256 (+ 94/160/320/1024/2048 for jpg) else 400; types that cannot be rendered (`.bin`, `.zip`, ...) get `302` to a generic icon on `app.box.com/static/img/file_icons/` |
| GET | `/files/{id}/collaborations`, `/comments`, `/tasks` | |
| GET, POST, PUT, DELETE | `/files/{id}/metadata/{scope}/{template}`, `/files/{id}/watermark` | |
| GET | `/internal_files/{id}/versions/{vid}/representations/{name}` and `https://public.boxcloud.com/api/2.0/internal_files/.../representations/{name}/content/...` | with `X-Rep-Hints: [pdf][extracted_text][png?dimensions=...]`; PDF, text and PNG are generated (the jpg hint serves PNG bytes) |
| OPTIONS | `/files/content`, `/files/{id}/content` | pre-flight: 200 `{upload_token, upload_url}` or 409 / 413 |

### Uploads (`https://upload.box.com/api/2.0`)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/files/content` | multipart `attributes` (`name`, `parent.id`, `content_created_at`, `content_modified_at`, `description`) + `file`; `Content-MD5` (the SHA-1 hex) is verified (400 `bad_digest`); 409 with `context_info.conflicts` as an object; 413 above 50 MB; returns `{total_count: 1, entries: [file]}` |
| POST | `/files/{id}/content` | new version; `If-Match`; optional `attributes.name` rename |
| POST | `/files/upload_sessions`, `/files/{id}/upload_sessions` | `{folder_id, file_size, file_name}` -> `{id, part_size, total_parts, num_parts_processed, session_endpoints, session_expires_at}` |
| PUT | `/files/upload_sessions/{id}` | one part: `Digest: sha=<base64 sha1>` and `Content-Range: bytes a-b/total`; parts in any order; returns `{part: {part_id, offset, size, sha1}}` |
| GET | `/files/upload_sessions/{id}`, `/files/upload_sessions/{id}/parts` | status, list parts (`total_count`) |
| POST | `/files/upload_sessions/{id}/commit` | `Digest: sha=<base64 sha1 of the whole file>` (mismatch is 400 `bad_digest`), body `{parts}`; 201 `{entries: [file]}` |
| DELETE | `/files/upload_sessions/{id}` | abort |

The default `part_size` is 8 MiB. Use `POST /_emulator/actions/configure_uploads {"part_size": 1000}` to make
tests exercise multi-part uploads with tiny payloads.

### Collaborations and shared links

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/collaborations` | `{item: {type, id}, accessible_by: {type: user\|group, id \| login}, role, expires_at}`; managed users are `accepted`, external or unknown logins become `pending` invitations (an external account is created for an unknown e-mail); `user_already_collaborator`; `co-owner` needs an owner/co-owner inviter; `notify` is accepted and ignored |
| GET, PUT, DELETE | `/collaborations/{id}` | PUT changes `role`, `expires_at`, and lets the invitee set `status` `accepted`/`rejected` (rejecting returns 204 and removes it); collaborators can delete themselves |
| GET | `/collaborations?status=pending` | the caller's pending invitations |
| GET | `/shared_items` | `BoxApi: shared_link=<url>&shared_link_password=<pw>`; open links need no token; `company` needs a managed user; wrong password 403; expired/removed 404 |

Shared links are set with `PUT /files/{id}?fields=shared_link` (or `/folders/{id}`) and
`{"shared_link": {"access": "open|company|collaborators", "password", "unshared_at", "vanity_name",
"permissions": {"can_download", "can_edit"}}}`, removed with `{"shared_link": null}`. Links look like
`https://app.box.com/s/<32 hex>`; files also get `download_url` (`/shared/static/<hash>.<ext>`). Setting an
`open` or `company` link needs `can_set_share_access` (editor and above); viewers can only manage
`collaborators` links.

### Search

`GET /search` - `query` (words are ANDed; `AND`/`OR`/`NOT`, quoted phrases, word-prefix matching,
case-insensitive), `type`, `file_extensions`, `ancestor_folder_ids`, `content_types=name,description,
file_content,comments,tag`, `created_at_range`/`updated_at_range` (`start,end`), `size_range`,
`owner_user_ids`, `recent_updater_user_ids`, `trash_content`, `mdfilters`, `scope`
(`enterprise_content`: admin only), `sort=relevance|modified_at`, `direction`, `limit` (30, max 200),
`offset`. Returns `{total_count, limit, offset, entries, type: "search_results_items"}`; only items the
caller can see are searched. File content search covers the text the world generator put in each document.

`POST /metadata_queries/execute_read` - `from` (`enterprise_336533359.clientInfo`), `ancestor_folder_id`,
`query` (`clientName = :name AND fiscalYear >= :year`, `LIKE`, `OR`, `NOT`, parentheses), `query_params`,
`order_by`, `limit` (max 100), `marker`, `fields`.

### Events

* `GET /events?stream_type=all|changes|sync|admin_logs|admin_logs_streaming&stream_position=now|0|<n>&limit=`
  -> `{chunk_size, next_stream_position, entries}`. Positions are monotonically increasing numeric strings.
  User streams (`all`, `changes`, `sync`) carry events the caller caused, on items they own, or on items
  they can currently see; `changes`/`sync` carry only tree-changing events. `admin_logs` is enterprise wide
  (admin, co-admin and the service account only) with `event_type`, `created_after`, `created_before`.
* Event types: `ITEM_CREATE`, `ITEM_UPLOAD`, `ITEM_DOWNLOAD`, `ITEM_MOVE`, `ITEM_COPY`, `ITEM_TRASH`,
  `ITEM_UNDELETE_VIA_TRASH`, `ITEM_RENAME`, `DELETE`, `LOCK`, `UNLOCK`, `COLLAB_ADD_COLLABORATOR`,
  `COLLAB_INVITE_COLLABORATOR`, `COLLAB_ROLE_CHANGE`, `COLLAB_REMOVE_COLLABORATOR`, `COLLAB_ACCEPT`,
  `COLLAB_REJECT`, `ITEM_SHARED_CREATE`, `ITEM_SHARED_UPDATE`, `ITEM_SHARED_UNSHARE`, `COMMENT_*`,
  `TASK_ASSIGNMENT_*`, `METADATA_INSTANCE_*`, `NEW_USER`, `EDIT_USER`, `DELETE_USER`, `GROUP_CREATION`,
  `GROUP_DELETION`, `GROUP_ADD_USER`, `GROUP_REMOVE_USER`. The seeded history (about 1,100 events, oldest
  first, dated like the world's documents) is available from position `0`.
* `OPTIONS /events` returns `{chunk_size: 1, entries: [{type: "realtime_server", url, ttl, max_retries,
  retry_timeout}]}`. The URL (`https://realtime.box.com/subscribe?channel=...`) is a long-poll endpoint: it
  answers `{"message": "new_change"}` as soon as an event visible to the channel's user is newer than the
  `stream_position` query parameter (or than the moment the channel was created), otherwise
  `{"message": "reconnect"}` after `EMULATORS_BOX_LONGPOLL_TIMEOUT` seconds (default 3). The SDK's
  `get_event_stream()` iterator works.

### Webhooks v2

`POST /webhooks {target: {id, type: file|folder}, address, triggers[]}`, `GET /webhooks`,
`GET|PUT|DELETE /webhooks/{id}`. The caller needs edit rights on the target; duplicates (same target and
address) are 409; unknown triggers are 400. `address` may be `http://` (real Box requires HTTPS). Valid
triggers: `FILE.UPLOADED`, `FILE.PREVIEWED`, `FILE.DOWNLOADED`, `FILE.TRASHED`, `FILE.DELETED`,
`FILE.RESTORED`, `FILE.COPIED`, `FILE.MOVED`, `FILE.RENAMED`, `FILE.LOCKED`, `FILE.UNLOCKED`,
`FOLDER.CREATED`, `FOLDER.DOWNLOADED`, `FOLDER.RESTORED`, `FOLDER.DELETED`, `FOLDER.COPIED`,
`FOLDER.MOVED`, `FOLDER.TRASHED`, `FOLDER.RENAMED`, `COMMENT.CREATED|UPDATED|DELETED`,
`TASK_ASSIGNMENT.CREATED|UPDATED`, `METADATA_INSTANCE.CREATED|UPDATED|DELETED`,
`COLLABORATION.CREATED|ACCEPTED|REJECTED|REMOVED|UPDATED`, `SHARED_LINK.CREATED|UPDATED|DELETED`,
`WEBHOOK.DELETED`.

A webhook on a folder fires for events on the folder and everything beneath it (including nested uploads);
a webhook on `0` covers the creator's whole tree; a file webhook only its file.

Deliveries (`ctx.webhooks.deliver`, so `EMULATORS_WEBHOOK_MODE` and `GET /_emulator/webhooks` apply; three
attempts with the same delivery id) are JSON:

```json
{"type": "webhook_event", "id": "<delivery uuid>", "created_at": "2026-10-16T00:26:54-07:00",
 "trigger": "FILE.UPLOADED", "webhook": {"id": "1000000001", "type": "webhook"},
 "created_by": {"type": "user", "id": "...", "name": "...", "login": "..."},
 "source": {"type": "file", "id": "...", "name": "...", "...": "the file object"}}
```

with headers `BOX-DELIVERY-ID`, `BOX-DELIVERY-TIMESTAMP` (`2026-10-16T07:26:54.752Z`),
`BOX-SIGNATURE-VERSION: 1`, `BOX-SIGNATURE-ALGORITHM: HmacSHA256`, `BOX-SIGNATURE-PRIMARY` and
`BOX-SIGNATURE-SECONDARY` where each is `base64(hmac_sha256(key, body_bytes + timestamp_bytes))`.
`WebhooksManager.validate_message` from the SDK accepts them. Keys default to the values in the credentials
table; change them with the `configure_webhook_keys` action (an empty secondary key omits that header).

### Metadata, comments, tasks and other resources

| Area | Endpoints |
| --- | --- |
| Metadata templates | `GET /metadata_templates/enterprise`, `/global`, `?metadata_instance_id=`, `/{scope}/{key}/schema`, `/{id}`; admin `POST /metadata_templates/schema`, `PUT` (JSON patch ops `addField`, `editField`, `removeField`, `reorderFields`, `editTemplate`, `add/removeEnumOption`, `add/removeMultiSelectOption`), `DELETE` |
| Metadata instances | `GET/POST/PUT/DELETE /files/{id}/metadata/{scope}/{template}` and `/folders/...`, `GET .../metadata`; `scope` is `enterprise`, `enterprise_336533359` or `global` (`properties`, free-form); updates are RFC 6902 (`application/json-patch+json`: add, replace, remove, test, move, copy); typed validation for string/float/date/enum/multiSelect |
| Comments | `GET /files/{id}/comments`, `POST /comments` (`item.type` file or comment for replies; `tagged_message` mentions), `GET/PUT/DELETE /comments/{id}` |
| Tasks | `POST /tasks`, `GET/PUT/DELETE /tasks/{id}`, `GET /files/{id}/tasks`, `GET /tasks/{id}/assignments`, `POST /task_assignments`, `GET/PUT/DELETE /task_assignments/{id}` |
| Web links | `POST /web_links`, `GET/PUT/DELETE /web_links/{id}` (+ trash, restore, purge); they appear in folder listings |
| Collections | `GET /collections` (one "Favorites" per user), `/collections/{id}`, `/collections/{id}/items`; set with `collections: [{id}]` on a file/folder update |
| Recent items | `GET /recent_items` (marker paging) |
| Zip downloads | `POST /zip_downloads` (202), `GET /zip_downloads/{id}/status`, content on `dl.boxcloud.com`; duplicate names get `name (1).ext` and are reported in `name_conflicts` |
| Watermarks | `GET/PUT/DELETE /files/{id}/watermark`, `/folders/{id}/watermark`; folder watermarks are inherited (`watermark_info`) |
| Classification | the standard `securityClassification-6VMVochwUWo` template (Public / Internal / Confidential) and the `classification` field |

## Seeded data

Everything is projected from the shared world (deterministic; the full seed takes about half a second).

* **Users** (70): 45 active employees + 2 departed (`inactive`) as managed users with `login` = e-mail,
  `job_title`, office address, phone, language and time zone; Arjun Mehta `admin`, Sarah Johansson `coadmin`
  (`box-dev-p035`, `box-dev-p036`). 22 client contacts who collaborate on files are external users
  (`enterprise: null`), e.g. `christopher.ingram@northwindlogistics.example`. One service account
  ("Halcyon Box Automation", co-owner of every team folder like a real integration).
  Example ids: Arjun `33529030912`, Maya Chen `34654717680`, Daniel Kowalski `33724305150`.
* **Groups** (13 managed groups): the 8 departments, All Staff, Partners, Leadership Team, People Managers
  and IT Administrators (office distribution lists are not Box groups); department heads are group admins.
* **Folders and files**: every team space is a top-level folder owned by Arjun Mehta (Client Engagements
  `236134373492`, Finance & Operations `327421805956`, Knowledge Base `367016944915`, Marketing & BD
  `275093781657`, Partner Group `235497581988`, People & Culture `141326474200`, Technology
  `404466958548`) shared with the space's groups (owner -> co-owner, editor -> editor, viewer -> viewer; the
  world's space owner is an additional co-owner). Personal spaces are folders (`Notes`, `Expenses`, `Drafts`)
  directly in each employee's root. 359 files with real DOCX/XLSX/PPTX/PDF/CSV/MD/PNG bytes (rendered at seed
  time; sizes and SHA-1s match the content), each with 1-7 versions (older versions are rendered lazily as
  shorter drafts). Two documents with identical names in one folder are disambiguated (`Client Onboarding
  Form (2).pdf`) because Box names are unique per folder. Starred documents are in the owner's "Favorites".
* **Sharing**: `Document.shares` are collaborations on the file (`viewer`/`commenter` -> `viewer`, `editor`
  -> `editor`); shares to client contacts carry the share's expiry and about a quarter are still `pending`
  invitations (5 external users have one). `anyone`/`organization` link shares are shared links with
  `access` `open`/`company`.
* **Restricted documents** (`Document.restricted`): modelled as files in a `Restricted/<space>/<folder>`
  folder in the owner's own root (Box cannot break inheritance), shared only with the people named in the
  document's shares. Example: `Salary Bands 2026.xlsx` (owner Olivia Grant, shared with Eleanor Whitfield and
  Victoria Hale) is invisible to the rest of People & Culture and to admins.
* **Metadata**: enterprise template `clientInfo` (`clientName` string, `engagementType` enum, `fiscalYear`
  float) applied to the 242 files in client folders; classification labels on restricted, leadership,
  finance, people, IT and knowledge files.
* **Social**: 24 review comments and 8 review tasks on deliverables, 148 recent-item rows, ~1,100 events.

## Actions (`POST /_emulator/actions/<name>`)

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `{user, expires_in, refresh, client_id}` | Box-shaped token for a user (box id, person id, e-mail) or the service account (`enterprise`); also backs `POST /_emulator/tokens` |
| `configure_webhook_keys` | `{primary, secondary}` | replaces the signature keys (reset restores the defaults) |
| `configure_uploads` | `{part_size}` | chunked-upload `part_size` in bytes |
| `simulate_upload` | `{user, parent_id, name, content \| content_base64}` | another user uploads (a new version when the name exists), firing events and webhooks |
| `accept_collaboration` | `{collaboration_id}` | the invitee accepts a pending invitation, firing `COLLAB_ACCEPT` / `COLLABORATION.ACCEPTED` |
| `expire_token` | `{token}` | revokes an access or refresh token |

The standard control plane (`/_emulator/reset`, `/state`, `/requests`, `/faults`, `/webhooks`...) is
available as on every provider. Injected `429`/`401` faults carry `Retry-After` / `WWW-Authenticate`;
faults on `/oauth2/*` are OAuth-shaped.

## Known gaps and deliberate differences

* Not implemented: Box AI, Box Sign, Hubs, Doc Gen, retention policies, legal holds, terms of service,
  storage policies, shield information barriers, device pinners, collaboration allowlists, integration
  mappings, file requests, skills cards, e-mail aliases, `GET /shared_items` for web links, ownership
  transfer through `role: owner`, and enterprise admin-console settings. Notifications (`notify`) are ignored.
* Chunked sessions accept any `file_size >= 1` and any `part_size` (real Box requires files of at least
  20 MB and 8 MB parts); the part size is configurable for tests.
* Simple uploads are limited to 50 MB; sessions to 50 GB. `GET .../thumbnail.jpg` and `jpg` representations
  return PNG bytes. Representations are generated on demand and never "pending".
* Scopes of the registered application are not enforced for regular tokens; only downscoped
  (`restricted_to`) tokens are scope-checked. JWT assertions are not signature-verified; CCG/JWT subjects
  must exist (a wrong enterprise id is `invalid_grant`).
* Timestamps are rendered in `America/Los_Angeles`, as Box does for its default region.
* Seeded events keep a reference to their item and render the *current* item as `source`; runtime events keep
  the snapshot taken when they happened. Event retention is unbounded.
* `item_status: "deleted"` / `purged_at` are not used: purging removes the item entirely.
* A shared link's `vanity_url` (`https://app.box.com/v/<name>`) is accepted in the `BoxApi` header, but only
  `/s/<hash>` has a (minimal HTML) landing page; password-protected landing pages are not interactive.
* Seeded ids are derived from the world (stable across runs); runtime ids count up from fixed bases
  (files `5000000000001`, folders `500000000001`, versions `9000000000001`).
