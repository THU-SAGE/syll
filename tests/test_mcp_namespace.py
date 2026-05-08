"""Phase 1b: namespace helpers (pure functions, no MCP server required)."""

from __future__ import annotations

import re

from syll.agent.mcp import canonical_name, parse_namespaced

# ── canonical_name ────────────────────────────────────────────────────────


def test_canonical_short_name_uses_double_underscore():
    assert canonical_name("fs", "read_file") == "mcp__fs__read_file"


def test_canonical_replaces_disallowed_chars_in_tool_name():
    # MCP servers may use dots, slashes, etc. — sanitized to underscore.
    assert canonical_name("fs", "read.file") == "mcp__fs__read_file"
    assert canonical_name("fs", "list/dir") == "mcp__fs__list_dir"
    assert canonical_name("fs", "do@thing") == "mcp__fs__do_thing"


def test_canonical_clamps_to_64_chars_with_hash_suffix():
    # 70-char tool name → must clamp to exactly 64.
    long_tool = "a" * 70
    n = canonical_name("fs", long_tool)
    assert len(n) == 64, f"expected 64, got {len(n)}: {n}"
    # Suffix is `_<4 hex chars>`. Layout: "mcp__fs__" (9) + 50 a's + "_" + 4 hex
    # = 9 + 50 + 1 + 4 = 64.
    assert re.match(r"^mcp__fs__a{50}_[0-9a-f]{4}$", n), n


def test_canonical_under_64_no_suffix():
    # Right at the boundary: 64 chars exactly, no suffix appended.
    server = "s"
    # mcp__s__ + 56 a's = 8 + 56 = 64 chars.
    tool = "a" * 56
    n = canonical_name(server, tool)
    assert len(n) == 64
    assert n == f"mcp__{server}__{tool}"


def test_canonical_just_over_64_gets_suffix():
    server = "s"
    # mcp__s__ + 57 a's = 65 chars → must clamp.
    tool = "a" * 57
    n = canonical_name(server, tool)
    assert len(n) == 64
    assert re.match(r"^mcp__s__a+_[0-9a-f]{4}$", n)


def test_canonical_collision_after_truncation_yields_distinct_hashes():
    # Two long but DIFFERENT tools must produce different clamped names.
    n1 = canonical_name("fs", "a" * 70 + "_alpha")
    n2 = canonical_name("fs", "a" * 70 + "_beta")
    assert n1 != n2, f"hash suffixes collided: {n1!r} == {n2!r}"
    assert len(n1) == 64 == len(n2)


def test_canonical_is_openai_tool_name_safe():
    # OpenAI tool-name regex: ^[a-zA-Z0-9_-]{1,64}$
    pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
    for server, tool in [
        ("fs", "read_file"),
        ("fs", "tool.with.dots"),
        ("fs", "a" * 70),
        ("playwright", "browser_run_code_unsafe"),
    ]:
        assert pattern.match(canonical_name(server, tool))


# ── parse_namespaced ──────────────────────────────────────────────────────


def test_parse_roundtrip_short_name():
    assert parse_namespaced("mcp__fs__read_file") == ("fs", "read_file")


def test_parse_returns_none_for_non_mcp():
    assert parse_namespaced("read_file") is None
    assert parse_namespaced("__fs__read_file") is None
    # Wrong server-name format (uppercase).
    assert parse_namespaced("mcp__FS__read_file") is None


def test_parse_handles_underscore_in_tool():
    # Tools naturally contain `_`. The pattern uses `(.+)` for the tool
    # part so they pass through unchanged (modulo earlier sanitization).
    assert parse_namespaced("mcp__fs__list_dir_recursive") == (
        "fs",
        "list_dir_recursive",
    )


def test_parse_does_not_invert_clamped_names():
    """Clamped names have a hash suffix that isn't part of the original
    tool name; parse_namespaced is documented as the inverse only for the
    unclamped path."""
    long_name = canonical_name("fs", "a" * 70)
    parsed = parse_namespaced(long_name)
    assert parsed is not None
    server, tool = parsed
    assert server == "fs"
    assert tool != "a" * 70  # exact roundtrip not promised
