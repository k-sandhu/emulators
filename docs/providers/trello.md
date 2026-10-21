# Trello

Hosts: `api.trello.com`, `trello.com`, `www.trello.com`. Dedicated port: **44118**.
The REST prefix is `/1`; gateway prefix mode is `/trello/1`. All returned board,
card, member and attachment URLs adapt to host, gateway-prefix and dedicated-port access.

## Authentication and test credentials

Use Trello's `key` and `token` query parameters, form fields or JSON properties.
The same pair is accepted in an `Authorization: OAuth oauth_consumer_key="...",
oauth_token="..."` header. Bearer headers accept emulator member grants.

| Credential | Value |
| --- | --- |
| API key | `c41e577a7b68e5b6fe81493f32834b9b` |
| Default token | `92f7d63df38432dda88893cf9b07c2a805cbd0cd95ff0a802477b25121f6bc5c` |
| Application secret | `halcyon-trello-application-secret` |
| Default member | Eleanor Whitfield, `eleanor.whitfield@halcyonadvisory.example` (`p001`) |

These fictional credentials are also returned by `GET /_emulator/info`.
The default member token has `read,write,account` scopes and does not expire.
`POST /_emulator/tokens` and `/_emulator/actions/mint_token`
accept `user` (world ID, member ID, username or email), `scopes`, `client_id`,
`client_secret` and `expires_in`. The result includes `token`, `access_token`,
`key`, `idMember`, `expires_in` and `scope`. App-only tokens are rejected because
Trello permissions belong to a member.

`read` allows REST reads; `write` allows mutations. Reading the authenticated
member's email additionally requires `account`; other members' email addresses
are not exposed. Invalid keys produce plain-text `401 invalid key`; missing,
expired, revoked or mismatched tokens produce `401 invalid token`. Permission
failures use `401 unauthorized permission requested`, as in the legacy Trello API.
Key-only reads can access public boards and their resources.

`GET https://trello.com/1/authorize` supports personal-token authorization with
`response_type=token`, `scope=read,write,account`, `expiration=1hour|1day|30days|never`,
`return_url`, and `callback_method=fragment`. `login_hint` selects a local world
member; without it the endpoint displays a small local account-selection notice.
A token is returned in the redirect fragment, or displayed when no return URL is supplied.

## Seeded data and permissions

With the repository's `2026-10-16` anchor, the deterministic seed contains:

| Resource | Count | World projection |
| --- | ---: | --- |
| Members | 131 | 45 active employees and active world client contacts; departed staff excluded |
| Workspaces (`organizations`) | 8 | Halcyon Advisory Group plus seven collaboration teams |
| Boards | 38 | Operations, seven team boards and 30 engagement boards |
| Lists | 114 | Three workflow lists per board |
| Cards | 54 | 24 client onboarding cards plus one delivery card per engagement |
| Labels | 114 | Client work, Ready and Needs attention on each board |
| Checklists / items | 30 / 90 | Scope confirmation, deliverable review and client approval |
| Attachments | 60 | Unrestricted, non-trashed engagement documents, rendered lazily |
| Actions | 30 | Engagement-lead comments matching the engagement's world status |

The main workspace includes every active employee. Private boards require explicit
board membership; workspace membership alone does not reveal private engagement
boards. Engagement leads are board administrators and their engagement teams are
normal members. Team workspaces and boards use the world's actual owner/member sets.
The operations board includes all active staff. Eleanor is its administrator;
Maya Chen (`p018`, `maya.chen@halcyonadvisory.example`) administers her engagements.

`normal` and `admin` board members can modify cards; `observer` members can read.
Only board administrators change memberships and board metadata. Workspace
administration is independent. The last administrator cannot be removed or demoted.
Boards created with `prefs_permissionLevel=org` are readable by workspace members;
`public` boards support key-only reads. Lists, cards, checklists, attachments,
actions, nested expansions, search and batches all retain the board's access checks.

IDs are 24 lowercase hex characters with Mongo-style timestamp prefixes.
Board/card `shortLink` values are stable eight-character identifiers. Each board
maintains its own `idShort` card sequence. Reset restores the complete seed and counters.
The seed takes approximately 0.2 seconds locally; document rendering is deferred until
attachment metadata or download is requested.

