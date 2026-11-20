"""Client configuration and the validation rules @sanity/client applies."""

from __future__ import annotations

import datetime
import re
import warnings
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

__all__ = [
    "ClientConfig",
    "Perspective",
    "SanityConfigWarning",
    "validate_dataset",
    "validate_document_id",
    "validate_perspective",
    "validate_project_id",
    "validate_request_tag",
]

DEFAULT_API_HOST = "https://api.sanity.io"
DEFAULT_CDN_HOST = "apicdn.sanity.io"
DEFAULT_TIMEOUT = 300.0

Perspective = Union[str, Sequence[str]]
"""``"published"``, ``"drafts"``, ``"raw"``, a release ID, or a stack of them."""

_DATASET_RE = re.compile(r"^(~[a-z0-9][-\w]{0,63}|[a-z0-9][-\w]{0,63})$")
_PROJECT_ID_RE = re.compile(r"^[-a-z0-9]+$", re.IGNORECASE)
_TAG_RE = re.compile(r"^[a-z0-9._-]{1,75}$", re.IGNORECASE)
_DOCUMENT_ID_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]{0,127}$", re.IGNORECASE)
_API_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SanityConfigWarning(UserWarning):
    """A configuration that works but is probably not what you meant."""


def validate_dataset(name: str) -> str:
    """Raise ``ValueError`` unless ``name`` is a valid dataset name."""
    if not isinstance(name, str) or not _DATASET_RE.match(name):
        raise ValueError(
            "Datasets can only contain lowercase characters, numbers, underscores and dashes, "
            "and start with tilde, and be maximum 64 characters"
        )
    return name


def validate_project_id(project_id: str) -> str:
    """Raise ``ValueError`` unless ``project_id`` is a valid project ID."""
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.match(project_id):
        raise ValueError("`project_id` can only contain only a-z, 0-9 and dashes")
    return project_id


def validate_request_tag(tag: str) -> str:
    """Raise ``ValueError`` unless ``tag`` is a valid request tag."""
    if not isinstance(tag, str) or not _TAG_RE.match(tag):
        raise ValueError(
            "Tag can only contain alphanumeric characters, underscores, dashes and dots, "
            "and be between one and 75 characters long."
        )
    return tag


def validate_document_id(operation: str, document_id: Any) -> str:
    """Raise ``ValueError`` unless ``document_id`` is a valid document ID."""
    if (
        not isinstance(document_id, str)
        or not _DOCUMENT_ID_RE.match(document_id)
        or ".." in document_id
    ):
        raise ValueError(f'{operation}(): "{document_id}" is not a valid document ID')
    return document_id


def validate_perspective(perspective: Any) -> Union[str, Tuple[str, ...]]:
    """Normalize a perspective and reject ``raw`` inside a stack."""
    if isinstance(perspective, str):
        return perspective
    if isinstance(perspective, Sequence):
        stack = tuple(perspective)
        if not all(isinstance(item, str) for item in stack):
            raise TypeError("perspective stack must contain strings")
        if len(stack) > 1 and "raw" in stack:
            raise ValueError(
                'Invalid API perspective value: "raw". The raw-perspective can not be '
                "combined with other perspectives"
            )
        return stack
    raise TypeError("perspective must be a string or a sequence of strings")


def _normalize_api_version(api_version: Union[str, datetime.date]) -> str:
    if isinstance(api_version, datetime.date):
        api_version = api_version.isoformat()
    version = str(api_version)
    if version.startswith("v"):
        version = version[1:]
    if version in ("1", "X"):
        return version
    valid = bool(_API_VERSION_RE.match(version))
    if valid:
        try:
            datetime.date.fromisoformat(version)
        except ValueError:
            valid = False
    if not valid:
        raise ValueError("Invalid API version string, expected `1` or date in format `YYYY-MM-DD`")
    return version


