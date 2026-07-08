from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import tweepy

logger = logging.getLogger(__name__)

TWEET_FIELDS = ["created_at", "public_metrics", "author_id"]
USER_FIELDS = ["username"]
EXPANSIONS = ["author_id"]


def _build_client(bearer_token: str) -> tweepy.Client:
    return tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)


def _window_start(window_hours: float) -> datetime:
    """回傳「現在往前 window_hours 小時」的 UTC 時間，作為抓取起點。"""
    return datetime.now(timezone.utc) - timedelta(hours=window_hours)


def _simplify_tweets(tweets, users_by_id: dict, source: str) -> list[dict]:
    if not tweets:
        return []
    simplified = []
    for tweet in tweets:
        author = users_by_id.get(tweet.author_id)
        username = author.username if author else "unknown"
        metrics = tweet.public_metrics or {}
        simplified.append(
            {
                "id": str(tweet.id),
                "author": username,
                "text": tweet.text,
                "created_at": str(tweet.created_at) if tweet.created_at else "",
                "url": f"https://x.com/{username}/status/{tweet.id}",
                "metrics": metrics,
                "source": source,
            }
        )
    return simplified


def get_user_tweets(
    client: tweepy.Client,
    username: str,
    max_results: int = 10,
    window_hours: float = 1,
) -> list[dict]:
    user_resp = client.get_user(username=username)
    if not user_resp.data:
        logger.warning("找不到帳號 @%s，略過。", username)
        return []
    user_id = user_resp.data.id

    resp = client.get_users_tweets(
        id=user_id,
        max_results=max(5, min(max_results, 100)),
        tweet_fields=TWEET_FIELDS,
        exclude=["retweets", "replies"],
        start_time=_window_start(window_hours),
    )
    users_by_id = {user_resp.data.id: user_resp.data}
    tweets = (resp.data or [])[:max_results]
    return _simplify_tweets(tweets, users_by_id, source=f"account:{username}")


def search_recent(
    client: tweepy.Client,
    query: str,
    max_results: int = 10,
    window_hours: float = 1,
) -> list[dict]:
    resp = client.search_recent_tweets(
        query=query,
        max_results=max(10, min(max_results, 100)),
        tweet_fields=TWEET_FIELDS,
        user_fields=USER_FIELDS,
        expansions=EXPANSIONS,
        start_time=_window_start(window_hours),
    )
    users_by_id = {}
    if resp.includes and "users" in resp.includes:
        users_by_id = {u.id: u for u in resp.includes["users"]}
    tweets = (resp.data or [])[:max_results]
    return _simplify_tweets(tweets, users_by_id, source=f"keyword:{query}")