## Implemented REST endpoints

All paths below start with `/1`. JSON success bodies follow Trello resource shapes;
creates and deletes return **200**, including empty-object delete responses.
Query, form-encoded and JSON write inputs are accepted; attachment uploads accept multipart.

| Method | Path | Supported behavior |
| --- | --- | --- |
| GET | `/members/{idOrUsername}`; `/members/me` | Public profile, self workspace/board IDs and scoped email; `fields` |
| GET | `/members/{id}/boards[/{filter}]` | Caller-visible member boards; nested resources |
| GET | `/members/{id}/organizations`, `/members/{id}/cards`, `/members/{id}/actions` | Workspaces, assigned cards and visible actions |
| POST | `/organizations` | `displayName`, optional `name`, `desc`, `website`; creator is administrator |
| GET, PUT, DELETE | `/organizations/{idOrName}` | Read, update display name/description/website, delete workspace |
| GET | `/organizations/{id}/boards`, `/members`, `/memberships` | Caller-visible boards and workspace members/roles |
| PUT, DELETE | `/organizations/{id}/members/{idMember}` | Add/update/remove existing world members; `type=normal|admin` |
| POST | `/boards` | `name`, `desc`, `idOrganization`, `defaultLists`, `defaultLabels`, `prefs_permissionLevel` |
| GET, PUT, DELETE | `/boards/{idOrShortLink}` | Read, rename, describe, archive/unarchive, change visibility, delete |
| GET | `/boards/{id}/cards[/{filter}]`, `/lists[/{filter}]` | Open by default; `open`, `closed`, `all`, `visible`, `none` |
| GET | `/boards/{id}/actions`, `/labels`, `/checklists`, `/members`, `/memberships` | Actions, labels, checklists and membership records; memberships support `filter` and `member=true` |
| POST | `/boards/{id}/lists`, `/labels` | Create workflow lists or board labels |
| PUT, DELETE | `/boards/{id}/members/{idMember}` | `normal`, `admin`, `observer`; removing a member also removes card assignments |
| POST | `/lists` | `name`, `idBoard`, `pos` |
| GET, PUT | `/lists/{id}` | Read, rename, reposition and archive/unarchive |
| GET | `/lists/{id}/cards`, `/board`, `/actions` | Cards and related resources |
| PUT | `/lists/{id}/closed`, `/name`, `/pos` | Single-field writes using `value` |
| POST | `/lists/{id}/archiveAllCards` | Archive every card in the list |
| POST | `/cards` | `idList`, `name`, `desc`, `pos`, dates, assignments, labels and optional `urlSource` attachment |
| GET, PUT, DELETE | `/cards/{idOrShortLink}` | Read, edit, move within/between boards, archive/unarchive, delete |
| GET | `/cards/{id}/board`, `/list`, `/members`, `/actions`, `/checklists`, `/checkItemStates` | Related objects and computed checklist completion |
| POST, DELETE | `/cards/{id}/idLabels[/{idLabel}]`, `/idMembers[/{idMember}]` | Add with `value`; remove by path ID |
| POST | `/cards/{id}/actions/comments` | Comment action with `text` |
| PUT, DELETE | `/cards/{id}/actions/{idAction}/comments` | Author or board administrator edits/deletes comments |
| GET | `/actions/{id}`, `/actions/{id}/board`, `/actions/{id}/card` | Action details; related card rechecks its current board ACL |
| GET, POST | `/cards/{id}/attachments` | Attachment metadata; upload `file` or attach an HTTP(S) `url` |
| GET, DELETE | `/cards/{id}/attachments/{idAttachment}` | Attachment details and removal |
| GET | `/cards/{id}/attachments/{idAttachment}/download/{filename}` | Real file bytes; requires OAuth or bearer Authorization header, including public-board downloads |
| POST | `/checklists`, `/cards/{id}/checklists` | `idCard` (top-level route), `name`, `pos` |
| GET, PUT, DELETE | `/checklists/{id}` | Read, rename, reposition, delete |
| GET | `/checklists/{id}/board`, `/cards`, `/checkItems` | Parent resources and ordered items; item `filter` |
| POST | `/checklists/{id}/checkItems` | `name`, `pos`, `checked` |
| GET, DELETE | `/checklists/{id}/checkItems/{idCheckItem}` | Read or delete an item |
| GET, PUT, DELETE | `/cards/{id}/checkItem/{idCheckItem}` | Item name, state, position, due date and board-member assignment |
| DELETE | `/cards/{id}/checklists/{idChecklist}` | Remove checklist |
| POST | `/labels` | `idBoard`, `name`, `color`; standard and light/dark palette colors, or null |
| GET, PUT, DELETE | `/labels/{id}` | Read, rename, recolor, delete; card label references are maintained |
| GET | `/search` | Caller-visible cards, boards, workspaces and members |
| GET | `/batch?urls=...` | Up to ten read-only subrequests, responses keyed by HTTP status |
| GET, DELETE | `/tokens/{token}` | Own token metadata and revocation |
| GET | `/tokens/{token}/member` | Token's member |
| GET, POST | `/webhooks`, `/tokens/{token}/webhooks` | List own hooks or create a verified subscription |
| GET, PUT, DELETE | `/webhooks/{id}` | Token-owned subscription lifecycle |
| GET, PUT | `/webhooks/{id}/{field}` | Read fields; update `description`, `active`, `idModel`, `callbackURL` using `value` |
| DELETE | `/tokens/{token}/webhooks/{id}` | Own webhook removal |

