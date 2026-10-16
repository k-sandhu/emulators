"""Mutations on the entry tree: create, write revisions, delete, move/copy, restore.

Every mutation keeps the per-request :class:`~emulators.providers.dropbox.model.Tree` in step and
appends to the ``changes`` log that ``list_folder/continue``, long-polling and webhooks read.
Permission and conflict policy lives in the routes; this module is the mechanism.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.dropbox.common import b64url, content_hash, digest, ts
from emulators.providers.dropbox.model import (
    CHANGES,
    ENTRIES,
    NAMESPACES,
    TEXT,
    Tree,
    new_rev,
    next_seq,
    now,
    parent_of,
)

TEXT_EXTENSIONS = {"txt", "md", "csv", "json", "html", "htm", "xml", "log", "yaml", "yml", "tsv", "ini"}


class Ops:
    def __init__(
        self,
        ctx: ProviderContext,
        tree: Tree,
        touched: set[str] | None = None,
        *,
        log: bool = True,
    ) -> None:
        self.ctx = ctx
        self.tree = tree
        self.touched = touched if touched is not None else set()
        self.log = log

    # -- ids and logging -------------------------------------------------------------
    def entry_id(self, ns_id: str) -> str:
        ns = self.tree.ns.get(ns_id)
        assert ns is not None
        ns["n"] = ns.get("n", 0) + 1
        return "id:" + b64url(digest("dbx-nsfile", ns_id)[:8]) + b64url((ns["n"] + 0x5F).to_bytes(8, "big"))

    def _log(self, rec: dict[str, Any], kind: str) -> None:
        if not self.log:
            return
        seq = next_seq(self.ctx)
        self.ctx.store.collection(CHANGES).put(
            {
                "id": str(seq),
                "seq": seq,
                "kind": kind,
                "ns": rec["ns"],
                "path": rec["path"],
                "dpath": rec["dpath"],
                "name": rec["name"],
                "type": rec["type"],
                "entry": rec["id"],
            }
        )
        rec["seq"] = seq
        ns = self.tree.ns.get(rec["ns"])
        if ns is not None:
            ns["seq"] = seq
        self.touched.add(rec["ns"])

    # -- content ---------------------------------------------------------------------
    def revision(
        self,
        data: bytes,
        *,
        when: datetime,
        client_modified: datetime | None = None,
        modified_by: str | None = None,
        rev: str | None = None,
        server_modified: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "rev": rev or new_rev(self.ctx),
            "size": len(data),
            "content_hash": content_hash(data),
            "blob": self.ctx.store.blobs.put(data),
            "client_modified": ts(client_modified or when),
            "server_modified": ts(server_modified or when),
            "modified_by": modified_by,
        }

    def index_text(self, rec: dict[str, Any], data: bytes | None, extra: str = "") -> None:
        ext = rec["name"].rsplit(".", 1)[-1].lower() if "." in rec["name"] else ""
        text = extra
        if data is not None and ext in TEXT_EXTENSIONS:
            text += "\n" + data[:200_000].decode("utf-8", errors="ignore")
        if text.strip():
            self.ctx.store.collection(TEXT).put({"id": rec["id"], "text": text.lower()})
        else:
            self.ctx.store.collection(TEXT).delete(rec["id"])

    # -- creation --------------------------------------------------------------------
    def _base(
        self, ns_id: str, parent_rel: str, name: str, kind: str, *, when: datetime, owner: str | None
    ) -> dict[str, Any]:
        parent = self.tree.live.get((ns_id, parent_rel)) if parent_rel else None
        parent_d = parent["dpath"] if parent else ""
        rec = {
            "id": self.entry_id(ns_id),
            "ns": ns_id,
            "path": parent_rel + "/" + name.lower(),
            "dpath": parent_d + "/" + name,
            "name": name,
            "type": kind,
            "deleted": False,
            "owner": owner,
            "created": ts(when),
            "seq": 0,
        }
        return rec

    def _insert(self, rec: dict[str, Any]) -> dict[str, Any]:
        self.ctx.store.collection(ENTRIES).put(rec)
        self.tree.add(rec)
        self._log(rec, "upsert")
        return rec

    def create_folder(
        self,
        ns_id: str,
        parent_rel: str,
        name: str,
        *,
        when: datetime | None = None,
        owner: str | None = None,
        mount_ns: str | None = None,
    ) -> dict[str, Any]:
        rec = self._base(ns_id, parent_rel, name, "folder", when=when or now(self.ctx), owner=owner)
        if mount_ns:
            rec["mount_ns"] = mount_ns
        return self._insert(rec)

    def ensure_folders(
        self, ns_id: str, parent_rel: str, names: list[str], *, when: datetime | None = None, owner: str | None = None
    ) -> str:
        """Create any missing folders along ``names`` under ``parent_rel``; return the final rel path."""
        rel = parent_rel
        for name in names:
            nxt = rel + "/" + name.lower()
            if (ns_id, nxt) not in self.tree.live:
                self.create_folder(ns_id, rel, name, when=when, owner=owner)
            rel = nxt
        return rel

    def create_file(
        self,
        ns_id: str,
        parent_rel: str,
        name: str,
        data: bytes,
        *,
        when: datetime | None = None,
        client_modified: datetime | None = None,
        server_modified: datetime | None = None,
        modified_by: str | None = None,
        owner: str | None = None,
        revisions: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        text: str = "",
    ) -> dict[str, Any]:
        when = when or now(self.ctx)
        rec = self._base(ns_id, parent_rel, name, "file", when=when, owner=owner)
        current = self.revision(
            data,
            when=when,
            client_modified=client_modified,
            server_modified=server_modified,
            modified_by=modified_by or owner,
        )
        rec["revisions"] = [*(revisions or []), current]
        rec["members"] = []
        rec["downloadable"] = True
        if extra:
            rec.update(extra)
        self.index_text(rec, data, text)
        return self._insert(rec)

    def write_revision(
        self,
        rec: dict[str, Any],
        data: bytes,
        *,
        when: datetime | None = None,
        client_modified: datetime | None = None,
        modified_by: str | None = None,
    ) -> dict[str, Any]:
        when = when or now(self.ctx)
        rev = self.revision(data, when=when, client_modified=client_modified, modified_by=modified_by)
        rec["revisions"].append(rev)
        self.index_text(rec, data)
        self._log(rec, "upsert")
        return rev

    # -- deletion --------------------------------------------------------------------
    def subtree_of(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        if rec["type"] == "folder" and not rec.get("mount_ns"):
            return self.tree.subtree(rec["ns"], rec["path"])
        return []

    def delete(self, rec: dict[str, Any], *, when: datetime | None = None, by: str | None = None) -> int:
        """Soft-delete ``rec`` and everything below it; returns the number of entries removed."""
        when = when or now(self.ctx)
        victims = [rec, *self.subtree_of(rec)]
        for victim in victims:
            victim["deleted"] = True
            victim["deleted_at"] = ts(when)
            victim["deleted_by"] = by
            self.tree.drop(victim, dead=True)
            self._log(victim, "delete")
        return len(victims)

    def restore_entry(self, rec: dict[str, Any]) -> None:
        rec["deleted"] = False
        rec.pop("deleted_at", None)
        self.tree.dead[(rec["ns"], rec["path"])] = [
            r for r in self.tree.dead.get((rec["ns"], rec["path"]), []) if r is not rec
        ]
        self.tree.add(rec)
        self._log(rec, "upsert")

    def purge(self, rec: dict[str, Any]) -> None:
        for victim in [rec, *self.subtree_of(rec)]:
            self.ctx.store.collection(ENTRIES).delete(victim["id"])
            self.ctx.store.collection(TEXT).delete(victim["id"])
            self.tree.drop(victim)
            self.tree.dead[(victim["ns"], victim["path"])] = [
                r for r in self.tree.dead.get((victim["ns"], victim["path"]), []) if r is not victim
            ]

    # -- relocation ------------------------------------------------------------------
    def relocate(
        self, rec: dict[str, Any], dest_ns: str, dest_parent_rel: str, dest_name: str
    ) -> dict[str, Any]:
        """Move ``rec`` (and its subtree) to ``dest_parent_rel/dest_name`` in ``dest_ns``."""
        subtree = self.subtree_of(rec)
        affected = [rec, *subtree]
        old = {r["id"]: (r["ns"], r["path"], r["dpath"], r["name"]) for r in affected}
        for item in affected:
            self._log(item, "delete")
            self.tree.drop(item)
        dest_parent = self.tree.live.get((dest_ns, dest_parent_rel)) if dest_parent_rel else None
        dest_pd = dest_parent["dpath"] if dest_parent else ""
        old_root_path, old_root_dpath = old[rec["id"]][1], old[rec["id"]][2]
        new_root_path = dest_parent_rel + "/" + dest_name.lower()
        new_root_dpath = dest_pd + "/" + dest_name
        for item in affected:
            _, path, dpath, _ = old[item["id"]]
            item["ns"] = dest_ns
            if item is rec:
                item["path"], item["dpath"], item["name"] = new_root_path, new_root_dpath, dest_name
            else:
                item["path"] = new_root_path + path[len(old_root_path) :]
                item["dpath"] = new_root_dpath + dpath[len(old_root_dpath) :]
        for item in affected:
            self.tree.add(item)
            self._log(item, "upsert")
        return rec

    def copy(
        self, rec: dict[str, Any], dest_ns: str, dest_parent_rel: str, dest_name: str, *, owner: str | None
    ) -> dict[str, Any]:
        """Deep-copy ``rec`` to a new location (new ids, latest revision only, no explicit shares)."""
        when = now(self.ctx)

        def clone(src: dict[str, Any], parent_rel: str, name: str) -> dict[str, Any]:
            if src["type"] == "folder":
                return self.create_folder(dest_ns, parent_rel, name, when=when, owner=owner)
            latest = src["revisions"][-1]
            data = self.ctx.store.blobs.get(latest["blob"])
            extra = {k: src[k] for k in ("downloadable", "export", "tags") if k in src}
            copy_rec = self.create_file(
                dest_ns,
                parent_rel,
                name,
                data,
                when=when,
                client_modified=_parse(latest["client_modified"]),
                modified_by=owner,
                owner=owner,
                extra=extra,
            )
            text = self.ctx.store.collection(TEXT).get(src["id"])
            if text is not None:
                self.ctx.store.collection(TEXT).put({"id": copy_rec["id"], "text": text["text"]})
            return copy_rec

        root = clone(rec, dest_parent_rel, dest_name)
        if rec["type"] == "folder" and not rec.get("mount_ns"):
            base = rec["path"]
            for item in self.tree.subtree(rec["ns"], rec["path"]):
                if item.get("mount_ns"):
                    continue
                rel_in = item["path"][len(base) :]
                parent_in = parent_of(rel_in)
                clone(item, root["path"] + parent_in, item["name"])
        return root


def _parse(value: str) -> datetime:
    from emulators.providers.dropbox.common import parse_ts

    return parse_ts(value)


def namespace(ctx: ProviderContext, ns_id: str) -> dict[str, Any]:
    ns = ctx.store.collection(NAMESPACES).get(ns_id)
    assert ns is not None
    return ns
