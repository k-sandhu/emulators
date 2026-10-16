"""search.messages / search.files / search.all (user tokens only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def test_search_requires_a_user_token_and_scope(slack: Slack) -> None:
    assert await slack.err("search.messages", None, query="audit") == "not_allowed_token_type"
    narrow = await slack.mint(user="p001", scopes=["users:read"])
    body = await slack.api("search.messages", narrow, query="audit")
    assert body["error"] == "missing_scope" and body["needed"] == "search:read"
    eleanor = await slack.user_token("p001")
    assert await slack.err("search.messages", eleanor) == "missing_query"
    assert await slack.err("search.messages", eleanor, query="x", sort="random") == "invalid_sort"


async def test_search_messages_shape(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    body = await slack.ok("search.messages", eleanor, query="coffee machine")
    assert body["query"] == "coffee machine"
    messages = body["messages"]
    assert messages["total"] > 0
    pages = -(-messages["total"] // 20)
    assert messages["paging"] == {"count": 20, "total": messages["total"], "page": 1, "pages": pages}
    assert messages["pagination"]["total_count"] == messages["total"] and messages["pagination"]["first"] == 1
    match = messages["matches"][0]
    assert "coffee machine" in match["text"].lower() and match["type"] == "message"
    assert match["channel"]["id"][0] in "CGD" and match["channel"]["name"]
    assert match["permalink"].startswith("https://halcyonadvisory.slack.com/archives/")
    assert (
        match["permalink"].endswith("p" + match["ts"].replace(".", "")) or "thread_ts" in match["permalink"]
    )
    assert match["username"] and match["blocks"] and match["team"] == slack.ctx.world.org.slack_team_id
    nothing = (await slack.ok("search.messages", eleanor, query="zzzxqj"))["messages"]
    assert nothing["total"] == 0 and nothing["matches"] == [] and nothing["paging"]["pages"] == 1


async def test_search_modifiers(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    general = slack.channel_id("general")
    in_general = (await slack.ok("search.messages", eleanor, query="in:#general", count=100))["messages"][
        "matches"
    ]
    assert in_general and {m["channel"]["id"] for m in in_general} == {general}
    by_id = (await slack.ok("search.messages", eleanor, query=f"in:{general}", count=100))["messages"][
        "total"
    ]
    assert by_id == len(in_general) or by_id >= len(in_general)
    author = next(m["user"] for m in in_general)
    handle = slack.ctx.store.collection("users").get(author)["name"]
    from_user = (await slack.ok("search.messages", eleanor, query=f"in:#general from:@{handle}", count=100))[
        "messages"
    ]["matches"]
    assert from_user and {m["user"] for m in from_user} == {author}
    mention = (await slack.ok("search.messages", eleanor, query=f"in:#general from:<@{author}>", count=100))[
        "messages"
    ]["total"]
    assert mention == len(from_user)
    await slack.ok("chat.postMessage", eleanor, channel=general, text="mine marker-alpha")
    mine = (await slack.ok("search.messages", eleanor, query="marker-alpha from:me"))["messages"]["matches"]
    assert len(mine) == 1 and mine[0]["user"] == slack.uid("p001")
    assert (await slack.ok("search.messages", eleanor, query="marker-alpha -from:me"))["messages"][
        "total"
    ] == 0
    maya = await slack.user_token("p018")
    assert (await slack.ok("search.messages", maya, query="marker-alpha -from:me"))["messages"]["total"] == 1
    assert (await slack.ok("search.messages", eleanor, query="marker-alpha -in:#general"))["messages"][
        "total"
    ] == 0
    only = (await slack.ok("search.messages", eleanor, query="in:#general -coffee", count=100))["messages"][
        "matches"
    ]
    assert only and all("coffee" not in m["text"].lower() for m in only)


async def test_search_date_filters(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    everything = (
        await slack.ok(
            "search.messages", eleanor, query="in:#general", count=100, sort="timestamp", sort_dir="asc"
        )
    )["messages"]["matches"]
    stamps = [float(m["ts"]) for m in everything]
    assert stamps == sorted(stamps)
    first = datetime.fromtimestamp(stamps[0], UTC).date()
    last = datetime.fromtimestamp(stamps[-1], UTC).date()
    after = (
        await slack.ok("search.messages", eleanor, query=f"in:#general after:{first.isoformat()}", count=100)
    )["messages"]["matches"]
    assert after and all(datetime.fromtimestamp(float(m["ts"]), UTC).date() > first for m in after)
    before = (
        await slack.ok("search.messages", eleanor, query=f"in:#general before:{last.isoformat()}", count=100)
    )["messages"]["matches"]
    assert before and all(datetime.fromtimestamp(float(m["ts"]), UTC).date() < last for m in before)
    on = (await slack.ok("search.messages", eleanor, query=f"in:#general on:{first.isoformat()}", count=100))[
        "messages"
    ]["matches"]
    assert on and all(datetime.fromtimestamp(float(m["ts"]), UTC).date() == first for m in on)
    during = (
        await slack.ok(
            "search.messages", eleanor, query=f"in:#general during:{first.strftime('%Y-%m')}", count=100
        )
    )["messages"]["total"]
    assert during == len(everything)


async def test_search_has_and_is_filters(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    general = slack.channel_id("general")
    reactions = (await slack.ok("search.messages", eleanor, query="in:#general has:reaction", count=100))[
        "messages"
    ]["matches"]
    assert reactions and all("no_reactions" not in m for m in reactions)
    threads = (await slack.ok("search.messages", eleanor, query="in:#general is:thread", count=100))[
        "messages"
    ]["matches"]
    assert threads
    posted = await slack.ok(
        "chat.postMessage", eleanor, channel=general, text="see https://example.com/page for details"
    )
    links = (await slack.ok("search.messages", eleanor, query="has:link details"))["messages"]["matches"]
    assert [m["ts"] for m in links] == [posted["ts"]]
    await slack.ok("pins.add", eleanor, channel=general, timestamp=posted["ts"])
    pinned = (await slack.ok("search.messages", eleanor, query="has:pin details"))["messages"]["matches"]
    assert [m["ts"] for m in pinned] == [posted["ts"]]


async def test_search_phrases_paging_sort_and_highlight(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    general = slack.channel_id("general")
    for text in ("alpha beta gamma", "beta alpha", "alpha only", "unrelated"):
        await slack.ok("chat.postMessage", eleanor, channel=general, text=text)
    phrase = (await slack.ok("search.messages", eleanor, query='"alpha beta"'))["messages"]["matches"]
    assert [m["text"] for m in phrase] == ["alpha beta gamma"]
    both = (await slack.ok("search.messages", eleanor, query="alpha beta", sort="timestamp"))["messages"][
        "matches"
    ]
    assert [m["text"] for m in both] == ["beta alpha", "alpha beta gamma"]
    page1 = (
        await slack.ok("search.messages", eleanor, query="alpha", count=2, sort="timestamp", sort_dir="asc")
    )["messages"]
    assert [m["text"] for m in page1["matches"]] == ["alpha beta gamma", "beta alpha"]
    assert page1["paging"] == {"count": 2, "total": 3, "page": 1, "pages": 2}
    page2 = (
        await slack.ok(
            "search.messages", eleanor, query="alpha", count=2, page=2, sort="timestamp", sort_dir="asc"
        )
    )["messages"]
    assert [m["text"] for m in page2["matches"]] == ["alpha only"] and page2["pagination"]["first"] == 3
    highlighted = (await slack.ok("search.messages", eleanor, query="alpha only", highlight=True))[
        "messages"
    ]["matches"][0]
    assert "alpha" in highlighted["text"]
    scored = (await slack.ok("search.messages", eleanor, query="alpha", sort="score"))["messages"]["matches"]
    assert scored and scored[0]["score"] >= scored[-1]["score"]
    cursor = await slack.ok("search.messages", eleanor, query="alpha", count=2, cursor="*")
    next_cursor = cursor["messages"]["response_metadata"]["next_cursor"]
    assert next_cursor
    rest = await slack.ok("search.messages", eleanor, query="alpha", count=2, cursor=next_cursor)
    assert (
        len(rest["messages"]["matches"]) == 1 and rest["messages"]["response_metadata"]["next_cursor"] == ""
    )


async def test_search_respects_conversation_privacy(slack: Slack) -> None:
    partners = slack.channel_id("partners")
    eleanor = await slack.user_token("p001")
    await slack.ok("chat.postMessage", eleanor, channel=partners, text="confidential partner bonus pool")
    found = (await slack.ok("search.messages", eleanor, query="confidential partner bonus"))["messages"][
        "matches"
    ]
    assert len(found) == 1 and found[0]["channel"]["is_private"] is True
    junior = await slack.mint(user="p020")
    assert (await slack.ok("search.messages", junior, query="confidential partner bonus"))["messages"][
        "total"
    ] == 0
    assert (await slack.ok("search.messages", junior, query="in:#partners"))["messages"]["total"] == 0
    im = (await slack.ok("conversations.open", eleanor, users=slack.uid("p018")))["channel"]["id"]
    await slack.ok("chat.postMessage", eleanor, channel=im, text="private dm zebra")
    assert (await slack.ok("search.messages", eleanor, query="zebra"))["messages"]["matches"][0]["channel"][
        "is_im"
    ] is True
    maya = await slack.user_token("p018")
    assert (await slack.ok("search.messages", maya, query="zebra"))["messages"]["total"] == 1
    assert (await slack.ok("search.messages", junior, query="zebra"))["messages"]["total"] == 0
    dm = (await slack.ok("search.messages", eleanor, query="zebra to:me"))["messages"]["total"]
    assert dm == 0
    to_maya = (await slack.ok("search.messages", eleanor, query=f"zebra to:<@{slack.uid('p018')}>"))[
        "messages"
    ]["total"]
    assert to_maya == 1


async def test_search_files_and_all(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    general = slack.channel_id("general")
    done = await slack.ok(
        "files.upload", eleanor, content="data", filename="budget-2027.csv", channels=general
    )
    files = (await slack.ok("search.files", eleanor, query="budget-2027"))["files"]
    assert files["total"] == 1 and files["matches"][0]["id"] == done["file"]["id"]
    assert (await slack.ok("search.files", eleanor, query="budget-2027 from:me"))["files"]["total"] == 1
    assert (await slack.ok("search.files", eleanor, query="budget-2027", count=1))["files"]["paging"][
        "pages"
    ] == 1
    maya = await slack.mint(user="p020")
    assert (await slack.ok("search.files", maya, query="budget-2027"))["files"]["total"] == 1
    private = slack.channel_id("partners")
    secret = await slack.ok(
        "files.upload", eleanor, content="x", filename="partners-only.txt", channels=private
    )
    assert (await slack.ok("search.files", maya, query="partners-only"))["files"]["total"] == 0
    assert (await slack.ok("search.files", eleanor, query="partners-only"))["files"]["matches"][0][
        "id"
    ] == secret["file"]["id"]
    everything = await slack.ok("search.all", eleanor, query="budget")
    assert everything["files"]["total"] == 1 and "messages" in everything
    assert await slack.err("search.files", eleanor) == "missing_query"
