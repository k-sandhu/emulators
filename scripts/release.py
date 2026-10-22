"""Release helpers for .github/workflows/release.yml.

Stdlib only so the workflow can run it with the runner's ``python3`` before anything is installed::

    python3 scripts/release.py check v0.2.0    # tag must be v + the project version; prints prerelease=...
    python3 scripts/release.py notes v0.2.0    # Markdown notes from the commits since the previous tag
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "k-sandhu/emulators"
IMAGE = f"ghcr.io/{REPO}"

# Conventional Commit types in the order their sections appear; unknown types land in "Other changes".
SECTIONS = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build and CI",
    "ci": "Build and CI",
    "chore": "Chores",
}
SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<desc>.+)$")
# PEP 440 final releases (optionally .postN) are digits and dots; a, b, rc and .dev mark prereleases.
FINAL = re.compile(r"^\d+(?:\.\d+)*(?:\.post\d+)?$")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def package_version() -> str:
    text = (ROOT / "src" / "emulators" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    return match.group(1) if match else ""


def is_prerelease(tag_or_version: str) -> bool:
    return not FINAL.match(tag_or_version.removeprefix("v"))


def check(tag: str) -> int:
    """Refuse to publish a tag whose artifacts would carry a different version than its name says."""
    version = project_version()
    errors = []
    if tag != f"v{version}":
        errors.append(f"tag {tag} does not match the project version in pyproject.toml (expected v{version})")
    if package_version() != version:
        errors.append(
            f"src/emulators/__init__.py __version__ is {package_version()!r}, pyproject has {version!r}"
        )
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    if errors:
        return 1
    print(f"version={version}")
    print(f"prerelease={'true' if is_prerelease(version) else 'false'}")
    return 0


def previous_tag(tag: str) -> str | None:
    """Nearest earlier ``v*`` tag in ``tag``'s history; a final release skips prereleases so v0.2.0 covers all of 0.2."""
    candidates = [
        name
        for name in git("tag", "--merged", tag, "--list", "v*").split()
        if name != tag and (is_prerelease(tag) or not is_prerelease(name))
    ]
    # The nearest ancestor has the longest history.
    return max(candidates, key=lambda name: int(git("rev-list", "--count", name)), default=None)


def notes(tag: str) -> str:
    previous = previous_tag(tag)
    log = git("log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", f"{previous}..{tag}" if previous else tag)
    breaking: list[str] = []
    sections: dict[str, list[str]] = {}
    for record in filter(None, (r.strip("\n") for r in log.split("\x1e"))):
        sha, subject, body = record.split("\x1f")
        match = SUBJECT.match(subject)
        link = f"([{sha[:7]}](https://github.com/{REPO}/commit/{sha}))"
        if match:
            scope = f"**{match['scope']}:** " if match["scope"] else ""
            line = f"- {scope}{match['desc']} {link}"
            section = SECTIONS.get(match["type"], "Other changes")
            if match["bang"] or "BREAKING CHANGE" in body:
                breaking.append(line)
        else:
            line, section = f"- {subject} {link}", "Other changes"
        sections.setdefault(section, []).append(line)

    version = tag.removeprefix("v")
    download = f"https://github.com/{REPO}/releases/download/{tag}"
    out = [
        "## Install",
        "",
        "```bash",
        f"docker pull {IMAGE}:{tag}    # linux/amd64, linux/arm64",
        f"curl -fsSL -o emulators.compose.yml {download}/docker-compose.yml",
        "docker compose -f emulators.compose.yml up -d --wait",
        f"pip install {download}/emulators-{version}-py3-none-any.whl",
        "```",
        "",
        "See [Run the emulators next to your stack](https://github.com/"
        f"{REPO}/blob/{tag}/docs/howto/docker-compose.md) for joining the vendor-hostname network.",
    ]
    if breaking:
        out += ["", "## Breaking changes", "", *breaking]
    order = [*dict.fromkeys(SECTIONS.values()), "Other changes"]
    for title in sorted(sections, key=order.index):
        out += ["", f"## {title}", "", *sections[title]]
    compare = f"compare/{previous}...{tag}" if previous else f"commits/{tag}"
    out += ["", f"**Full changelog:** https://github.com/{REPO}/{compare}", ""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "notes"):
        sub.add_parser(name).add_argument("tag", help="the release tag, e.g. v0.2.0")
    args = parser.parse_args()
    if args.command == "check":
        return check(args.tag)
    sys.stdout.write(notes(args.tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
