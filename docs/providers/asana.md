# Asana

The Asana emulator serves `app.asana.com`, using `/api/1.0` for resource APIs and
`/-/oauth_authorize`, `/-/oauth_token`, and `/-/oauth_revoke` for OAuth. Its dedicated
HTTP port is **44117**. Gateway prefix mode uses `/asana/api/1.0` and
`/asana/-/oauth_token`. Pagination links, file URLs, and permalinks use the gateway's
URL helpers in hostname, prefix, and dedicated-port modes.

## Authentication and permissions

Use `Authorization: Bearer asana-dev-p018` for Maya Chen or `asana-dev-p035` for Arjun
Mehta. Every active world employee also has a deterministic `asana-dev-<world ID>`
PAT. These synthetic PATs have no expiry. Control-plane tokens can be minted with
`POST /_emulator/tokens` and a JSON body such as
`{"user":"p018","scopes":["tasks:read"],"expires_in":3600}`. Users accept a world ID,
Asana gid, or employee email. App-only grants are rejected.

OAuth implements authorization codes, state, redirect binding, PKCE (`plain` or
`S256`), reusable refresh tokens, and refresh-token revocation of linked access
tokens. Supply `login_hint=maya.chen@halcyonadvisory.example` for headless consent;
otherwise the authorization endpoint presents the account picker. Clients are
lenient by default, accepting arbitrary local client IDs and nonempty secrets.
Token and revocation requests must be form encoded. Access tokens expire after
3600 seconds; refreshing preserves the refresh token. The `default` scope grants
the user's supported API surface. Resource-specific read/write/delete scopes
restrict route access. Invalid, expired, and revoked access tokens return Asana's
`errors` envelope with HTTP 401; insufficient scopes and inaccessible objects use
403; nonexistent or deleted objects use 404.

Private teams and projects enforce membership. Public workspace projects are
visible to active employees. Tasks are visible through an accessible project,
their assignee, collaborators (`followers`), or their parent task. Creating a task
adds the creator as a collaborator; after removing that collaboration the creator
has no permanent access privilege. Sharing one task does not expose its private
project: task project/membership fields are filtered for the caller. Comments and
attachments inherit their parent resource's permissions.

## Endpoints

All resource paths below are relative to `/api/1.0`. Creates return 201; ordinary
reads, updates, relationship actions, and deletes return 200. Deletes return
`{"data":{}}`.

