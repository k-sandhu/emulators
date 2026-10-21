# Provider reference

One page per emulator: hostnames, auth flows and test credentials, endpoint tables, seeded data
with concrete ids, webhooks and their signature scheme, `/_emulator/actions/*` simulations, and
known gaps. Live credentials and seeded ids for the current seed are always available at
`GET https://<vendor host>/_emulator/info` (or `uv run emulators info <provider>`).

| Category | Provider (`name`, port) | Reference |
|---|---|---|
| Identity, directory, mail, calendar | Microsoft identity platform + Graph (`microsoft`, 44101) | [identity, directory, Outlook](microsoft.md) · [OneDrive, SharePoint, Teams, meetings](microsoft-graph-files-teams.md) |
| Identity, directory, storage, mail, calendar, meetings | Google (`google`, 44102) | [OAuth, Directory, Drive, Docs](google.md) · [Gmail, Calendar, Meet](google-gmail-calendar-meet.md) |
| Storage | Box (`box`, 44103) | [box.md](box.md) |
| Storage | Dropbox (`dropbox`, 44104) | [dropbox.md](dropbox.md) |
| CRM | Salesforce (`salesforce`, 44105) | [salesforce.md](salesforce.md) |
| Meetings | Zoom (`zoom`, 44106) | [zoom.md](zoom.md) |
| Messaging | Slack (`slack`, 44107) | [slack.md](slack.md) |
| Messaging (SMS/MMS, Verify, Lookup, Voice) | Twilio (`twilio`, 44108) | [twilio.md](twilio.md) |
| Messaging | WhatsApp Cloud API + Messenger (`meta`, 44109) | [meta.md](meta.md) |
| Payments | Stripe (`stripe`, 44110) | [stripe.md](stripe.md) |
| Accounting | QuickBooks Online (`quickbooks`, 44111) | [quickbooks.md](quickbooks.md) |
| Accounting | Xero (`xero`, 44112) | [xero.md](xero.md) |
| CRM | HubSpot (`hubspot`, 44113) | [hubspot.md](hubspot.md) |
| Meetings | Webex (`webex`, 44114) | [webex.md](webex.md) |
| Knowledge | Notion (`notion`, 44115) | [notion.md](notion.md) |
| Knowledge, work tracking | Confluence + Jira (`atlassian`, 44116) | [atlassian.md](atlassian.md) |
| Work tracking | Asana (`asana`, 44117) | [asana.md](asana.md) |
| Work tracking | Trello (`trello`, 44118) | [trello.md](trello.md) |
| Structured data | Airtable (`airtable`, 44119) | [airtable.md](airtable.md) |

All providers are seeded from the same synthetic organisation (see
[architecture](../architecture.md#the-world-srcemulatorsworld)), share one clock, and expose the
same control plane (`/_emulator/{health,info,reset,state,requests,faults,tokens,webhooks,actions,docs}`).
