#!/usr/bin/env python3
"""Render a "top repositories by stars" markdown block into README targets.

Fetches the public repositories of a GitHub user (or reads them from a JSON
file with --input), filters/sorts them deterministically, and replaces only
the content between the ``<!-- top-repos:start -->`` and
``<!-- top-repos:end -->`` marker lines in every README target.

The target list comes from the comma-separated ``TOP_REPOS_README``
environment variable (default ``README.md``). Targets are validated
all-or-nothing: the marked region of every target is parsed and the block is
rendered once BEFORE anything is written, so one invalid target can never
leave a partially updated set of READMEs.

Standard library only. The script never commits or pushes; the scheduled
workflow owns committing. It is idempotent: unchanged input does not modify
any target.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any

START_MARKER = "<!-- top-repos:start -->"
END_MARKER = "<!-- top-repos:end -->"
PER_PAGE = 100
MAX_PAGES = 10
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "egdev6-profile-top-repos/1.0"


# ---------------------------------------------------------------------------
# Configuration


class Config:
    """Runtime configuration resolved from environment variables."""

    def __init__(self) -> None:
        self.user = os.environ.get("TOP_REPOS_USER", "egdev6")
        self.top_n = _parse_top_n(os.environ.get("TOP_N", ""))
        self.exclude = _parse_exclude(os.environ.get("TOP_REPOS_EXCLUDE", ""))
        self.readme_paths = _parse_readme_paths(os.environ.get("TOP_REPOS_README", ""))
        self.token = os.environ.get("GITHUB_TOKEN", "")


def _parse_top_n(raw: str) -> int:
    if not raw.strip():
        return 5
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"TOP_N must be an integer, got: {raw!r}")
    if value < 1:
        raise ValueError(f"TOP_N must be >= 1, got: {value}")
    return value


def _parse_exclude(raw: str) -> set[str]:
    """Parse a comma-separated exclusion list, ignoring blank entries."""
    return {entry.strip() for entry in raw.split(",") if entry.strip()}


def _parse_readme_paths(raw: str) -> list[str]:
    """Parse a comma-separated list of README target paths.

    Entries are stripped of surrounding whitespace and blank entries are
    ignored, so ``"README.md, README.es.md"`` and
    ``"README.md,README.es.md"`` behave identically. An empty value (or one
    that yields no entries) falls back to ``README.md``.
    """
    paths = [entry.strip() for entry in raw.split(",") if entry.strip()]
    return paths or ["README.md"]


# ---------------------------------------------------------------------------
# Data acquisition


def fetch_repositories(user: str, token: str) -> list[dict[str, Any]]:
    """Fetch all owner repositories of ``user`` from the GitHub API.

    Follows pages until a page returns fewer than ``PER_PAGE`` entries,
    capped at ``MAX_PAGES`` pages. Raises ``RuntimeError`` with a clear
    message on any non-2xx response.
    """
    repositories: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"https://api.github.com/users/{user}/repos?per_page={PER_PAGE}&type=owner&page={page}"
        payload = _fetch_page(url, token)
        if not isinstance(payload, list):
            raise RuntimeError(f"GitHub API returned unexpected payload for {url}: expected a JSON array")
        repositories.extend(payload)
        if len(payload) < PER_PAGE:
            break
    return repositories


def _fetch_page(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None or not (200 <= status < 300):
                raise RuntimeError(f"GitHub API returned non-2xx status {status} for {url}")
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API request failed for {url}: HTTP {error.code} {error.reason}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub API request failed for {url}: {error.reason}") from error
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub API returned invalid JSON for {url}: {error}") from error


def load_repositories_from_file(path: str) -> list[dict[str, Any]]:
    """Load a raw repository array from a JSON file (offline testing)."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise RuntimeError(f"Input file {path} must contain a JSON array of repositories")
    return payload


# ---------------------------------------------------------------------------
# Filtering, sorting, rendering


