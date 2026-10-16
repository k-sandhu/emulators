# Google OAuth, Directory, Drive and Docs

Google Workspace uses the `google` provider. Gmail, Calendar and Meet are documented in
[google-gmail-calendar-meet.md](google-gmail-calendar-meet.md).

## Hosts and routing

| API | Hosts |
| --- | --- |
| Authorization / discovery | `accounts.google.com` |
| Tokens, token information, revocation | `oauth2.googleapis.com` |
| User information | `openidconnect.googleapis.com`, `www.googleapis.com` |
| Directory | `admin.googleapis.com`, `www.googleapis.com` |
| Drive / uploads | `www.googleapis.com`, `drive.googleapis.com` |
| Docs | `docs.googleapis.com` |

Use the original HTTPS URLs with `hub.async_transport()` or the SDK intercept fixture.
Prefix routing is available at `http://localhost:44080/google`; the dedicated provider
port is `44102`. Generated resource, export, download and OAuth discovery URLs follow
the routing mode. File web links use `drive.google.com` and `docs.google.com` in host mode.

```python
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["google"], anchor="2026-10-16", webhook_mode="sync")
```

## Authentication and test credentials

Client registration is lenient by default: any fictional client ID and secret work.
For example use `1234567890-emulator.apps.googleusercontent.com` / `emulator-secret`.
No real Google credentials are required or used.

Use `GET /o/oauth2/v2/auth` with `response_type=code`, `client_id`, `redirect_uri`,
space-separated `scope`, and `login_hint=maya.chen@halcyonadvisory.example`. It redirects
with `code`, `scope`, `authuser`, `prompt` and the original `state`. Without `login_hint`
it renders an account picker. PKCE S256/plain, `nonce`, and incremental consent are
supported. Exchange the code with a form-encoded POST to `/token`; Basic client
authentication also works. Codes are single use and bound to client and redirect URI.

`access_type=offline` issues a `1//0g...` refresh token. Refresh tokens are reused and
not rotated; refresh responses omit a replacement refresh token. Access tokens start
with `ya29.a0`, last 3599 seconds, and are stored by the shared token service.
`openid` adds an RS256 ID token with numeric `sub`, audience, issuer, profile claims,
`at_hash`, and the authorization nonce. JWKS and the legacy X.509 certificate map allow
the official google-auth library to verify those tokens locally.

Service accounts use the JWT bearer grant with an RS256 assertion, `iss`, `scope`,
token-endpoint `aud`, `iat`, `exp` (at most one hour), and optional `sub`. Assertions
are checked structurally and for claims/time; a real registered signing key is not
required. `sub` impersonates an active internal user. Without impersonation a service
account sees only files explicitly accessible to it and cannot administer Directory.

The control plane offers the same identity without an OAuth round trip:

```http
POST https://www.googleapis.com/_emulator/tokens
Content-Type: application/json

{"user":"p018","scopes":["https://www.googleapis.com/auth/drive.readonly"]}
```

`user` accepts a world ID, email, or numeric Google user ID. `app_only`, `client_id`,
`expires_in` and `subject` are also accepted. `subject` selects the impersonated user.
Bearer headers and the `access_token` query parameter are supported. Explicit Drive,
Docs and Directory scopes are checked; an empty scope list is unrestricted for local
fixtures. Admin Directory writes require an administrator even with a broad scope.

## Endpoints