Implemented scalar GET fields on cards, boards, lists, members and checklists return
`{"_value": ...}`. `fields=all|none|comma,separated` projects resource properties.
Card expansions include `board`, `list`, `members`, `attachments=true|cover`,
`checklists=all` and `actions=...`, with the corresponding `*_fields` selectors.
Board expansions include `cards`, `lists`, `members`, `memberships`, `organization`
and `actions`, with field selectors and membership-role filters.

Card `pos` accepts `top`, `bottom` or a positive finite number. Dates use ISO 8601
and normalize to UTC; `due=null` and `start=null` clear dates. Card moves retain
checklists and attachments, keep destination-board members, map labels by name/color,
and allocate a destination `idShort`. Moving with `idBoard` alone selects its first
open list. Source/destination actions use `moveCardFromBoard` and `moveCardToBoard`.
Badges calculate comments, attachment count and checklist completion from current state.
The `visible` card filter excludes archived cards and cards on archived lists or boards;
archiving a list does not change each card's own `closed` field.

Action pages default to 50, with `limit=1..1000`, `before`/`since` Mongo IDs or ISO
dates and optional zero-based `page`. Card collection routes accept `limit`, `before`
and `since`; ordinary list/card ordering uses `pos`. Action IDs remain chronological
for multiple writes in the same second. Paging returns bare arrays, without invented
next-link envelopes.

Search supports plain exact words, quoted phrases and `partial=true` prefixes.
Card filters include `board:`, `list:`, `label:`, `member:`, `is:open|archived|closed`,
`name:` and `description:`. `idBoards`, `idCards`, `idOrganizations`, `modelTypes`,
per-model result limits, `cards_page` and parent/member/attachment card expansions
are supported. Unsupported operators are rejected explicitly.

## Webhooks and simulation actions

Creating a webhook performs a real **HEAD** validation through `ctx.webhooks.request`;
only **200** creates the subscription. Its token must read `idModel`. Supported
models are boards, cards, lists, workspaces and the authenticated member. Duplicate
callback/model/token subscriptions are rejected. Callback updates are revalidated
before applying any other changes.

Deliveries contain `action`, the updated `model` and `webhook`. The exact signature is:

```text
X-Trello-Webhook = base64(HMAC-SHA1(application_secret, raw_body + callbackURL))
```

