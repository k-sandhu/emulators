"""Seed conversations, messages, threads, reactions, files, pins, bookmarks and webhooks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from emulators.core.ids import stable_guid, stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.slack import access
from emulators.providers.slack.incoming import create_webhook
from emulators.providers.slack.messages import refresh_thread
from emulators.providers.slack.records import make_channel
from emulators.providers.slack.richtext import text_to_blocks
from emulators.providers.slack.seed_users import EXT_CLIENTS, GUEST_PERSONS, OWNER_PERSON
from emulators.providers.slack.shapes import FILETYPES
from emulators.providers.slack.util import escape_text, format_ts, slugify, stable_id, ts_key
from emulators.world import documents
from emulators.world.model import ChatMessage, Document, Team

TEAM_PREFIX = {
    "t-audit": "audit",
    "t-tax": "tax",
    "t-advisory": "advisory",
    "t-growth": "bd",
    "t-leadership": "partners",
}
CLIENT_SUFFIX = {"General": "", "Client Requests": "requests", "Fees & Staffing": "fees"}
REACTION_NAMES = {"like": "+1", "+1": "+1", "laugh": "joy", "heart": "heart"}
#: Channels the bot user is *not* a member of, so ``not_in_channel`` has a public example.
BOT_ABSENT = {"social", "bd-general"}
WEBHOOK_CHANNELS = (
    "general",
    "it-help",
    "audit-fieldwork",
    "tax-updates",
    "advisory-deals",
    "bd-pipeline",
    "eng-bluefin-seafood",
)
MAX_SEED_FILES = 16


def channel_name(team: Team, name: str, client_slug: str | None) -> str:
    if team.id == "t-firm":
        return slugify(name)
    if client_slug:
        suffix = CLIENT_SUFFIX.get(name, slugify(name))
        return f"eng-{client_slug}" + (f"-{suffix}" if suffix else "")
    prefix = TEAM_PREFIX.get(team.id, slugify(team.name))
    slug = slugify(name)
    if name == "General":
        return prefix if prefix == "partners" else f"{prefix}-general"
    return slug if slug.startswith(prefix) else f"{prefix}-{slug}"


def _micro(moment: datetime) -> int:
    return int(moment.timestamp()) * 1_000_000


def assign_timestamps(messages: list[ChatMessage]) -> dict[str, str]:
    """Deterministic, strictly increasing microsecond ``ts`` per message of one conversation."""
    result: dict[str, int] = {}
    last = 0
    ordered = sorted(enumerate(messages), key=lambda pair: (pair[1].created, pair[0]))
    for _, msg in ordered:
        jitter = int(stable_hash("ts", msg.id, length=6), 16) % 58_000_000
        candidate = _micro(msg.created) + jitter
        if msg.reply_to_id and msg.reply_to_id in result:
            candidate = max(candidate, result[msg.reply_to_id] + 1_000_000)
        if candidate <= last:
            candidate = last + 1_000 + jitter % 997
        result[msg.id] = last = candidate
    return {mid: format_ts(value // 1_000_000, value % 1_000_000) for mid, value in result.items()}


def convert_text(ctx: ProviderContext, msg: ChatMessage, mapping: dict[str, str]) -> str:
    text = escape_text(msg.text)
    for person_id in msg.mentions:
        uid = mapping.get(person_id)
        if uid:
            first = ctx.world.person(person_id).first_name
            text = text.replace(f"@{first}", f"<@{uid}>", 1)
    return text


def _reactions(msg: ChatMessage, mapping: dict[str, str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for emoji, person_id in msg.reactions:
        uid = mapping.get(person_id)
        name = REACTION_NAMES.get(emoji, emoji)
        if uid and uid not in grouped.setdefault(name, []):
            grouped[name].append(uid)
    return [{"name": name, "users": users, "count": len(users)} for name, users in grouped.items() if users]


def make_file(ctx: ProviderContext, doc: Document, uploader: str, created: int) -> dict[str, Any]:
    """A file record backed by a world document; bytes are rendered once and stored as a blob."""
    owner = ctx.world.person(doc.owner_id)
    table = [list(row) for row in doc.table] if doc.table else None
    data, mime = documents.render(
        doc.kind, doc.title, list(doc.paragraphs), author=owner.display_name, created=doc.created, table=table
    )
    kind, pretty = FILETYPES.get(doc.kind, (doc.kind, doc.kind.upper()))
    record = {
        "id": stable_id("F", doc.id),
        "created": created,
        "timestamp": created,
        "name": doc.name,
        "title": doc.name,
        "mimetype": mime,
        "filetype": kind,
        "pretty_type": pretty,
        "user": uploader,
        "user_team": access.team_id(ctx),
        "editable": False,
        "size": len(data),
        "mode": "hosted",
        "is_external": False,
        "external_type": "",
        "is_public": False,
        "public_url_shared": False,
        "display_as_bot": False,
        "username": "",
        "has_rich_preview": False,
        "file_access": "visible",
        "_blob": ctx.store.blobs.put(data),
        "_doc": doc.id,
        "_shares": [],
    }
    return access.col(ctx, access.FILES).put(record)


class Seeder:
    def __init__(self, ctx: ProviderContext, mapping: dict[str, str]) -> None:
        self.ctx = ctx
        self.w = ctx.world
        self.mapping = mapping
        self.names: dict[str, str] = {}
        self.docs_by_client: dict[str, list[Document]] = {}
        self.files_made = 0
        for doc in self.w.documents:
            if doc.client_id:
                self.docs_by_client.setdefault(doc.client_id, []).append(doc)
        self.bot = access.app_config(ctx)

    # -- helpers ------------------------------------------------------------------------------
    def uids(self, person_ids: Any, *, active_only: bool = True) -> list[str]:
        out = []
        for pid in person_ids:
            person = self.w.person(pid)
            if (person.active or not active_only) and pid in self.mapping:
                out.append(self.mapping[pid])
        return out

    def attach(self, doc: Document, uploader: str, moment: datetime) -> str | None:
        if self.files_made >= MAX_SEED_FILES:
            return None
        self.files_made += 1
        return make_file(self.ctx, doc, uploader, int(moment.timestamp()))["id"]

    def store_message(
        self,
        channel: dict[str, Any],
        msg: ChatMessage,
        ts: str,
        parent_ts: str | None,
        files: list[str],
    ) -> dict[str, Any]:
        uid = self.mapping[msg.author_id]
        text = convert_text(self.ctx, msg, self.mapping)
        rec: dict[str, Any] = {
            "id": access.message_key(channel["id"], ts),
            "_channel": channel["id"],
            "type": "message",
            "user": uid,
            "text": text,
            "ts": ts,
            "team": access.team_id(self.ctx),
            "client_msg_id": stable_guid("slack-msg", msg.id),
        }
        blocks = text_to_blocks(text, ts)
        if blocks:
            rec["blocks"] = blocks
        if parent_ts:
            rec["thread_ts"] = parent_ts
            parent = access.get_message(self.ctx, channel["id"], parent_ts)
            if parent and parent.get("user"):
                rec["parent_user_id"] = parent["user"]
        reactions = _reactions(msg, self.mapping)
        if reactions:
            rec["reactions"] = reactions
        if msg.edited:
            rec["edited"] = {"user": uid, "ts": format_ts(int(msg.edited.timestamp()), 0)}
        if channel.get("is_ext_shared"):
            owner = access.user_by_id(self.ctx, uid) or {}
            rec["user_team"] = rec["source_team"] = owner.get("team_id", access.team_id(self.ctx))
        if files:
            rec["_files"] = files
            for fid in files:
                access.col(self.ctx, access.FILES).get(fid)["_shares"].append(
                    {"channel": channel["id"], "ts": ts}
                )
        access.col(self.ctx, access.MESSAGES).put(rec)
        if ts_key(ts) > ts_key(channel["_last_ts"]):
            channel["_last_ts"] = ts
        return rec

    def load_messages(
        self,
        channel: dict[str, Any],
        messages: tuple[ChatMessage, ...] | list[ChatMessage],
        attachments: dict[str, list[str]] | None = None,
    ) -> None:
        stamps = assign_timestamps(list(messages))
        members: list[str] = channel["_members"]
        for msg in messages:
            parent_ts = stamps.get(msg.reply_to_id) if msg.reply_to_id else None
            files = (attachments or {}).get(msg.id, [])
            for doc_id in msg.attachment_doc_ids:
                fid = self.attach(self.w.document(doc_id), self.mapping[msg.author_id], msg.created)
                if fid:
                    files.append(fid)
            self.store_message(channel, msg, stamps[msg.id], parent_ts, files)
            who = {msg.author_id, *msg.mentions, *(pid for _, pid in msg.reactions)}
            for person_id in who:
                uid = self.mapping.get(person_id)
                if uid and uid not in members and not self._outsider(person_id):
                    members.append(uid)
        self._thread_parents(channel)

    def _thread_parents(self, channel: dict[str, Any]) -> None:
        cid = channel["id"]
        parents = {
            m["thread_ts"]
            for m in access.col(self.ctx, access.MESSAGES)
            if m["_channel"] == cid and m.get("thread_ts") and m["thread_ts"] != m["ts"]
        }
        for parent_ts in parents:
            parent = access.get_message(self.ctx, cid, parent_ts)
            if parent is not None:
                refresh_thread(self.ctx, cid, parent)

    def _outsider(self, person_id: str) -> bool:
        return self.w.person(person_id).is_external

    # -- team channels ------------------------------------------------------------------------
    def seed_team_channels(self) -> None:
        w = self.w
        owner_uid = self.mapping[OWNER_PERSON]
        index = 0
        for team in w.teams:
            client_slug = slugify(w.client(team.client_id).name) if team.client_id else None
            team_members = self.uids(team.member_ids)
            if team.id == "t-firm":
                team_members = self.uids(p.id for p in w.active_employees)
            creator = self.mapping[team.owner_ids[0]] if team.owner_ids else owner_uid
            for ch in team.channels:
                name = channel_name(team, ch.name, client_slug)
                private = ch.is_private or team.id == "t-leadership"
                members = self.uids(ch.member_ids) if ch.member_ids else list(team_members)
                first = min((m.created for m in ch.messages), default=w.anchor)
                created = int((first - timedelta(days=60 + 9 * index)).timestamp())
                is_general = team.id == "t-firm" and ch.name == "General"
                topic = ""
                if is_general:
                    topic = "Halcyon Advisory Group: firm-wide conversation"
                record = make_channel(
                    self.ctx,
                    channel_id=stable_id("G" if private else "C", ch.id),
                    name=name,
                    creator=creator,
                    created=created,
                    members=members,
                    is_private=private,
                    is_general=is_general,
                    topic=topic or ch.description,
                    purpose=ch.description or f"Conversation for {team.name}",
                )
                if not private and name not in BOT_ABSENT:
                    record["_members"].append(self.bot["bot_user_id"])
                if name == "eng-bluefin-seafood-fees":
                    record["_members"].append(self.bot["bot_user_id"])
                if name.startswith("eng-bluefin-seafood") and not private:
                    for pid in GUEST_PERSONS:
                        if pid == "c002-1" or name.endswith("requests"):
                            record["_members"].append(self.mapping[pid])
                access.col(self.ctx, access.CHANNELS).put(record)
                self.names[name] = record["id"]
                attachments = self._channel_attachments(name, ch.messages, team)
                self.load_messages(record, ch.messages, attachments)
                index += 1

    def _channel_attachments(
        self, name: str, messages: tuple[ChatMessage, ...], team: Team
    ) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        if team.client_id:
            docs = self.docs_by_client.get(team.client_id, [])
            cursor = 0
            for msg in messages:
                if "Uploaded" in msg.text and docs and not msg.reply_to_id:
                    doc = docs[cursor % len(docs)]
                    cursor += 1
                    fid = self.attach(doc, self.mapping[msg.author_id], msg.created)
                    if fid:
                        found[msg.id] = [fid]
        elif name == "announcements":
            docs = [d for d in self.w.documents if d.space_id == "people"][:3]
            top = [m for m in messages if not m.reply_to_id]
            for position, doc in zip((1, 4, 7), docs, strict=False):
                if position < len(top):
                    fid = self.attach(doc, self.mapping[top[position].author_id], top[position].created)
                    if fid:
                        found[top[position].id] = [fid]
        return found

    # -- Slack Connect ------------------------------------------------------------------------
    def seed_ext_channels(self) -> None:
        w = self.w
        for client_id in EXT_CLIENTS:
            client = w.client(client_id)
            engagement = next(
                (e for e in w.engagements if e.client_id == client_id and e.status == "In Progress"), None
            )
            if engagement is None:
                continue
            name = f"ext-{slugify(client.name)}"
            lead = self.mapping[engagement.lead_id]
            internal = self.uids([engagement.lead_id, *engagement.team_ids])
            contacts = [self.mapping[c] for c in client.contact_ids if c in self.mapping]
            base = datetime(w.anchor.year, w.anchor.month, w.anchor.day, 15, 0, tzinfo=UTC) - timedelta(
                days=14
            )
            record = make_channel(
                self.ctx,
                channel_id=stable_id("C", "ext", client_id),
                name=name,
                creator=lead,
                created=int((base - timedelta(days=3)).timestamp()),
                members=[*internal, *contacts, self.bot["bot_user_id"]],
                purpose=f"Shared channel with {client.name}",
                topic=engagement.name.split(" — ")[-1],
                ext_team_ids=[stable_id("T", client_id)],
            )
            access.col(self.ctx, access.CHANNELS).put(record)
            self.names[name] = record["id"]
            lead_person = w.person(engagement.lead_id)
            first_contact = w.person(client.contact_ids[0])
            second_contact = w.person(client.contact_ids[min(1, len(client.contact_ids) - 1)])
            script = [
                (
                    lead_person,
                    f"Welcome to the shared channel, {first_contact.first_name}! "
                    f"We'll coordinate the {engagement.service.lower()} work here.",
                    None,
                ),
                (
                    first_contact,
                    f"Thanks {lead_person.first_name}, glad to be connected. "
                    "We'll post documents as they're ready.",
                    None,
                ),
                (
                    lead_person,
                    "Sharing the request list this week. Let us know if anything needs more time.",
                    None,
                ),
                (second_contact, "Received, we'll review it with the team and confirm timing by Friday.", 2),
                (lead_person, "Perfect, thank you both.", None),
            ]
            messages: list[ChatMessage] = []
            for position, (author, text, reply_to) in enumerate(script):
                messages.append(
                    ChatMessage(
                        id=f"ext-{client_id}-{position + 1:03d}",
                        author_id=author.id,
                        created=base + timedelta(days=position, hours=position),
                        text=text,
                        reply_to_id=messages[reply_to].id if reply_to is not None else None,
                    )
                )
            self.load_messages(record, messages)

    # -- DMs and group DMs ---------------------------------------------------------------------
    def seed_chats(self) -> None:
        w = self.w
        for chat in w.chats:
            uids = self.uids(chat.member_ids, active_only=False)
            first = min((m.created for m in chat.messages), default=w.anchor)
            created = int((first - timedelta(days=1)).timestamp())
            if len(uids) == 2:
                record = make_channel(
                    self.ctx,
                    channel_id=stable_id("D", chat.id),
                    creator=uids[0],
                    created=created,
                    members=uids,
                    is_im=True,
                )
            else:
                handles = [(access.user_by_id(self.ctx, u) or {})["name"] for u in uids]
                record = make_channel(
                    self.ctx,
                    channel_id=stable_id("G", chat.id),
                    name="mpdm-" + "--".join(handles) + "-1",
                    creator=uids[0],
                    created=created,
                    members=uids,
                    is_mpim=True,
                    purpose="Group messaging with: " + ", ".join(f"@{h}" for h in handles),
                )
            access.col(self.ctx, access.CHANNELS).put(record)
            self.load_messages(record, chat.messages)
        self._welcome_dm()

    def _welcome_dm(self) -> None:
        owner = self.mapping[OWNER_PERSON]
        created = int((self.w.anchor - timedelta(days=30)).timestamp())
        record = make_channel(
            self.ctx,
            channel_id=stable_id("D", "bot-welcome"),
            creator=self.bot["bot_user_id"],
            created=created,
            members=[self.bot["bot_user_id"], owner],
            is_im=True,
        )
        access.col(self.ctx, access.CHANNELS).put(record)
        ts = format_ts(created + 60, 120)
        text = "Hi Eleanor, I'm Halcyon Bot. I post engagement and billing notifications for the firm."
        record["_last_ts"] = ts
        access.col(self.ctx, access.MESSAGES).put(
            {
                "id": access.message_key(record["id"], ts),
                "_channel": record["id"],
                "type": "message",
                "user": self.bot["bot_user_id"],
                "text": text,
                "ts": ts,
                "team": access.team_id(self.ctx),
                "bot_id": self.bot["bot_id"],
                "app_id": self.bot["app_id"],
                "blocks": text_to_blocks(text, ts),
            }
        )

    # -- pins, bookmarks, webhooks ------------------------------------------------------------
    def seed_pins_bookmarks_webhooks(self) -> None:
        owner = self.mapping[OWNER_PERSON]
        admin = next((self.mapping[p.id] for p in self.w.employees if p.is_admin), owner)
        for name, who in (("general", owner), ("announcements", owner), ("it-help", admin)):
            cid = self.names.get(name)
            if not cid:
                continue
            top = [
                m
                for m in access.channel_messages(self.ctx, cid)
                if not m.get("thread_ts") or m["thread_ts"] == m["ts"]
            ]
            if top:
                message = top[0]
                access.col(self.ctx, access.PINS).put(
                    {
                        "id": access.message_key(cid, message["ts"]),
                        "channel": cid,
                        "ts": message["ts"],
                        "created": int(message["ts"].split(".")[0]) + 3600,
                        "created_by": who,
                        "type": "message",
                    }
                )
        links = (
            ("general", "Firm intranet", f"{self.w.org.website}/intranet", ":house:"),
            ("it-help", "Service desk", f"{self.w.org.website}/servicedesk", ":ticket:"),
            ("audit-fieldwork", "Audit methodology", f"{self.w.org.website}/methodology/audit", ":books:"),
            ("tax-updates", "CRA and IRS bulletins", f"{self.w.org.website}/tax/bulletins", ":newspaper:"),
        )
        created = int((self.w.anchor - timedelta(days=45)).timestamp())
        for rank, (name, title, link, emoji) in enumerate(links):
            cid = self.names.get(name)
            if not cid:
                continue
            bid = stable_id("Bk", cid, title, length=9)
            access.col(self.ctx, access.BOOKMARKS).put(
                {
                    "id": bid,
                    "channel_id": cid,
                    "title": title,
                    "link": link,
                    "emoji": emoji,
                    "icon_url": "",
                    "type": "link",
                    "entity_id": "",
                    "date_created": created,
                    "date_updated": created,
                    "rank": f"{rank:02d}",
                    "last_updated_by_user_id": admin,
                    "last_updated_by_team_id": access.team_id(self.ctx),
                    "shortcut_id": "",
                    "app_id": "",
                }
            )
        for name in WEBHOOK_CHANNELS:
            cid = self.names.get(name)
            if cid:
                create_webhook(
                    self.ctx, cid, created_by=admin, created=created, label=f"Halcyon alerts #{name}"
                )


def seed_conversations(ctx: ProviderContext, mapping: dict[str, str]) -> dict[str, str]:
    seeder = Seeder(ctx, mapping)
    seeder.seed_team_channels()
    seeder.seed_ext_channels()
    seeder.seed_chats()
    seeder.seed_pins_bookmarks_webhooks()
    return seeder.names
