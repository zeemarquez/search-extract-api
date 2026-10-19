"""Schemas for ``/feeds`` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import Result, SourceStatus


class FeedInfo(BaseModel):
    url: str
    title: str | None = None
    link: str | None = None
    description: str | None = None
    language: str | None = None
    updated_at: datetime | None = None
    format: str | None = Field(default=None, description="rss20, atom10, jsonfeed...")
    item_count: int = 0


class FeedResponse(BaseModel):
    feed: FeedInfo
    results: list[Result]
    count: int
    cached: bool = False


class FeedsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(min_length=1, max_length=100)
    limit_per_feed: int = Field(default=50, ge=1, le=500)
    limit: int = Field(default=200, ge=1, le=2000)
    since: datetime | None = None
    session_id: str | None = None
    no_cache: bool = False


class FeedsResponse(BaseModel):
    feeds: list[FeedInfo]
    results: list[Result]
    count: int
    sources: list[SourceStatus]
    failed_sources: list[str] = Field(default_factory=list)
    partial: bool = False
    took_ms: int = 0


class DiscoveredFeed(BaseModel):
    url: str
    title: str | None = None
    type: str | None = None
    found_by: Literal["self", "link", "anchor", "guess"]
    verified: bool | None = None
    item_count: int | None = None


class DiscoverResponse(BaseModel):
    url: str
    feeds: list[DiscoveredFeed]
