# Microsoft Graph: files, SharePoint, Teams and meeting artifacts

This page covers Microsoft part B. Identity, directory, Outlook and subscription
management are described in `microsoft.md`. Every Graph route below accepts both
`/v1.0` and `/beta`.

## Hosts and routing

* `graph.microsoft.com`: Graph JSON and authenticated content endpoints.
* `halcyonadvisory.sharepoint.com`: team libraries, upload sessions, copy monitors and
  preauthenticated downloads.
* `halcyonadvisory-my.sharepoint.com`: personal OneDrive downloads.
* `login.microsoftonline.com`: OAuth/OIDC, implemented by the identity module.

Consumers can keep vendor URLs with `hub.async_transport()` or the SDK interception
fixture. Prefix routing also works at `http://localhost:44080/microsoft/v1.0/...`;
download URLs, upload URLs, paging links and delta links stay on that prefix. An
`EmulatorServer` or the dedicated Microsoft port supports ordinary HTTP clients.

## Authentication and test credentials

Use any client ID and secret in the default lenient mode. The examples and SDK tests
use client ID `00000000-0000-0000-0000-00000000c0de` and secret `synthetic-secret`.
MSAL can acquire delegated tokens using a world employee's email and a synthetic
password; the identity module also supports client credentials and authorization code
flows. No real Microsoft account is contacted.

Alternatively, mint a delegated token through the control plane:

```http
POST https://graph.microsoft.com/_emulator/tokens
Content-Type: application/json

{"user":"p018","scopes":["Files.ReadWrite","OnlineMeetings.Read","Chat.ReadWrite"]}
```

Mint an application token with `{"app_only":true}`. Send the returned token as
`Authorization: Bearer ...`. Missing, invalid and expired tokens return Graph-shaped
401 responses. `/me` requires delegated authentication. Application tokens can access
all seeded drives, sites, teams, chats and meetings. Bulk message/artifact exports,
call records and site-permission listing require application authentication.

Delegated visibility follows world membership and document ACLs. Personal drives are
private unless individual files are shared. Team space membership expands directory
groups; restricted documents break inheritance. Direct user/group shares and unexpired
organization/anonymous link grants augment access. Inaccessible files, sites, teams,
private channels, chats and meetings return `404 itemNotFound`; read-only file writes
and unauthorized message edits return `403 accessDenied`. Only the organizer can
modify a meeting, and only the author can edit a message. Sending ordinary messages
requires delegated authentication.

All JSON mutations use the shared Graph object-body parser and return 400 for malformed
JSON, arrays or null. Collection and delta paging honor `Prefer: odata.maxpagesize=N`
and return `Preference-Applied`. `$select` supports nested paths and `*` on single
entities; directory-derived conversation members retain their `@odata.type` annotation.
Teams bodies normalize `HTML`/`Text` to `html`/`text`, including edits. Channel delta
applies selection without adding entity-level context fields. Paging links preserve
literal OData parameter names, and prefix-mode links keep one routing prefix.

## Endpoint table

In the drive rows, `{D}` means any of `/me/drive`, `/users/{userId}/drive`,
`/drives/{driveId}`, `/groups/{groupId}/drive`, or `/sites/{siteId}/drive`. `{I}` means
`{D}/items/{itemId}` or a root/item-relative path address. `{M}` means
`/me/onlineMeetings/{meetingId}` or `/users/{userId}/onlineMeetings/{meetingId}`.
`{C}` means `/teams/{teamId}/channels/{channelId}`. Paths omit the version prefix.

