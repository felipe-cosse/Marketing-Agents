"""DEL-07: malformed/ambiguous/unsafe repository text fails without fetching links."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from scripts.verify_repository_text import TextPolicyError, check_external_url, check_text, verify


@pytest.mark.parametrize(
    "name,text",
    [
        ("fixture.json", '{"a": 1, "a": 2}\n'),
        ("fixture.json", '{"a": NaN}\n'),
        ("fixture.json", "{bad json}\n"),
        ("fixture.yaml", "a: 1\na: 2\n"),
        ("fixture.yaml", "a: !!python/object:unexpected {}\n"),
        ("fixture.md", "# No newline"),
        ("fixture.md", "# Tabs\tforbidden\n"),
        ("fixture.md", "# CRLF\r\n"),
        ("fixture.json", "{} \n"),
        ("fixture.md", "# Missing\n\n[local](missing.md)\n"),
        ("fixture.md", "# Missing anchor\n\n[local](fixture.md#unknown)\n"),
        ("fixture.md", "# Escape\n\n[local](../outside.md)\n"),
        ("fixture.md", "# Unsafe\n\n[external](https://example.invalid/path)\n"),
        ("fixture.md", "# Unsafe\n\n[link]: https://example.invalid/path\n"),
        ("fixture.md", "# Unsafe\n\n<https://example.invalid/path>\n"),
    ],
)
def test_del_07_bad_repository_text_fails(tmp_path, name, text):
    (tmp_path / name).write_bytes(text.encode())
    with pytest.raises((ValueError, TypeError, yaml.YAMLError)):
        check_text(tmp_path, name)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@github.com/path",
        "https://github.com:invalid/path",
        "https://github.com:0/path",
        "http://github.com/path",
        "https:///missing-host",
        "https://github.com.evil.invalid/path",
        "https://github.com/white space",
        "https://github.com:8443/path",
        "file:///tmp/private",
        "mailto:a@example.invalid",
    ],
)
def test_del_07_external_urls_are_syntax_and_allowlist_checked(url):
    with pytest.raises(TextPolicyError):
        check_external_url(url)


def test_del_07_valid_text_links_and_loopback_are_offline(tmp_path, monkeypatch):
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("repository link check must not fetch URLs")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    (tmp_path / "fixture.md").write_text(
        "# Title\n\nA hard break  \nnext line.\n"
        "[local](fixture.md#title) [external](https://github.com/example/repo)\n"
        "[reference][doc]\n\n[doc]: fixture.md#title\n"
        "<https://docs.sqlalchemy.org/en/20/>\n"
    )
    check_text(tmp_path, "fixture.md")
    check_external_url("http://127.0.0.1:8000/docs")
    check_external_url("http://[::1]:8000/docs")


def test_del_07_inventory_includes_untracked_files_and_preserves_original_sources(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    with pytest.raises(TextPolicyError, match="empty_text_inventory"):
        verify(tmp_path)
    (tmp_path / "good.json").write_text("{}\n")
    source = tmp_path / "references"
    source.mkdir()
    (source / "original.md").write_text("Original source has no normalized newline")
    assert verify(tmp_path) == 1
    (tmp_path / "bad.json").write_text("{sensitive-content-not-to-echo}\n")
    with pytest.raises(TextPolicyError, match=r"bad\.json:JSONDecodeError") as caught:
        verify(tmp_path)
    assert "sensitive-content" not in str(caught.value)


def test_del_07_symlink_escape_fails(tmp_path):
    (tmp_path / "source.json").write_text("{}\n")
    (tmp_path / "link.json").symlink_to(tmp_path / "source.json")
    with pytest.raises(TextPolicyError, match="unsupported_symlink"):
        check_text(tmp_path, "link.json")


def test_del_07_compose_yaml_merge_allows_explicit_override_not_duplicate(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text("defaults: &base\n  retries: 1\nservice:\n  <<: *base\n  retries: 2\n")
    check_text(tmp_path, "fixture.yaml")
    path.write_text(path.read_text() + "  retries: 3\n")
    with pytest.raises(TextPolicyError, match="duplicate_key"):
        check_text(tmp_path, "fixture.yaml")