def select_top_repositories(
    repositories: list[dict[str, Any]],
    top_n: int,
    exclude: set[str],
) -> list[dict[str, Any]]:
    """Filter, sort deterministically, and truncate to ``top_n`` entries."""
    eligible = []
    for repository in repositories:
        if not isinstance(repository, dict):
            continue
        name = repository.get("name")
        html_url = repository.get("html_url")
        # Repositories missing name or html_url cannot be rendered: drop them.
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(html_url, str) or not html_url:
            continue
        # Missing fork/archived/private keys are treated as False (safe defaults).
        if repository.get("fork", False):
            continue
        if repository.get("archived", False):
            continue
        if repository.get("private", False):
            continue
        if name in exclude:
            continue
        stars = repository.get("stargazers_count", 0)
        if not isinstance(stars, int) or isinstance(stars, bool):
            stars = 0
        eligible.append({"name": name, "html_url": html_url, "description": repository.get("description"), "stars": stars})
    eligible.sort(key=lambda repo: (-repo["stars"], repo["name"]))
    return eligible[:top_n]


def _normalize_description(description: Any) -> str | None:
    """Return a cleaned single-line description, or None when blank/missing."""
    if not isinstance(description, str):
        return None
    collapsed = re.sub(r"\s+", " ", description).strip()
    return collapsed or None


# ASCII punctuation markdown would otherwise interpret when embedded mid-line:
# emphasis (*), links and images ([ ] ( )), code spans (`), raw HTML (< >),
# and the remaining punctuation it treats specially. Periods and hyphens are
# excluded: they are literal in this embedded position and escaping them
# would alter already-published blocks.
_MARKDOWN_ESCAPE_PATTERN = re.compile(r"([\\`*\[\](){}#<>|~!])")
# Underscore opens or closes emphasis only at a word boundary; an intraword
# underscore (as in TOP_REPOS_EXCLUDE) is always literal in CommonMark and
# must stay unescaped so published blocks remain byte-identical.
_MARKDOWN_UNDERSCORE_PATTERN = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def escape_markdown(text: str) -> str:
    """Backslash-escape the markdown control characters in ``text``.

    Repository names and descriptions are embedded verbatim into a public
    README, so characters markdown would otherwise interpret must be
    escaped: emphasis (``*`` and word-boundary ``_``), links and images
    (``[``, ``]``, ``(``, ``)``), code spans (backtick), raw HTML (``<``,
    ``>``), and the remaining punctuation markdown treats specially
    (``\\``, ``{``, ``}``, ``#``, ``!``, ``|``, ``~``). Intraword
    underscores are always literal in CommonMark and stay unescaped.
    Every escape is rendering-safe: CommonMark renders a backslash-escaped
    ASCII punctuation character as the bare character.
    """
    escaped = _MARKDOWN_ESCAPE_PATTERN.sub(r"\\\1", text)
    return _MARKDOWN_UNDERSCORE_PATTERN.sub(r"\\_", escaped)


def render_repository_line(repository: dict[str, Any]) -> str:
    """Render one markdown line for a repository."""
    description = _normalize_description(repository["description"])
    name = escape_markdown(repository["name"])
    # html_url is rendered unescaped on purpose: it is trusted because the
    # GitHub API only ever returns https://github.com/<owner>/<repo>, and
    # --input is a test-only path that the workflow never uses.
    base = f"- [**{name}**]({repository['html_url']}) · ⭐ {repository['stars']}"
    if description:
        return f"- [**{name}**]({repository['html_url']}) — {escape_markdown(description)} · ⭐ {repository['stars']}"
    return base


def render_block(repositories: list[dict[str, Any]]) -> list[str]:
    """Render the markdown lines of the block; the block must not be empty."""
    if not repositories:
        raise RuntimeError("Rendered block is empty: no eligible repositories to display")
    return [render_repository_line(repository) for repository in repositories]


# ---------------------------------------------------------------------------
# README update


def _split_lines_keepends(content: str) -> list[str]:
    return content.splitlines(keepends=True)


