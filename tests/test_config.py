from __future__ import annotations

import datetime
import warnings

import pytest

from sanity_client import ClientConfig, SanityClient, SanityConfigWarning
from sanity_client.config import (
    validate_dataset,
    validate_document_id,
    validate_perspective,
    validate_project_id,
    validate_request_tag,
)


def test_urls_follow_the_js_client() -> None:
    cfg = ClientConfig.create(project_id="abc123", dataset="production", api_version="2025-02-19")
    assert cfg.url == "https://abc123.api.sanity.io/v2025-02-19"
    assert cfg.cdn_url == "https://abc123.apicdn.sanity.io/v2025-02-19"


def test_custom_api_host_has_no_separate_cdn() -> None:
    cfg = ClientConfig.create(project_id="p", dataset="d", api_version="1", api_host="http://localhost:3333")
    assert cfg.url == "http://p.localhost:3333/v1"
    assert cfg.cdn_url == cfg.url


def test_global_api_without_project_hostname() -> None:
    cfg = ClientConfig.create(project_id="p", api_version="1", use_project_hostname=False)
    assert cfg.url == "https://api.sanity.io/v1"


def test_missing_api_version_warns_and_uses_v1() -> None:
    with pytest.warns(SanityConfigWarning):
        cfg = ClientConfig.create(project_id="p", dataset="d")
    assert cfg.api_version == "1"


@pytest.mark.parametrize(
    ("given", "expected"),
    [("v2021-06-07", "2021-06-07"), ("X", "X"), ("1", "1"), (datetime.date(2025, 2, 19), "2025-02-19")],
)
def test_api_version_forms(given: object, expected: str) -> None:
    cfg = ClientConfig.create(project_id="p", api_version=given)  # type: ignore[arg-type]
    assert cfg.api_version == expected


@pytest.mark.parametrize("bad", ["2021-13-01", "2021-6-7", "latest", "v"])
def test_bad_api_versions_raise(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid API version"):
        ClientConfig.create(project_id="p", api_version=bad)


def test_project_id_required_with_project_hostname() -> None:
    with pytest.raises(ValueError, match="project_id"):
        ClientConfig.create(api_version="1")


def test_tag_prefix_trailing_dot_is_dropped() -> None:
    cfg = ClientConfig.create(project_id="p", api_version="1", request_tag_prefix="web.")
    assert cfg.request_tag_prefix == "web"


def test_token_is_hidden_from_repr() -> None:
    cfg = ClientConfig.create(project_id="p", api_version="1", token="secret-value")
    assert "secret-value" not in repr(cfg)
    client = SanityClient(config=cfg)
    assert "secret-value" not in repr(client)
    client.close()


def test_merge_validates_again() -> None:
    cfg = ClientConfig.create(project_id="p", dataset="d", api_version="1")
    assert cfg.merge(dataset="other").dataset == "other"
    with pytest.raises(ValueError):
        cfg.merge(dataset="Bad Name")
    with pytest.raises(TypeError, match="unknown config"):
        cfg.merge(colour="blue")


def test_merge_accepts_dates() -> None:
    cfg = ClientConfig.create(project_id="p", api_version="1").merge(api_version=datetime.date(2024, 1, 2))
    assert cfg.url.endswith("/v2024-01-02")


def test_require_dataset() -> None:
    with pytest.raises(ValueError, match="dataset"):
        ClientConfig.create(project_id="p", api_version="1").require_dataset()


@pytest.mark.parametrize("name", ["production", "~private", "a", "a-b_c", "x" * 64])
def test_valid_datasets(name: str) -> None:
    assert validate_dataset(name) == name


@pytest.mark.parametrize("name", ["Production", "-a", "a b", "x" * 65, ""])
def test_invalid_datasets(name: str) -> None:
    with pytest.raises(ValueError):
        validate_dataset(name)


def test_project_id_rules() -> None:
    assert validate_project_id("abc-123") == "abc-123"
    with pytest.raises(ValueError):
        validate_project_id("abc_123")


def test_tag_rules() -> None:
    assert validate_request_tag("a.b-c_d") == "a.b-c_d"
    for bad in ["", "a b", "x" * 76, "ø"]:
        with pytest.raises(ValueError):
            validate_request_tag(bad)


def test_document_id_rules() -> None:
    assert validate_document_id("op", "drafts.abc") == "drafts.abc"
    for bad in ["", "-a", "a..b", "a/b", "x" * 129]:
        with pytest.raises(ValueError, match="not a valid document ID"):
            validate_document_id("op", bad)


def test_perspective_rules() -> None:
    assert validate_perspective("published") == "published"
    assert validate_perspective(["rA", "drafts"]) == ("rA", "drafts")
    with pytest.raises(ValueError, match="raw"):
        validate_perspective(["raw", "drafts"])
    with pytest.raises(TypeError):
        validate_perspective(3)


def test_no_warning_when_api_version_given() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ClientConfig.create(project_id="p", api_version="2025-02-19")