| Method | Path | Notes |
|---|---|---|
| GET | `/me/drive`, `/me/drives`, `/drives`, `/drives/{id}` | Drive metadata, visibility-filtered drive discovery. |
| GET | `/users/{id}/drive`, `/groups/{id}/drive`, `/sites/{id}/drive`, `/sites/{id}/drives` | Stable world-backed drive aliases. |
| GET | `{D}/root`, `{I}`, `{D}/root/children`, `{I}/children` | Real folder tree; default page 200; `$top`, `$orderby`, `$select`, `$expand=thumbnails`. |
| GET | `{D}/root:/Clients/Northwind Logistics:`, `{D}/root:/...:/children`, `{D}/items/{id}:/sub/path:` | Case-insensitive path segments; percent-encoded names work. |
| GET | `{I}/content` | 302 to an expiring, preauthenticated SharePoint URL. Download accepts byte Range and conditional ETag requests. |
| GET | `{I}/content?format=pdf` | PDF rendition of seeded or uploaded DOCX/XLSX/PPTX document text; PDF passthrough. |
| PUT | `{D}/root:/{name}:/content`, `{D}/items/{parent}:/{name}:/content`, `{I}/content` | Simple create/replace; `If-Match`; `@microsoft.graph.conflictBehavior=fail/replace/rename`. |
| POST | `{D}/root:/{name}:/createUploadSession`, `{D}/items/{parent}:/{name}:/createUploadSession`, `{I}/createUploadSession` | Expiring SharePoint upload URL; sequential Content-Range fragments; 202 progress and final 200/201. |
| GET, PUT, DELETE | returned upload URL | Inspect progress, upload fragments, cancel. No bearer token needed. Non-final fragments are multiples of 320 KiB; fragments must be under 60 MiB. |
| POST | `{I}/children` | Create folder; fail/replace/rename conflicts. |
| PATCH, DELETE | `{I}` | Rename/move, description updates; deletion keeps a recycle-bin tombstone for delta. |
| POST | `{I}/copy` | 202 with SharePoint monitor URL; monitor returns completed/resourceId/resourceLocation. Copies can cross drives. |
| GET | `{I}/versions`, `{I}/versions/{version}/content` | Seeded version metadata; retained bytes for versions replaced through uploads. |
| GET | `{I}/thumbnails`, `{I}/thumbnails/0/{size}/content` | Thumbnail set and valid synthetic PNG. |
| GET | `{D}/root/search(q='budget')`, `{D}/items/{id}/search(q='budget')` | ACL-filtered name, description and seeded document-text search. |
| GET | `/me/drive/recent`, `/me/drive/sharedWithMe` | Modified-date ordering and direct shares with `remoteItem`. |
| GET | `{D}/special/documents`, `{D}/special/approot` | Documents root; application folder under Apps. |
| GET | `{D}/root/delta` | Full enumeration, paged next links, opaque delta links; `token=latest`; 410 resyncRequired for invalid/reset/cross-user tokens. |
| GET | `{I}/permissions`, `{I}/permissions/{id}` | Direct and inherited grants, V2 identities and sharing link metadata. |
| POST | `{I}/invite`, `{I}/createLink` | World user invitations; anonymous/organization/users links, expiry/password-presence metadata. |
| PATCH, DELETE | `{I}/permissions/{id}` | Update/revoke direct permissions; inherited grants must be changed at their source. |
| GET | `/sites/root`, `/sites/{hostname}:/sites/{slug}`, `/sites/{id}`, `/sites?search=...` | Root intranet plus seven world team sites. |
| GET | `/sites/{id}/sites`, `/groups/{id}/sites/root` | Subsite list (empty for site collections); group site resolution. |
| GET | `/sites/{id}/lists`, `/sites/{id}/lists/{id}`, `/sites/{id}/lists/{id}/columns` | Documents libraries and Engagement Tracker schema. |
| GET | `/sites/{id}/lists/{id}/items`, `.../items/{id}`, `.../items/{id}/fields`, `.../items/{id}/driveItem` | `$expand=fields($select=...)` and `expand=fields(select=...)`; Documents mirrors the live drive tree. |
| POST | `/sites/{id}/lists/{id}/items` | Create Engagement Tracker item with a fields object. |
| PATCH, DELETE | `/sites/{id}/lists/{id}/items/{id}` | Update fields/delete; field PATCH also accepts `.../{id}/fields`; schema and numeric validation. |
| GET | `/sites/{id}/permissions` | Application-only site membership grants. |
| GET | `/me/joinedTeams`, `/users/{id}/joinedTeams`, `/teams/{id}`, `/teams/{id}/members` | Team membership and aadUserConversationMember records. |
| GET | `/teams/{id}/channels`, `{C}`, `/teams/{id}/primaryChannel`, `{C}/members` | Standard/private channels; private channels require their own membership. |
| GET, POST | `{C}/messages`, `{C}/messages/{id}/replies` | Top-level threads and replies; default page 20, maximum 50; `$expand=replies`; caller identity on posts. |
| GET, PATCH | `{C}/messages/{id}`, `{C}/messages/{id}/replies/{replyId}` | Retrieve/edit with message timestamps and edit history. |
| POST | `{C}/messages/{id}/softDelete`, `.../undoSoftDelete` | Soft deletion/restoration, also for replies. |
| GET | `{C}/messages/delta` | Full/paged message enumeration and subsequent changes, opaque `$deltatoken` links. |
| GET | `/teams/{id}/channels/getAllMessages` | Application-only export, including private channels and replies. |
| GET | `/me/chats`, `/users/{id}/chats`, `/chats/{id}`, `/chats/{id}/members` | Membership-filtered chats; `$expand=members,lastMessagePreview`. |
| GET, POST | `/chats/{id}/messages` | Caller identity, paging; individual GET/PATCH and softDelete/undoSoftDelete supported. |
| GET | `/users/{id}/chats/getAllMessages` | Application-only export across a user's chats. |
| GET, POST | `/me/onlineMeetings`, `/users/{id}/onlineMeetings` | GET requires exact `$filter=JoinWebUrl eq '...'`; a bare list is 400. POST creates a standalone meeting. |
| GET, PATCH, DELETE | `{M}` | Calendar-compatible stable meeting ID and join URL; organizer-only delegated changes. |
| POST | `/me/onlineMeetings/createOrGet`, `/users/{id}/onlineMeetings/createOrGet` | Organizer-scoped externalId idempotency, 201 then 200. |
| GET | `{M}/transcripts`, `{M}/transcripts/{id}`, `.../{id}/content`, `.../{id}/metadataContent` | WebVTT, UUID cue IDs and voice tags; metadata is timestamped JSON inside WebVTT. |
| GET | `{M}/transcripts/{id}/content?$format=text/vtt` | Parlance-compatible default/explicit format. DOCX compatibility extension also supported; unattributed format available through Accept. |
| GET | `{M}/recordings`, `{M}/recordings/{id}`, `.../{id}/content` | MP4, cached bytes, Range/206; same bytes appear in organizer's OneDrive Recordings folder. |
| GET | `{M}/attendanceReports`, `.../{reportId}`, `.../{reportId}/attendanceRecords` | Optional `$expand=attendanceRecords`; participant identity, email, roles and intervals. |
| GET | `/users/{id}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{id}',startDateTime=...,endDateTime=...)` | Application-only, date-bounded organizer transcript export. |
| GET | `/users/{id}/onlineMeetings/getAllRecordings(...)` | Same parameters/permissions for recordings. |
| GET | `/communications/callRecords/{id}`, `.../{id}/sessions` | Application-only groupCall records; `$expand=sessions($expand=segments)`. |