| Method | Path | Supported behavior |
| --- | --- | --- |
| GET | `/users`, `/users/{gid}` | Compact lists; `me`; workspace/team filters; full user reads |
| GET | `/workspaces`, `/workspaces/{gid}`, `/users/{gid}/workspaces` | Shared organization and membership |
| GET | `/workspaces/{gid}/users` | World employee projection, sorted by gid |
| GET | `/organizations/{gid}/teams`, `/users/{gid}/teams` | Organization and user teams; user route requires organization |
| POST | `/teams` | Create an organization team |
| GET, PUT | `/teams/{gid}` | Retrieve or edit a team |
| GET | `/teams/{gid}/users` | Team members |
| POST | `/teams/{gid}/addUser`, `/teams/{gid}/removeUser` | Membership and private-team access |
| GET, POST | `/projects`, `/workspaces/{gid}/projects`, `/teams/{gid}/projects` | Visible projects, archived filtering, project creation |
| GET, PUT, DELETE | `/projects/{gid}` | Names, notes, privacy, owner, dates, archive, view and lifecycle |
| POST | `/projects/{gid}/addMembers`, `/removeMembers`, `/addFollowers`, `/removeFollowers` | Atomic relationship changes; project owner cannot be removed |
| GET | `/projects/{gid}/project_memberships` | Stable membership IDs and access level |
| GET, POST | `/projects/{gid}/sections` | Ordered sections and insertion relative to another section |
| GET, PUT, DELETE | `/sections/{gid}` | Section lifecycle; deleting a section leaves tasks in the project |
| POST | `/sections/{gid}/addTask` | Project/section placement and task ordering |
| GET, POST | `/tasks` | Vendor filter rules; workspace inference on creation |
| GET | `/projects/{gid}/tasks`, `/sections/{gid}/tasks`, `/tags/{gid}/tasks` | Ordered, visible task collections |
| GET, PUT, DELETE | `/tasks/{gid}` | Assignees, notes/rich text, completion, due/start dates, milestones and approvals |
| GET, POST | `/tasks/{gid}/subtasks` | Subtasks inherit parent visibility, with no automatic project membership |
| POST | `/tasks/{gid}/setParent` | Parent changes with cycle prevention |
| POST | `/tasks/{gid}/addProject`, `/removeProject` | Multiple projects, section selection, insertion order |
| POST | `/tasks/{gid}/addFollowers`, `/removeFollowers` | Collaborators and private-task access |
| GET | `/tasks/{gid}/dependencies`, `/dependents` | Visible dependencies and reverse relationships |
| POST | `/tasks/{gid}/addDependencies`, `/removeDependencies`, `/addDependents`, `/removeDependents` | Cycle prevention; maximum 30 combined links on either task; atomic bulk validation |
| POST | `/tasks/{gid}/addTag`, `/removeTag` | Client/task tags |
| GET, POST | `/tasks/{gid}/stories` | Paginated comments; plain text or `<body>` rich text |
| GET, PUT, DELETE | `/stories/{gid}` | Comment lifecycle; author-only edit/delete |
| GET, POST | `/tags`, `/workspaces/{gid}/tags` | Tag collection and creation |
| GET, PUT, DELETE | `/tags/{gid}` | Tag lifecycle; deletion removes task relationships |
| GET, POST | `/attachments`, `/tasks/{gid}/attachments` | Parent-scoped lists and multipart uploads, maximum 100 MiB |
| GET, DELETE | `/attachments/{gid}` | Metadata, signed download URL, deletion |
| GET | `/attachments/{gid}/download` | Signed one-hour capability URL; actual bytes and single byte ranges |
| GET, POST | `/webhooks` | Workspace-scoped subscription list and handshake creation |
| GET, DELETE | `/webhooks/{gid}` | Token-bound subscription access and deletion |

Task lists require a project or tag, or both assignee and workspace. Section task
paths are supported. Lists support integer `limit` values from 1 through 100 and
opaque offsets bound to the principal, client, scopes, route, and filter/projection
query. Responses contain `next_page.offset`, `next_page.path`, and `next_page.uri`,
or `next_page: null`. Cursors preserve the original ordering and recheck current
visibility; resetting the provider invalidates them.

List responses use vendor compact records. `opt_fields` selects exactly the
requested fields plus `gid`, including nested expansions such as `assignee.email`,
`memberships.section.name`, and `(owner|followers).email`. JSON writes accept a
top-level `data` object and optional sibling `options.fields`; supported ordinary
writes also accept form encoding. Boolean, date, rich-text, ID-array, nonfinite JSON,
and Unicode validation precedes persistence. Approval tasks start `pending` and
keep `approval_status`, `completed`, `completed_at`, and `completed_by` consistent.
Start-date writes and clears require the corresponding due date in the same
request; project start and due dates cannot be the same day.

## Seeded world

At seed 42 and anchor `2026-10-16`, the provider projects:

- 45 active employees; Maya's gid is `1500411220738629`, Arjun's is `2133150209354679`.
- One organization workspace, Halcyon Advisory Group, gid `1001199997837236`.
- 15 world engagement/department teams, 30 engagement projects, and 90 sections.
- 359 document tasks, each with a real document attachment, and 24 client tags.

Seeded IDs are deterministic numeric strings derived from world IDs. Runtime IDs
are deterministic sequence strings beginning at `9900000000000000` after reset.
Restricted document tasks preserve their explicit world sharing. Document bodies
are rendered lazily into the content-addressed blob store; multipart uploads retain
their exact bytes. A measured cold provider construction and seed took 0.304 seconds
on the development host. Reset restores seeded content and removes runtime writes,
tokens, cursors, and subscriptions.

## Webhooks and actions