def _line_without_terminator(line: str) -> str:
    return line.rstrip("\r\n")


def replace_marked_region(content: str, rendered_lines: list[str]) -> str:
    """Replace only the content between the two marker lines.

    Marker lines and everything outside them survive byte-for-byte, including
    trailing whitespace and the presence or absence of a final newline.
    """
    lines = _split_lines_keepends(content)
    start_indexes = [i for i, line in enumerate(lines) if _line_without_terminator(line) == START_MARKER]
    end_indexes = [i for i, line in enumerate(lines) if _line_without_terminator(line) == END_MARKER]

    if not start_indexes:
        raise RuntimeError(f"README is missing the start marker line: {START_MARKER}")
    if not end_indexes:
        raise RuntimeError(f"README is missing the end marker line: {END_MARKER}")
    if len(start_indexes) > 1:
        raise RuntimeError(f"Start marker {START_MARKER} appears {len(start_indexes)} times; exactly one is required")
    if len(end_indexes) > 1:
        raise RuntimeError(f"End marker {END_MARKER} appears {len(end_indexes)} times; exactly one is required")
    start_index, end_index = start_indexes[0], end_indexes[0]
    if end_index < start_index:
        raise RuntimeError(f"End marker {END_MARKER} precedes start marker {START_MARKER}")

    # Rendered lines must use the same terminator as the start marker line
    # so a CRLF README does not end up with mixed line endings.
    terminator = "\r\n" if lines[start_index].endswith("\r\n") else "\n"
    rendered_with_terminators = [line + terminator for line in rendered_lines]
    new_lines = lines[: start_index + 1] + rendered_with_terminators + lines[end_index:]
    return "".join(new_lines)


def plan_target(path: str, rendered_lines: list[str]) -> tuple[str, str]:
    """Validate one target and return ``(current, updated)`` without writing.

    Raises ``RuntimeError`` naming ``path`` when its markers are missing,
    duplicated, or out of order, so callers can validate every target before
    writing any of them (all-or-nothing semantics).
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        current = handle.read()
    try:
        updated = replace_marked_region(current, rendered_lines)
    except RuntimeError as error:
        raise RuntimeError(f"{path}: {error}") from error
    return current, updated


def write_target(path: str, updated: str) -> None:
    """Atomically replace ``path`` with ``updated``.

    The replacement is written to a temporary file in the same directory as
    the target and then moved into place with ``os.replace``, so a crash or
    a full disk can never leave a truncated target: on any error the
    original file survives byte-for-byte.
    """
    directory = os.path.dirname(os.path.abspath(path))
    file_descriptor, temp_path = tempfile.mkstemp(dir=directory, prefix=".top-repos-", suffix=".tmp")
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        # Preserve the original file's permission bits through the replace.
        os.chmod(temp_path, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the top-repositories block in the configured README targets")
    parser.add_argument("--dry-run", action="store_true", help="Print the rendered block to stdout and do not touch any target")
    parser.add_argument("--input", metavar="FILE", help="Read the raw repository array JSON from FILE instead of calling the network")
    args = parser.parse_args(argv)

    config = Config()

    if args.input:
        repositories = load_repositories_from_file(args.input)
    else:
        repositories = fetch_repositories(config.user, config.token)

    top = select_top_repositories(repositories, config.top_n, config.exclude)
    rendered_lines = render_block(top)

    if args.dry_run:
        for line in rendered_lines:
            print(line)
        return 0

    # All-or-nothing: parse the marked region of EVERY target and render its
    # replacement BEFORE writing anything, so an invalid target can never
    # leave a partially updated set of READMEs.
    plans: list[tuple[str, str, str]] = []
    for path in config.readme_paths:
        current, updated = plan_target(path, rendered_lines)
        plans.append((path, current, updated))

    for path, current, updated in plans:
        if updated == current:
            print(f"No changes to the top-repositories block in {path}")
            continue
        write_target(path, updated)
        print(f"Updated the top-repositories block in {path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