## Seeded data and concrete examples

With seed `emulators` and anchor `2026-10-16`, the projection contains 52 drives,
787 root/folder/file items (359 world documents plus meeting recordings), eight sites
including the root, eight lists, seven Teams, 20 channels, 26 chats, 740 chat/channel
messages, 81 noncancelled Teams meetings, 24 transcripts and 25 recordings. Past Teams
meetings also have attendance and call records. Initial combined Microsoft hub
construction takes approximately 1.1 seconds on the development machine, with no
document or recording binary rendering during seed.

* Maya Chen: `maya.chen@halcyonadvisory.example`, world user `p018`, directory ID
  `02084459-8781-4fc6-ad74-ba491d0c93bb`.
* Maya's drive:
  `b!Oyf8hQI-a1gfktVEfMckGQ9P5lGflJh_hMTEdrpHo6tbhhVizHwRaDc3xRLVmY48`.
* Client Engagements site:
  `halcyonadvisory.sharepoint.com,c1d0846c-d3b4-4cc4-b66b-66cdfe5d7c9f,1b50c0f9-b79e-49a4-a82b-8ff5f0d93135`.
  Resolve it by `/sites/halcyonadvisory.sharepoint.com:/sites/ClientEngagements`.
* First team, Halcyon Firmwide: `0f18a270-5fda-49a8-a603-a96be3465fa4`.
* World meeting `m0002`, organized by `eleanor.whitfield@halcyonadvisory.example`,
  has both transcript and recording. Obtain its exact join URL from its calendar
  event, or `common.join_web_url(world, world.meetings[1])`, then perform the Parlance
  lookup. IDs and URLs match the calendar module.
* Engagement Tracker has one item per world engagement with Title, Client, Service,
  Status, StartDate, EndDate, BudgetHours and Lead fields.

Document content is generated only when first requested as metadata/content, then
cached in the content-addressed blob store. Files contain valid Office/PDF/CSV/PNG
structures. Hashes include the actual 160-bit, shift-11 QuickXorHash and SHA-1/SHA-256
of the downloaded bytes. Recordings use the shared valid MP4 generator.

## Webhooks and actions

Use the identity/subscriptions module's `/subscriptions` endpoints. Validation echoes
`validationToken`; basic notifications include `subscriptionId`, `resourceData`,
`clientState`, `tenantId` and expiry. Microsoft Graph basic notifications have no HMAC
signature: subscribers validate their configured clientState. All network delivery
uses `ctx.webhooks`, including in-process test receivers.