Creating a webhook sends a dispatcher POST with `X-Hook-Secret` to the configured
target. The target must return 200 or 204 and echo the same header before the
subscription is stored. The 201 response includes the secret once at the top level.
Deliveries sign the exact JSON bytes using HMAC-SHA256 in `X-Hook-Signature`.

Task, project, and workspace subscriptions receive visible task changes, comments,
and attachments. Filters match `resource_type`, `resource_subtype`, `action`, and
changed `fields`. Field filters require `action="changed"` and are rejected on
workspace subscriptions before the handshake. Workspace subscriptions require
nonempty filters. Delivery ceases when access is lost; revoked subscription tokens
and HTTP 410 responses remove the subscription. The dispatcher's journal records
handshakes, attempts, and deliveries.

`POST /_emulator/actions/simulate_task_change` accepts
`{"user":"p018","task_gid":"...","changes":{"completed":true}}` and uses the
normal mutation validation and signed delivery path. `expire_token` accepts
`{"token":"..."}`; `mint_token` exposes the same minting behavior as the token
control route.

## Official SDK verification

The official Python `asana` SDK 5.4.0 uses urllib3, which the shared httpx/requests
interceptor does not rewrite. Its tests use the SDK's supported
`Configuration.host = emulator_server.url_for("asana") + "/api/1.0"` setting and
real HTTP against the gateway. They cover automatic pagination, exact projections,
task/project/subtask/dependency/approval lifecycle, comments, multipart upload and
binary download, and vendor-shaped auth/ACL/validation errors. No SDK code is patched.

```powershell
uv run --frozen pytest tests/providers/asana -m 'not e2e' -q
$env:EMULATORS_PROVIDERS='asana'
uv run --frozen --with asana pytest tests/sdk/test_asana_sdk.py -q
```

## Known gaps

- Portfolios, goals, custom fields/types, time tracking, rules, templates, task search,
  jobs, batch requests, SCIM, guest invitation, and external URL attachments are not
  implemented. Unsupported task/project write fields are rejected.
- Project roles are simplified to owner/admin and editor membership. The full
  enterprise viewer/commenter restrictions, administrative policies, and
  field-specific OAuth scopes are not modeled. There is one organization workspace.
- Deleted tasks remain internally marked for consistent 404s; trash restoration,
  automatic 30-day purge, and cascading deletion/restoration of subtasks are absent.
- Collections default to 100 results. The vendor's legacy unpaginated responses for
  requests without `limit`, cursor expiration schedules, and API rate limiting are
  not simulated. `opt_pretty` is validated but formatting is not changed.
- Webhook delivery is immediate and uses the shared dispatcher's retry settings,
  without Asana's production aggregation delays, eight-hour heartbeats, 24-hour
  retry schedule, or every generated system-story event. Team/portfolio webhooks
  and webhook updates are absent. Local HTTP targets are allowed for testing.
- Document download URLs use the provider host rather than Asana's production CDN.
  URLs are capability links until expiry or attachment deletion; only single byte
  ranges are supported. Local upload size uses 100 MiB.
- The optional SDK is installed through a frozen temporary `--with asana` overlay;
  it is not added to the repository lockfile. Hostname-preserving urllib3
  interception is not supplied by this provider.

## Primary references

- [Asana API documentation index](https://developers.asana.com/llms.txt) and
  [official OpenAPI schema](https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml).
- [Input/output options](https://developers.asana.com/docs/inputoutput-options),
  [pagination](https://developers.asana.com/docs/pagination), and
  [OAuth](https://developers.asana.com/docs/oauth).
- [Task listing](https://developers.asana.com/reference/gettasks),
  [task creation](https://developers.asana.com/reference/createtask),
  [dependency creation](https://developers.asana.com/reference/adddependenciesfortask), and
  [privacy and visibility](https://help.asana.com/s/article/understanding-privacy-and-visibility-in-asana).
- [Attachment upload](https://developers.asana.com/reference/createattachmentforobject),
  [webhook creation](https://developers.asana.com/reference/createwebhook), and
  [webhook delivery guide](https://developers.asana.com/docs/webhooks-guide).
