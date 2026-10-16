"""Seed Salesforce Files: every client document becomes a ContentDocument + ContentVersion linked to the client
Account (and the engagement it belongs to), with real file bytes rendered by the world's document generators."""

from __future__ import annotations

import hashlib

from emulators.providers.salesforce.common import fmt_dt
from emulators.providers.salesforce.hooks import FILE_TYPES, file_extension
from emulators.providers.salesforce.seedlib import SeedState, stamp
from emulators.world.documents import render
from emulators.world.model import World


def seed(state: SeedState, world: World) -> None:
    org = state.org
    for d in world.documents:
        if not d.client_id or d.trashed or d.client_id not in state.account_by_client:
            continue
        owner = state.user_by_person.get(d.owner_id, state.admin_id)
        author = world.person(d.owner_id).display_name if d.owner_id in state.user_by_person else ""
        table = [list(row) for row in d.table] if d.table else None
        data, _ = render(d.kind, d.title, list(d.paragraphs), author=author, created=d.created, table=table)
        key = org.store.blobs.put(data)
        ext = file_extension(d.name)
        file_type = FILE_TYPES.get(ext or "", "UNKNOWN")
        title = d.name.rsplit(".", 1)[0]
        doc = state.new(
            "ContentDocument",
            OwnerId=owner,
            Title=title,
            PublishStatus="R",
            Description=d.description or None,
            ContentSize=len(data),
            FileType=file_type,
            FileExtension=ext,
            ContentModifiedDate=None,
            IsArchived=False,
            SharingOption="A",
            SharingPrivacy="N",
        )
        version = state.new(
            "ContentVersion",
            ContentDocumentId=doc["Id"],
            IsLatest=True,
            VersionNumber="1",
            Title=title,
            Description=d.description or None,
            SharingOption="A",
            SharingPrivacy="N",
            PathOnClient=d.name,
            RatingCount=0,
            PositiveRatingCount=0,
            NegativeRatingCount=0,
            OwnerId=owner,
            VersionData="blob:" + key,
            ContentSize=len(data),
            ContentModifiedById=owner,
            FirstPublishLocationId=state.account_by_client[d.client_id],
            Origin="C",
            ContentLocation="S",
            FileType=file_type,
            FileExtension=ext,
            IsMajorVersion=True,
            Checksum=hashlib.md5(data, usedforsecurity=False).hexdigest(),
            **{"_size_VersionData": len(data)},
        )
        for rec in (doc, version):
            rec["ContentModifiedDate"] = fmt_dt(d.modified)
            stamp(rec, d.created, modified=d.modified, created_by=owner)
        doc["LatestPublishedVersionId"] = version["Id"]
        state.save("ContentDocument", doc)
        state.save("ContentVersion", version)
        targets = [(owner, "I"), (state.account_by_client[d.client_id], "I")]
        engagement = state.engagement_by_world.get(d.engagement_id or "")
        if engagement:
            targets.append((engagement, "I"))
        for entity, share in targets:
            link = state.new(
                "ContentDocumentLink",
                LinkedEntityId=entity,
                ContentDocumentId=doc["Id"],
                ShareType=share,
                Visibility="AllUsers",
            )
            link["SystemModstamp"] = doc["CreatedDate"]
            state.save("ContentDocumentLink", link)
