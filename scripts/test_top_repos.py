#!/usr/bin/env python3
"""Offline tests for scripts/top_repos.py.

Runs the real CLI through subprocess (no imports of internals, no network).
Prints one PASS/FAIL line per case and exits non-zero when any case fails.
Never touches the real README.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "top_repos.py")
FIXTURE = os.path.join(REPO_ROOT, "scripts", "fixtures", "repos.json")

EXPECTED_BLOCK_EXCLUDE_DOTFILES = """\
- [**mega-tool**](https://github.com/egdev6/mega-tool) — A CLI toolbox for everyday automation · ⭐ 1200
- [**web-frame**](https://github.com/egdev6/web-frame) — A tiny web framework. Wrapped across two lines. · ⭐ 950
- [**alpha-lib**](https://github.com/egdev6/alpha-lib) — Small utility library · ⭐ 500
- [**beta-lib**](https://github.com/egdev6/beta-lib) — Another small library, tied with alpha-lib on stars · ⭐ 500
- [**silent-repo**](https://github.com/egdev6/silent-repo) · ⭐ 300
"""

EXPECTED_BLOCK_EMPTY_EXCLUDE = """\
- [**mega-tool**](https://github.com/egdev6/mega-tool) — A CLI toolbox for everyday automation · ⭐ 1200
- [**web-frame**](https://github.com/egdev6/web-frame) — A tiny web framework. Wrapped across two lines. · ⭐ 950
- [**dotfiles**](https://github.com/egdev6/dotfiles) — Eligible but dropped via TOP_REPOS_EXCLUDE · ⭐ 800
- [**alpha-lib**](https://github.com/egdev6/alpha-lib) — Small utility library · ⭐ 500
- [**beta-lib**](https://github.com/egdev6/beta-lib) — Another small library, tied with alpha-lib on stars · ⭐ 500
"""

EXPECTED_ESCAPE_LINE = (
    "- [**markdown-tricky**](https://github.com/egdev6/markdown-tricky)"
    " — Tricky \\[x\\]\\(y\\) \\*bold\\* \\<b\\>html\\</b\\> and \\_under\\_ · ⭐ 290\n"
)

EXPECTED_NAME_ESCAPE_LINE = (
    "- [**esc-name\\]with\\\\slash**](https://github.com/egdev6/esc-name]with\\slash)"
    " — Name has a \\] bracket and a \\\\ backslash · ⭐ 20\n"
)

README_TEMPLATE = """\
# egdev6

Distinctive intro text that must survive byte-for-byte. SENTINEL-BEGIN-42

<!-- top-repos:start -->
<!-- stale placeholder line one -->
<!-- stale placeholder line two -->
<!-- top-repos:end -->

Distinctive outro text with a trailing-double-space line:
Trailing spaces after the colon above must survive. SENTINEL-END-77
No final newline here"""


def run_cli(args: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in ("TOP_REPOS_USER", "TOP_N", "TOP_REPOS_EXCLUDE", "TOP_REPOS_README", "GITHUB_TOKEN"):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


failures: list[str] = []


def report(case: int, description: str, passed: bool, observed: str) -> None:
    label = "PASS" if passed else "FAIL"
    print(f"Case {case} {description}: {label}")
    if not passed:
        print(f"  observed: {observed}")
        failures.append(f"case {case}")


def dry_run_block(exclude: str) -> tuple[str, str]:
    result = run_cli(
        ["--dry-run", "--input", os.path.relpath(FIXTURE, REPO_ROOT)],
        {"TOP_REPOS_EXCLUDE": exclude},
    )
    return result.stdout, f"returncode={result.returncode} stderr={result.stderr.strip()}"


def case_1() -> None:
    stdout, observed = dry_run_block("dotfiles")
    passed = observed == "returncode=0 stderr=" and stdout == EXPECTED_BLOCK_EXCLUDE_DOTFILES
    report(1, "dry-run with dotfiles excluded renders the exact expected block", passed, observed + "\n  stdout=" + repr(stdout))


def case_2() -> None:
    stdout, observed = dry_run_block("")
    has_dotfiles = "- [**dotfiles**](https://github.com/egdev6/dotfiles)" in stdout
    passed = observed == "returncode=0 stderr=" and stdout == EXPECTED_BLOCK_EMPTY_EXCLUDE and has_dotfiles
    report(2, "dry-run with empty exclude list renders dotfiles and the exact expected block", passed, observed + "\n  stdout=" + repr(stdout))


def make_temp_readme(directory: str) -> str:
    path = os.path.join(directory, "README.md")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(README_TEMPLATE)
    return path


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def case_3_and_4() -> None:
    with tempfile.TemporaryDirectory() as directory:
        readme = make_temp_readme(directory)
        before = read_bytes(readme)
        start_marker = b"<!-- top-repos:start -->\n"
        end_marker = b"<!-- top-repos:end -->\n"
        prefix, rest = before.split(start_marker, 1)
        _, suffix = rest.split(end_marker, 1)

        # Case 3: in-place update touches only the marked region.
        result = run_cli(
            ["--input", os.path.relpath(FIXTURE, REPO_ROOT)],
            {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_REPOS_README": readme},
        )
        after = read_bytes(readme)
        new_prefix, new_rest = after.split(start_marker, 1)
        region, new_suffix = new_rest.split(end_marker, 1)
        passed = (
            result.returncode == 0
            and new_prefix == prefix
            and new_suffix == suffix
            and region.decode("utf-8") == EXPECTED_BLOCK_EXCLUDE_DOTFILES
        )
        observed = (
            f"returncode={result.returncode} stderr={result.stderr.strip()} "
            f"prefix_intact={new_prefix == prefix} suffix_intact={new_suffix == suffix} "
            f"region={region.decode('utf-8')!r}"
        )
        report(3, "in-place update changes only the marked region", passed, observed)

        # Case 4: a second run is a no-op.
        unchanged_bytes = read_bytes(readme)
        result_2 = run_cli(
            ["--input", os.path.relpath(FIXTURE, REPO_ROOT)],
            {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_REPOS_README": readme},
        )
        passed_2 = result_2.returncode == 0 and read_bytes(readme) == unchanged_bytes
        observed_2 = (
            f"returncode={result_2.returncode} stderr={result_2.stderr.strip()} "
            f"bytes_equal={read_bytes(readme) == unchanged_bytes}"
        )
        report(4, "second run over the same file leaves it byte-identical (idempotence)", passed_2, observed_2)


def case_5() -> None:
    with tempfile.TemporaryDirectory() as directory:
        readme = make_temp_readme(directory)
        # Remove the markers entirely, keeping the surrounding text.
        content = README_TEMPLATE
        for marker in ("<!-- top-repos:start -->\n", "<!-- top-repos:end -->\n"):
            content = content.replace(marker, "")
        with open(readme, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        before = read_bytes(readme)
        result = run_cli(
            ["--input", os.path.relpath(FIXTURE, REPO_ROOT)],
            {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_REPOS_README": readme},
        )
        passed = result.returncode != 0 and read_bytes(readme) == before and result.stderr.strip() != ""
        observed = (
            f"returncode={result.returncode} stderr={result.stderr.strip()} "
            f"file_unmodified={read_bytes(readme) == before}"
        )
        report(5, "README without markers fails with non-zero exit and no modification", passed, observed)


def case_6_atomic_write() -> None:
    """FIX 1: a failed write must leave the original README intact."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        report(6, "atomic write failure keeps original README byte-identical", True, "skipped: running as root, directory chmod cannot fail the write")
        return
    with tempfile.TemporaryDirectory() as directory:
        readme = make_temp_readme(directory)
        before = read_bytes(readme)
        os.chmod(directory, 0o555)
        try:
            result = run_cli(
                ["--input", os.path.relpath(FIXTURE, REPO_ROOT)],
                {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_REPOS_README": readme},
            )
            passed = (
                result.returncode != 0
                and "error:" in result.stderr
                and "Traceback" not in result.stderr
                and read_bytes(readme) == before
            )
            observed = (
                f"returncode={result.returncode} stderr={result.stderr.strip()} "
                f"file_unmodified={read_bytes(readme) == before}"
            )
        finally:
            os.chmod(directory, 0o755)
    report(6, "atomic write failure keeps original README byte-identical", passed, observed)


def case_7_crlf() -> None:
    """FIX 3: a CRLF README must keep CRLF endings inside the region."""
    with tempfile.TemporaryDirectory() as directory:
        readme = os.path.join(directory, "README.md")
        with open(readme, "w", encoding="utf-8", newline="") as handle:
            handle.write(README_TEMPLATE.replace("\n", "\r\n"))
        before = read_bytes(readme)
        start_marker = b"<!-- top-repos:start -->\r\n"
        end_marker = b"<!-- top-repos:end -->\r\n"
        prefix, rest = before.split(start_marker, 1)
        _, suffix = rest.split(end_marker, 1)

        result = run_cli(
            ["--input", os.path.relpath(FIXTURE, REPO_ROOT)],
            {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_REPOS_README": readme},
        )
        after = read_bytes(readme)
        new_prefix, new_rest = after.split(start_marker, 1)
        region, new_suffix = new_rest.split(end_marker, 1)
        expected_region = EXPECTED_BLOCK_EXCLUDE_DOTFILES.replace("\n", "\r\n").encode("utf-8")
        result_2 = run_cli(
            ["--input", os.path.relpath(FIXTURE, REPO_ROOT)],
            {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_REPOS_README": readme},
        )
        passed = (
            result.returncode == 0
            and new_prefix == prefix
            and new_suffix == suffix
            and region == expected_region
            and b"\n" not in region.replace(b"\r\n", b"")
            and result_2.returncode == 0
            and read_bytes(readme) == after
        )
        observed = (
            f"returncode={result.returncode} stderr={result.stderr.strip()} "
            f"prefix_intact={new_prefix == prefix} suffix_intact={new_suffix == suffix} "
            f"region_crlf_exact={region == expected_region} "
            f"idempotent={result_2.returncode == 0 and read_bytes(readme) == after}"
        )
    report(7, "CRLF README keeps CRLF endings and is idempotent", passed, observed)


def case_8_clean_errors() -> None:
    """FIX 2: bad TOP_N and invalid JSON input must fail cleanly, no traceback."""
    inputs = [
        ("TOP_N=0", ["--dry-run", "--input", os.path.relpath(FIXTURE, REPO_ROOT)], {"TOP_N": "0"}),
        ("TOP_N=abc", ["--dry-run", "--input", os.path.relpath(FIXTURE, REPO_ROOT)], {"TOP_N": "abc"}),
    ]
    passed = True
    observed_parts = []
    # The invalid-JSON sub-case must run while the temporary file still exists:
    # if the CLI runs after the TemporaryDirectory exits, the case degenerates
    # into a missing-file check and never reaches json.JSONDecodeError.
    with tempfile.TemporaryDirectory() as directory:
        bad_json = os.path.join(directory, "bad.json")
        with open(bad_json, "w", encoding="utf-8") as handle:
            handle.write("{this is not json")
        inputs.append(("invalid JSON input", ["--dry-run", "--input", bad_json], {}))
        for label, args, env_overrides in inputs:
            result = run_cli(args, env_overrides)
            ok = result.returncode != 0 and "error:" in result.stderr and "Traceback" not in result.stderr
            passed = passed and ok
            observed_parts.append(f"{label}: returncode={result.returncode} stderr={result.stderr.strip()!r}")
    report(8, "bad TOP_N and invalid JSON fail with clean error: lines and no traceback", passed, " | ".join(observed_parts))


def case_9_markdown_escaping() -> None:
    """FIX 4: markdown control characters in names/descriptions are escaped."""
    result = run_cli(
        ["--dry-run", "--input", os.path.relpath(FIXTURE, REPO_ROOT)],
        {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_N": "20"},
    )
    passed = result.returncode == 0 and EXPECTED_ESCAPE_LINE in result.stdout
    observed = (
        f"returncode={result.returncode} stderr={result.stderr.strip()} "
        f"escaped_line_present={EXPECTED_ESCAPE_LINE in result.stdout}"
    )
    report(9, "markdown control characters are escaped in rendered links and text", passed, observed)


def case_10_name_escaping() -> None:
    """FIX 4: markdown control characters in the repository NAME are escaped."""
    result = run_cli(
        ["--dry-run", "--input", os.path.relpath(FIXTURE, REPO_ROOT)],
        {"TOP_REPOS_EXCLUDE": "dotfiles", "TOP_N": "20"},
    )
    passed = result.returncode == 0 and EXPECTED_NAME_ESCAPE_LINE in result.stdout
    observed = (
        f"returncode={result.returncode} stderr={result.stderr.strip()} "
        f"escaped_name_line_present={EXPECTED_NAME_ESCAPE_LINE in result.stdout}"
    )
    report(10, "markdown control characters in the repository NAME are escaped", passed, observed)


def main() -> int:
    case_1()
    case_2()
    case_3_and_4()
    case_5()
    case_6_atomic_write()
    case_7_crlf()
    case_8_clean_errors()
    case_9_markdown_escaping()
    case_10_name_escaping()
    if failures:
        print(f"FAILED: {len(failures)} case(s) failed: {', '.join(failures)}")
        return 1
    print("All cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