All paths below are relative to the relevant host. Successful creates return 200
unless otherwise noted; deletes return an empty 204 except OAuth revocation.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/o/oauth2/v2/auth`, `/o/oauth2/auth` | Authorization code redirect or account picker |
| POST | `/token`, `/oauth2/v4/token` | Code, refresh and service-account JWT grants |
| GET, POST | `/tokeninfo` | Access-token or ID-token information |
| GET | `/v1/userinfo`, `/oauth2/v3/userinfo`, `/oauth2/v2/userinfo` | Acting user's OIDC profile; legacy v2 names |
| GET | `/oauth2/v3/certs`, `/oauth2/v1/certs` | JWKS / X.509 PEM map |
| GET | `/.well-known/openid-configuration` | Local OIDC metadata |
| POST | `/revoke` | Revoke associated client/user grants; empty 200 |
| GET, POST | `/admin/directory/v1/users` | Query, sort, page, public/admin view; insert |
| GET, PATCH, PUT, DELETE | `/admin/directory/v1/users/{userKey}` | Email, alias or numeric ID lookup; mutation |
| POST | `/admin/directory/v1/users/{userKey}/makeAdmin` | Set super-admin flag with `{status}` |
| GET | `/admin/directory/v1/users/{userKey}/photos/thumbnail` | URL-safe base64 PNG photo resource |
| GET, POST | `/admin/directory/v1/users/{userKey}/aliases` | List / add aliases |
| DELETE | `/admin/directory/v1/users/{userKey}/aliases/{alias}` | Remove alias |
| GET, POST | `/admin/directory/v1/customer/{customerId}/orgunits` | Hierarchical list / insert |
| GET, PATCH, PUT, DELETE | `/admin/directory/v1/customer/{customerId}/orgunits/{path}` | Nested path or OU ID; rename/move descendants and users |
| GET, POST | `/admin/directory/v1/groups` | Customer/domain/user membership filtering; insert |
| GET, PATCH, PUT, DELETE | `/admin/directory/v1/groups/{groupKey}` | Group email or ID |
| GET, POST | `/admin/directory/v1/groups/{groupKey}/members` | Roles, paging, derived membership; insert |
| GET, PATCH, PUT, DELETE | `/admin/directory/v1/groups/{groupKey}/members/{memberKey}` | Member email or ID |
| GET | `/admin/directory/v1/groups/{groupKey}/hasMember/{memberKey}` | Recursive nested-group membership |
| GET | `/admin/directory/v1/customers/{customerKey}` | Customer metadata; `my_customer` accepted |
| GET | `/admin/directory/v1/customer/{customerId}/domains` | Primary verified domain |
| GET | `/admin/directory/v1/customer/{customerId}/resources/calendars` | Two meeting rooms per office |
| GET | `/drive/v3/about` | Required `fields`; user, quota and format metadata |
| GET, POST | `/drive/v3/files` | Query/sort/page/partial response; metadata creation |
| GET, PATCH, DELETE | `/drive/v3/files/{id}` | Metadata, media download, metadata update, permanent deletion |
| GET | `/drive/v3/files/{id}/export` | Native Docs/Sheets/Slides export; 10 MiB limit |
| POST | `/drive/v3/files/{id}/copy` | Copy binary/native files |
| DELETE | `/drive/v3/files/trash` | Empty the user's trash |
| GET | `/drive/v3/files/generateIds` | Preallocate file IDs |
| POST | `/upload/drive/v3/files` | `uploadType=media`, `multipart` or `resumable` |
| PATCH | `/upload/drive/v3/files/{id}` | Media or resumable update |
| PUT | Resumable `Location` URL | Chunks, `Content-Range`, status query and 308 Range acknowledgement |
| GET, POST | `/drive/v3/files/{id}/permissions` | ACL list / create; fields and pagination |
| GET, PATCH, DELETE | `/drive/v3/files/{id}/permissions/{permissionId}` | Permission mutation; inherited shared-drive grants protected |
| GET, POST | `/drive/v3/drives` | Visible shared drives; idempotent create with `requestId` |
| GET, PATCH, DELETE | `/drive/v3/drives/{id}` | Shared-drive metadata and organizer operations |
| POST | `/drive/v3/drives/{id}/hide`, `/unhide` | User-specific drive visibility preference |
| GET | `/drive/v3/changes/startPageToken` | Current sequenced change cursor |
| GET | `/drive/v3/changes` | Mutations, removals, paging, drive filtering and new cursor |
| POST | `/drive/v3/files/{id}/watch`, `/drive/v3/changes/watch` | Header-only push channels |
| POST | `/drive/v3/channels/stop` | Stop a caller-owned channel |
| GET | `/drive/v3/files/{id}/revisions`, `/revisions/{revisionId}` | Seed head / runtime revisions; binary revision download |
| GET | `/drive/v3/files/{id}/listLabels` | Empty label list |
| POST | `/v1/documents` | Create a native Doc in My Drive |
| GET | `/v1/documents/{id}` | Paragraph/text-run structure with UTF-16 indexes |
| POST | `/v1/documents/{id}:batchUpdate` | Atomic `insertText` / `replaceAllText`; required revision guard |

Directory users default to 100 results, maximum 500. Groups and group members use
200. Drive files and changes default to 100, maximum 1000. Opaque file-list tokens
must be used with the same query. Drive's `fields` grammar supports commas, nesting,
slash paths and wildcards; invalid selectors return a Google-shaped 400.

The Drive `q` parser supports string/boolean/date comparisons, parent and
owner/writer/reader membership, `sharedWithMe`, name/full-text search, link visibility,
property maps, `not`, `and`, `or`, grouping and escaped apostrophes. Ordering supports
multiple keys, descending order and natural names. `corpora=drive` requires `driveId`,
`supportsAllDrives=true` and `includeItemsFromAllDrives=true`. Shared-drive metadata and
binary GETs require `supportsAllDrives=true`. Invisible files return 404; visible files
without write permission return 403 `insufficientFilePermissions`.

## Seeded data

With seed `emulators` and anchor `2026-10-16`, Directory has 47 employee accounts
(45 active and two suspended departed staff), the world directory groups, hierarchical
department OUs, `/Advisory/London` and `/Departed`. All Staff contains department groups;
derived membership expands those nested groups. No external contacts are Directory users.

| Fixture | Identifier |
| --- | --- |
| Customer | `C03e85e3b` or `my_customer` |
| Maya Chen (Advisory partner) | `p018`, `maya.chen@halcyonadvisory.example`, `122854712747506845221` |
| Directory administrators | `p035` / `arjun.mehta@halcyonadvisory.example`; `p036` / `sarah.johansson@halcyonadvisory.example` |
| Maya's My Drive root | `1FUf23CoYE6h7wkoNZ5i4qIqPotA7zckx`; `root` also resolves for the caller |
| All Staff | `allstaff@halcyonadvisory.example`, `111282468764909760231` |
| Client Engagements shared drive | `0A9WhImY3_g0m6efxE2` |
| Finance & Operations shared drive | `0Astgd3tXlRTiB1PJ73` |
| People & Culture shared drive | `0AbsdMKGMocVCYoVrDi` |
| Technology shared drive | `0AduJC_FDJtQpeixkJW` |
| Marketing & BD shared drive | `0AuYXVwSd-O5cKUkeK6` |
| Knowledge Base shared drive | `0AUN4SSG6WVcXP54Kqv` |
| Partner Group shared drive | `0AOc6KpHDZWMoUOXhBw` |
| Northwind engagement letter | `1K2U16IQTDUo5cGm92bnngTgiOVmoRgyu` |

Drive projects 359 world documents plus folders, personal roots and Meet artifacts
(741 total items with this seed/anchor). Office documents in personal, knowledge,
people, leadership and growth spaces become native Docs/Sheets/Slides. Other document
formats remain binary files. Binary content and exports render lazily and cache in the
content-addressed blob store. Checksums and string byte sizes match downloaded bytes.

Shared-drive roles come from space members and live Directory group membership.
Restricted world documents break inherited access. Personal items inherit parent
sharing; explicit shares support users, groups, domain and anyone links. Anyone-with-link
items can be fetched by a known ID but do not appear in an unrelated user's listing.
Meet recordings and transcript Docs share the exact IDs used by the Meet API and grant
attendees reader access. Recordings use the world's MP4 generator.

## Webhooks and actions

`files.watch` and `changes.watch` accept `type=web_hook`, `id`, `address`, optional
`token` and millisecond `expiration`. Creation delivers `sync`; file changes deliver
`add`, `update`, `remove`, `trash` or `untrash`, while change-feed channels deliver
`change`. Bodies are empty. Headers include `X-Goog-Channel-ID`, optional channel token,
channel expiration, resource ID/URI/state and increasing message number. File mutations
also include `X-Goog-Changed` (`content`, `properties`, `parents` or `permissions`).
Google Drive notifications have no cryptographic signature; consumers check the channel
ID, resource ID and configured channel token. Delivery uses `ctx.webhooks.deliver`, so
sync/manual/async modes and the control-plane webhook journal work normally.

`POST /_emulator/actions/mint_token` has the same payload and behavior as
`POST /_emulator/tokens`. There are no other part-A simulation actions. Resource
mutations produce change-journal entries and notifications directly. Reset restores
the seeded collections, grants, cursors, upload sessions and channels.

## Known gaps

- OAuth omits implicit/device flows, real consent policy, app verification, service-account
  key registration and domain-wide delegation approval. Refresh grants have no expiry.
  OIDC certificates use a local self-signed certificate and the shared emulator signing key.
- Directory implements the documented fixture query subset, not custom schemas, arbitrary
  query fields, deleted-user restoration, group alias management or device administration.
  User insertion requires a password but does not store or authenticate it. Scope families
  and admin roles are checked; fine-grained delegated-admin custom roles are not modelled.
- Drive `drive.file` uses file ACLs without modelling Google's per-app file picker grant.
  App properties, file stars and view history are not isolated per client/user. Domain and
  anyone discovery, app-data storage, labels, comments and replies have no populated fixtures.
- Revisions for seeded documents expose the head only; runtime binary revisions retain
  content. Native Docs revisions retain metadata, not historical native exports. Export
  formatting is synthetic and supports the formats in `about.exportFormats`.
- Cross-drive folder moves, domain ownership-transfer restrictions, pending-owner workflows,
  ownership notification email delivery, and the full Workspace restriction-policy matrix
  are not simulated. Descendants inherit a folder's trash state; the change journal records
  the directly mutated folder rather than separate synthetic entries for all descendants.
- Change cursors do not expire. The journal retains each mutation; it does not compact
  repeated file changes or implement all of Google's corpus-transition behavior. Watch
  channels do not renew automatically. Quota accounting counts materialized binary metadata.
- Uploads are held in memory; sessions do not expire and chunk sizes need not be multiples
  of 256 KiB. Native imports of uploaded Office binaries do not parse Office layout/content.
  Downloads support one byte range per request.
- The remaining Docs API (styles, tables, tabs, named ranges, suggestions and other batch
  request types) is outside this minimal implementation.

## Validation and references

Provider tests cover HTTP auth, OIDC, Directory hierarchy, group membership, Drive ACLs,
queries/fields, downloads, exports, uploads, file mutations, revisions, push channels,
incremental sync and Docs editing. `tests/sdk/test_google_drive_sdk.py` uses the official
google-api-python-client, google-auth refresh and service-account credentials, and
google-auth ID-token verification through `emulator_intercept`.

```powershell
uv run pytest tests/providers/google -q
$env:EMULATORS_PROVIDERS = "google"
uv run pytest tests/sdk/test_google_drive_sdk.py -q
```

Implementation references: [Google OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server),
[OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect),
[Directory user search](https://developers.google.com/workspace/admin/directory/v1/guides/search-users),
[Drive files](https://developers.google.com/workspace/drive/api/reference/rest/v3/files),
[Drive partial responses](https://developers.google.com/workspace/drive/api/guides/fields-parameter),
[Drive uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads).