@dataclass(frozen=True)
class ClientConfig:
    """Resolved client settings.

    Built by :class:`~sanity_client.SanityClient`, so you rarely create one
    directly. ``url`` and ``cdn_url`` are derived the same way as in the JS
    client: ``https://<projectId>.api.sanity.io/v<apiVersion>`` and the
    matching ``apicdn`` host.
    """

    project_id: Optional[str] = None
    dataset: Optional[str] = None
    api_version: str = "1"
    token: Optional[str] = field(default=None, repr=False)
    use_cdn: bool = True
    perspective: Optional[Union[str, Tuple[str, ...]]] = None
    api_host: str = DEFAULT_API_HOST
    use_project_hostname: bool = True
    request_tag_prefix: Optional[str] = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: Optional[float] = DEFAULT_TIMEOUT
    result_source_map: Union[bool, str, None] = None
    stack_id: Optional[str] = None
    organization_id: Optional[str] = None
    url: str = field(init=False, default="")
    cdn_url: str = field(init=False, default="")

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if self.use_project_hostname and not self.project_id:
            raise ValueError("Configuration must contain `project_id`")
        if self.use_project_hostname and self.project_id:
            validate_project_id(self.project_id)
        if self.dataset:
            validate_dataset(self.dataset)
        if self.perspective is not None:
            set_(self, "perspective", validate_perspective(self.perspective))
        prefix = self.request_tag_prefix
        if prefix:
            set_(self, "request_tag_prefix", validate_request_tag(prefix).rstrip("."))
        else:
            set_(self, "request_tag_prefix", None)
        set_(self, "api_version", _normalize_api_version(self.api_version))
        set_(self, "headers", dict(self.headers))
        if "://" not in self.api_host:
            raise ValueError("`api_host` must include the protocol, for example https://api.sanity.io")
        protocol, host = self.api_host.split("://", 1)
        cdn_host = DEFAULT_CDN_HOST if self.is_default_api else host
        if self.use_project_hostname:
            set_(self, "url", f"{protocol}://{self.project_id}.{host}/v{self.api_version}")
            set_(self, "cdn_url", f"{protocol}://{self.project_id}.{cdn_host}/v{self.api_version}")
        else:
            set_(self, "url", f"{self.api_host}/v{self.api_version}")
            set_(self, "cdn_url", self.url)

    @property
    def is_default_api(self) -> bool:
        """Whether ``api_host`` is Sanity's own API host."""
        return self.api_host == DEFAULT_API_HOST

    @classmethod
    def create(
        cls,
        *,
        project_id: Optional[str] = None,
        dataset: Optional[str] = None,
        api_version: Union[str, datetime.date, None] = None,
        token: Optional[str] = None,
        use_cdn: bool = True,
        perspective: Optional[Perspective] = None,
        api_host: str = DEFAULT_API_HOST,
        use_project_hostname: bool = True,
        request_tag_prefix: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = DEFAULT_TIMEOUT,
        result_source_map: Union[bool, str, None] = None,
        stack_id: Optional[str] = None,
        organization_id: Optional[str] = None,
    ) -> "ClientConfig":
        """Build a config, warning when no API version is pinned."""
        if api_version is None:
            warnings.warn(
                "Using the Sanity client without specifying an API version is deprecated. "
                "Pass api_version, for example api_version='2025-02-19'.",
                SanityConfigWarning,
                stacklevel=3,
            )
            api_version = "1"
        if isinstance(api_version, datetime.date):
            api_version = api_version.isoformat()
        return cls(
            project_id=project_id,
            dataset=dataset,
            api_version=api_version,
            token=token,
            use_cdn=use_cdn,
            perspective=perspective,  # type: ignore[arg-type]
            api_host=api_host,
            use_project_hostname=use_project_hostname,
            request_tag_prefix=request_tag_prefix,
            headers=dict(headers or {}),
            timeout=timeout,
            result_source_map=result_source_map,
            stack_id=stack_id,
            organization_id=organization_id,
        )

    def merge(self, **changes: Any) -> "ClientConfig":
        """A copy with ``changes`` applied and validated again."""
        if "api_version" in changes and isinstance(changes["api_version"], datetime.date):
            changes["api_version"] = changes["api_version"].isoformat()
        if "headers" in changes and changes["headers"] is None:
            changes["headers"] = {}
        unknown = set(changes) - _CONFIG_FIELDS
        if unknown:
            raise TypeError(f"unknown config option(s): {', '.join(sorted(unknown))}")
        return replace(self, **changes)

    def require_dataset(self) -> str:
        """The dataset, or ``ValueError`` when none is configured."""
        if not self.dataset:
            raise ValueError("`dataset` must be provided to perform queries")
        return self.dataset


_CONFIG_FIELDS = frozenset(
    {
        "project_id",
        "dataset",
        "api_version",
        "token",
        "use_cdn",
        "perspective",
        "api_host",
        "use_project_hostname",
        "request_tag_prefix",
        "headers",
        "timeout",
        "result_source_map",
        "stack_id",
        "organization_id",
    }
)
