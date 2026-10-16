"""Bytes behind Media and Recording URLs, rendered to match the content type they advertise.

Twilio stores whatever the ``MediaUrl`` served; the emulator never fetches those URLs, so it
renders a small, *valid* file of the advertised type instead (a PNG served as ``image/jpeg``
would break any client that sniffs or decodes the attachment).
"""

from __future__ import annotations

import base64

# A 16x16 JPEG and GIF, generated once offline (no imaging library at runtime).
_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNS"
    "Mj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09P"
    "T0//wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQID"
    "AAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlq"
    "c3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3"
    "+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEI"
    "FEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImK"
    "kpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDC"
    "ooor2jyj/9k="
)
_GIF_B64 = (
    "R0lGODdhEAAQAIIAAAAzmTMzmQBmmTNmmQAzzDMzzABmzDNmzCwAAAAAEAAQAEAIeAAHCBA4gOBAgwULHjjIEGHBgQ0JSoT40GHEAQYw"
    "ajyw8SJFihIJLqxIMuPAjCYdSgQwIICAAANYsnTpUmDNmixfDgyQcUABAT8DHGAZ1EDNmAN/+kzK1KZOl0GBxkxIwGZGnjEXHqWZFSaA"
    "oQOqFmQ5NWvLjGQDAgA7"
)
JPEG = base64.b64decode(_JPEG_B64)
GIF = base64.b64decode(_GIF_B64)

# One MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz, joint stereo): a 4-byte header and 413 zero
# bytes. A stream of such frames is a valid MP3 that decodes as silence.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
_MP3_FRAME_SECONDS = 1152 / 44100


def silent_mp3(seconds: float) -> bytes:
    return _MP3_FRAME * max(1, round(seconds / _MP3_FRAME_SECONDS))


def render_for_content_type(content_type: str, source: str, seed: int) -> bytes:
    """A small valid file of ``content_type`` (``source`` is the MediaUrl it stands in for)."""
    from emulators.world import documents, media

    note = f"Media attachment received via {source}"
    if content_type == "image/png":
        return documents.make_png(320, 240, seed=seed)
    if content_type == "image/jpeg":
        return JPEG
    if content_type == "image/gif":
        return GIF
    if content_type == "application/pdf":
        return documents.render("pdf", "Attachment", [note])[0]
    if content_type == "audio/mpeg":
        return silent_mp3(2.0)
    if content_type in ("audio/wav", "audio/x-wav"):
        return media.make_wav(2.0, seed=seed)
    if content_type == "video/mp4":
        return media.make_mp4(2.0, seed=seed)
    if content_type == "text/csv":
        return documents.render("csv", "Attachment", [note])[0]
    if content_type == "text/markdown":
        return documents.render("md", "Attachment", [note])[0]
    if content_type == "text/vcard":
        return f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Halcyon Advisory Group\r\nNOTE:{note}\r\nEND:VCARD\r\n".encode()
    return documents.render("txt", "Attachment", [note])[0]
