"""Tests for the Reddit connector: graceful disable, no-network injection."""
from __future__ import annotations

import os
from unittest import mock

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_intelligence.reddit_connector import (
    RedditCredentials,
    fetch_subreddit_posts,
)


def test_missing_credentials_does_not_crash():
    with mock.patch.dict(os.environ, {}, clear=True):
        res = fetch_subreddit_posts(["stocks"])
    assert res.status == SourceStatus.NO_CREDENTIALS
    assert res.posts == []
    assert any("credentials" in w for w in res.warnings)


def test_injected_fetch_parses_minimal_fields():
    creds = RedditCredentials("id", "secret", "ua")
    sample = {
        "data": {"children": [
            {"data": {
                "id": "abc", "subreddit": "wallstreetbets",
                "created_utc": 1700000000.0, "title": "$NVDA thesis",
                "selftext": "DCF valuation", "link_flair_text": "DD",
                "score": 42, "num_comments": 7, "upvote_ratio": 0.95,
                "permalink": "/r/wsb/abc", "author": "someuser",
            }},
        ]},
    }
    res = fetch_subreddit_posts(
        ["wallstreetbets"],
        credentials=creds,
        oauth_token_fn=lambda c: "tok",
        http_get=lambda url, headers, params: sample,
    )
    assert res.status == SourceStatus.OK
    assert len(res.posts) == 1
    p = res.posts[0]
    assert p.post_id == "abc"
    assert p.community == "wallstreetbets"
    assert p.flair == "DD"
    # Author handle must be hashed, never raw.
    assert p.author_hash.startswith("rh_")
    assert "someuser" not in p.author_hash


def test_rate_limit_maps_to_status():
    creds = RedditCredentials("id", "secret", "ua")
    from portfolio_automation.social_intelligence.reddit_connector import _RateLimited

    def _boom(c):
        raise _RateLimited()

    res = fetch_subreddit_posts(["stocks"], credentials=creds,
                                oauth_token_fn=_boom, http_get=lambda **k: {})
    assert res.status == SourceStatus.RATE_LIMITED


def test_fetch_error_does_not_raise():
    creds = RedditCredentials("id", "secret", "ua")

    def _explode(url, headers, params):
        raise RuntimeError("network down")

    res = fetch_subreddit_posts(["stocks"], credentials=creds,
                                oauth_token_fn=lambda c: "tok", http_get=_explode)
    # One subreddit, fetch error → no posts but status degraded, not a crash.
    assert res.status in (SourceStatus.DEGRADED, SourceStatus.OK)
    assert any("fetch_error" in w for w in res.warnings)