All deliveries use `ctx.webhooks.deliver` and appear in `/_emulator/webhooks`.
Inactive hooks are skipped. Revocation deletes that token's hooks; expired grants
are removed before the next event. Delivery stops immediately on **410 Gone** and
deletes the webhook. Other failures receive three attempts. Sync, async and manual
dispatcher modes work; failure counters are maintained after actual delivery.

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `user`, `scopes?`, `client_id?`, `expires_in?` | Member token, with permissions described above |
| `post_comment` | `user?`, `card`, `text` | Comment through the normal permission/action/webhook path |
| `move_card` | `user?`, `card`, `idList` | Move through normal card permissions and webhook delivery |

Actions default to Eleanor (`p001`); a board administrator is not automatically
authorized for every other private board. These are control-plane test operations,
not Trello vendor endpoints.

## Known gaps

- OAuth 1 three-legged request/access-token endpoints and cryptographic signature
  verification are not implemented. OAuth headers accept existing emulator tokens.
  OAuth 2 authorization-code, refresh, granular scopes and Power-Up workspace grants
  are absent. Personal-token authorization uses `login_hint`; interactive consent,
  `postMessage`, deny UI and registered return-origin enforcement are absent.
- No Power-Ups, enterprise/billing, notifications, automation, custom fields, voting,
  reactions, stickers, board templates/copying, card/checklist copying, full cover/map
  writes, list moves between boards or invitation emails. Membership writes add
  existing world members only. Most board preferences are informational.
- Actions have functional data/member payloads, without every localized `display`,
  limits or production change subtype. Workspace/member webhooks receive implemented
  board/card actions; organization administration itself does not synthesize the full
  vendor action catalog. No 30-day automatic webhook disabling or token-revocation
  terminal webhook; retries use short local delays instead of Trello's production schedule.
- Search implements the operators above; full Trello grammar, negative/OR operators,
  due-date modifiers, action search and server relevance ranking are absent.
- Seed attachments are real rendered files; generated previews, thumbnail services,
  browser pages and image avatars are not hosted. Uploads are capped at 10 MiB.
  URL attachments remain links and are never fetched. Restricted world documents are
  excluded rather than introducing a card-level file ACL unsupported by Trello.
- Fields cover the implemented model shapes. Arbitrary nested recursion, plugin data,
  every nested-resource parameter and all production validation messages are not modeled.
  Unimplemented write parameters may be ignored or explicitly rejected; they are not
  advertised as supported in the endpoint table.
- Rate limits are injected faults, without automatic counting. Injected 429 responses
  use the documented JSON `API_TOKEN_LIMIT_EXCEEDED` envelope; ordinary authentication,
  permission and validation failures remain plain text. No production quota counters
  or rate-limit remaining headers are synthesized.

## References and verification

The implementation follows Atlassian's [Trello OpenAPI](https://dac-static.atlassian.com/cloud/trello/swagger.v3.json),
[cards](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/),
[boards](https://developer.atlassian.com/cloud/trello/rest/api-group-boards/),
[lists](https://developer.atlassian.com/cloud/trello/rest/api-group-lists/),
[checklists](https://developer.atlassian.com/cloud/trello/rest/api-group-checklists/),
[authorization](https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/),
[nested resources](https://developer.atlassian.com/cloud/trello/guides/rest-api/nested-resources/),
[paging](https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/#paging),
[webhooks](https://developer.atlassian.com/cloud/trello/guides/rest-api/webhooks/),
the Trello team's [authenticated attachment download contract](https://community.developer.atlassian.com/t/update-authenticated-access-to-s3/43681),
and [rate-limit errors](https://developer.atlassian.com/cloud/trello/guides/rest-api/rate-limits/).

```powershell
uv run --frozen pytest tests/providers/trello -q
$env:EMULATORS_PROVIDERS = "trello"
uv run --frozen pytest tests/sdk/test_trello_http.py -q
uv run --frozen ruff check src/emulators/providers/trello tests/providers/trello tests/sdk/test_trello_http.py
uv run --frozen ruff format --check src/emulators/providers/trello tests/providers/trello tests/sdk/test_trello_http.py
```

There is no official Trello Python REST SDK in this environment. The SDK-marked
suite uses unchanged `requests` and `httpx` consumers against real Trello hostnames
through the live socket interception fixture; it does not label unofficial clients official.