Drive writes publish `drives/{driveId}/root` changes. Teams posts/edits publish their
channel/chat message resource. Artifact actions publish user resource changes, which
the shared subscriptions listener matches to communications subscriptions; meeting
completion also publishes its call record. All notifications use the common change bus.

| Action | Payload | Result |
|---|---|---|
| `POST /_emulator/actions/end_meeting` | `{"meeting_id":"m0002"}` or the Graph meeting ID | Idempotently ends a live/future meeting and creates transcript, recording, OneDrive file, attendance and call record. Already-ended meetings remain unchanged. |
| `POST /_emulator/actions/add_transcript` | `{"meeting_id":"...","text":"Follow-up discussion."}` | Creates another transcript, defaulting to the organizer as speaker. |
| `POST /_emulator/actions/add_transcript` | `{"meeting_id":"...","turns":[{"speaker_id":"p018","text":"Approved.","offset_s":0,"duration_s":2}]}` | Explicit timed turns; speakers must be world participants in that meeting. |

Reset restores seeded metadata and permissions and invalidates upload/download tickets
and delta cursors. Immutable cached blob bytes remain reusable.

## Known gaps

* Scope strings are accepted by shared authentication; these modules enforce actor,
  membership and ACL restrictions rather than a full Microsoft permission-consent and
  application-access-policy system. Root intranet has no default library; team site
  libraries come from world spaces. Group aliases choose a matching world space;
  this does not provision one distinct site for every directory group.
* No tenant/site provisioning, site permission mutation, retention administration,
  recycle-bin restore API, share-link redemption, email invitation delivery, custom
  list/column creation, complex SharePoint field types or server search indexing.
  Sharing recipients must exist in the world. Link password presence is represented;
  password-protected redemption is not implemented.
* Copy jobs complete immediately. Upload sessions support sequential fragments and
  cancellation; deferred commit and automatic session cleanup are not implemented.
  Seeded historical versions have metadata only; upload replacement versions retain
  actual prior bytes. PDF conversion extracts document text rather than reproducing
  Office page layout, embedded graphics, formulas or slide styling. Historically exact
  seeded version bytes are not available.
* Thumbnails are synthetic PNG previews. Recent uses modification time rather than
  personal view telemetry. sharedWithMe includes direct person/group shares and does
  not reproduce Microsoft's complete remote-item ranking and discovery rules.
* Teams team/channel/chat provisioning, membership mutation, hosted-content upload,
  reaction mutation, bots, migration mode and rich/encrypted notifications are not
  implemented. Message history models edits; the world does not supply historical
  edit bodies. Channel delta is a compatibility extension matching the brief.
* Online meeting invitation emails, PSTN/live-event features, server expiration and
  full Teams policy settings are not simulated. DOCX transcript output is an explicit
  compatibility extension: Microsoft deprecated it in 2023. The shared media generator
  emits valid silent AAC audio in an MP4 container without a video track. Attendance
  and session quality data are synthetic, not actual device/network telemetry.
* Delta tokens are store-backed and remain valid until reset, rather than following
  Microsoft's service retention windows. Paging cursors are not long-lived server
  snapshots except for delta enumeration.
* The installed Microsoft SDK is MSAL. SDK contracts validate actual MSAL OAuth tokens
  with ordinary requests-based Graph consumers; the optional Microsoft Graph SDK's
  generated model deserialization is not exercised because it is not installed.

## Verification and references

```powershell
uv run pytest tests/providers/microsoft -q
$env:EMULATORS_PROVIDERS = 'microsoft'
uv run pytest tests/sdk/test_microsoft_files_teams_sdk.py -q
```

Wire details were checked against Microsoft's references for
[upload sessions](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0),
[QuickXorHash](https://learn.microsoft.com/en-us/onedrive/developer/code-snippets/quickxorhash?view=odsp-graph-online),
[chatMessage](https://learn.microsoft.com/en-us/graph/api/resources/chatmessage?view=graph-rest-1.0),
[transcript metadata and content](https://learn.microsoft.com/en-us/graph/api/calltranscript-get?view=graph-rest-1.0),
[attendance records](https://learn.microsoft.com/en-us/graph/api/resources/attendancerecord?view=graph-rest-1.0),
and [call records](https://learn.microsoft.com/en-us/graph/api/resources/callrecords-callrecord?view=graph-rest-1.0).
