"""Where model downloads come from: Hugging Face, a mirror of it, or ModelScope, and through which proxy.

Settings live in engine.json under "downloads". The catalog's sha256 still checks every file, so a
wrong mirror fails with an error, never with a bad model in place.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError, field_validator

from app.errors import ApiError
from app.model_folders import get_setting

HF = "https://huggingface.co/"
HF_RESOLVE = re.compile(r"^https://huggingface\.co/([^/]+)/([^/]+)/resolve/[^/]+/(.+)$")


class DownloadSettings(BaseModel):
    source: Literal["huggingface", "modelscope"] = "huggingface"
    hf_endpoint: str | None = None  # a Hugging Face mirror, e.g. https://hf-mirror.com
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None  # comma-separated host names that skip the proxy
    ca_bundle: str | None = None  # a PEM file, trusted on top of the usual certificates

    @field_validator("hf_endpoint", "http_proxy", "https_proxy", "no_proxy", "ca_bundle", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("hf_endpoint", "http_proxy", "https_proxy")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("needs a full http:// or https:// address")
        return v.rstrip("/")

    @field_validator("ca_bundle")
    @classmethod
    def _file(cls, v: str | None) -> str | None:
        if v is None:
            return None
        path = Path(v).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"there's no file at {v}")
        return str(path)


DEFAULT = DownloadSettings()


def read(engine_file: Path | None) -> DownloadSettings:
    """The saved settings; a broken value (a CA file since deleted) is an error, not a quiet fallback."""
    raw = get_setting(engine_file, "downloads", None)
    if not isinstance(raw, dict):
        return DEFAULT
    try:
        return DownloadSettings.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ApiError("bad_params", f"Download settings: {where} {first.get('msg', 'is invalid')}. Fix it in Settings.") from exc


def file_url(url: str, s: DownloadSettings) -> str:
    """A catalog URL on the chosen source. Only Hugging Face URLs move; others stay as they are."""
    if not url.startswith(HF):
        return url
    if s.source == "modelscope" and (m := HF_RESOLVE.match(url)):
        # ModelScope copies of HF repos keep the org/repo name; its default branch is master.
        return f"https://modelscope.cn/models/{m[1]}/{m[2]}/resolve/master/{m[3]}"
    if s.hf_endpoint:
        return f"{s.hf_endpoint}/{url[len(HF):]}"
    return url


def hint(s: DownloadSettings) -> str:
    """Added to a failed download's error when the user changed where downloads come from."""
    return "" if s == DEFAULT else " Check the download settings in Settings."


def client(s: DownloadSettings) -> httpx.AsyncClient | None:
    """A client with the proxies and CA file, or None to use the host's usual one."""
    if not (s.http_proxy or s.https_proxy or s.ca_bundle):
        return None
    verify = httpx.create_ssl_context()
    if s.ca_bundle:
        verify.load_verify_locations(cafile=s.ca_bundle)
    hosts = [h.strip() for h in (s.no_proxy or "").split(",") if h.strip()]
    mounts: dict[str, httpx.AsyncHTTPTransport | None] = {}
    for scheme, proxy in [("http://", s.http_proxy), ("https://", s.https_proxy)]:
        if proxy and "*" not in hosts:
            mounts[scheme] = httpx.AsyncHTTPTransport(proxy=proxy, verify=verify)
    for host in hosts:
        # The patterns httpx makes from NO_PROXY; None means no proxy, the client's own transport.
        mounts[host if "://" in host else f"all://*{host.lstrip('*')}"] = None
    return httpx.AsyncClient(verify=verify, mounts=mounts, timeout=httpx.Timeout(60.0, connect=10.0))
