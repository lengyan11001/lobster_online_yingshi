"""YouTube Analytics：视频列表、视频统计、频道分析。

使用 YouTube Data API v3 和 YouTube Analytics API v2。
需要 OAuth scope: youtube.readonly + yt-analytics.readonly。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .youtube_api_upload import build_httpx_proxy_url

logger = logging.getLogger(__name__)

YT_DATA_BASE = "https://www.googleapis.com/youtube/v3"
YT_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"


async def _refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str,
    proxy_url: Optional[str] = None,
) -> str:
    kwargs: Dict[str, Any] = {"timeout": 30.0}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**kwargs) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(f"刷新 YouTube token 失败: {r.text[:500]}")
    data = r.json()
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"刷新 YouTube token 未返回 access_token: {data}")
    return token


async def _yt_get(
    url: str, access_token: str, params: Dict[str, str],
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    kwargs: Dict[str, Any] = {"timeout": 30.0}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**kwargs) as client:
        r = await client.get(url, params=params, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube API 错误 ({r.status_code}): {r.text[:500]}")
    return r.json()


async def get_channel_uploads_playlist(
    access_token: str, proxy_url: Optional[str] = None,
) -> str:
    data = await _yt_get(
        f"{YT_DATA_BASE}/channels", access_token,
        {"part": "contentDetails", "mine": "true"},
        proxy_url,
    )
    items = data.get("items", [])
    if not items:
        raise RuntimeError("未找到 YouTube 频道（可能需要重新授权）")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


async def get_playlist_videos(
    playlist_id: str, access_token: str, max_results: int = 200,
    proxy_url: Optional[str] = None,
) -> List[str]:
    video_ids: List[str] = []
    page_token: Optional[str] = None
    per_page = min(max_results, 50)
    while len(video_ids) < max_results:
        params: Dict[str, str] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": str(per_page),
        }
        if page_token:
            params["pageToken"] = page_token
        data = await _yt_get(
            f"{YT_DATA_BASE}/playlistItems", access_token, params, proxy_url,
        )
        for item in data.get("items", []):
            vid = (item.get("contentDetails") or {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return video_ids[:max_results]


async def get_videos_stats(
    video_ids: List[str], access_token: str,
    proxy_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not video_ids:
        return []
    chunk_size = 50
    results: List[Dict[str, Any]] = []
    for i in range(0, len(video_ids), chunk_size):
        chunk = video_ids[i : i + chunk_size]
        data = await _yt_get(
            f"{YT_DATA_BASE}/videos", access_token,
            {"part": "snippet,statistics", "id": ",".join(chunk)},
            proxy_url,
        )
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            results.append({
                "id": item.get("id"),
                "title": snippet.get("title", ""),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnail": (snippet.get("thumbnails") or {}).get("default", {}).get("url", ""),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "favorites": int(stats.get("favoriteCount", 0)),
            })
    return results


async def get_channel_analytics(
    access_token: str, days: int = 28,
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    """从 YouTube Analytics API 获取频道级别的汇总数据。"""
    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        data = await _yt_get(
            f"{YT_ANALYTICS_BASE}/reports", access_token,
            {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": "views,likes,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost",
            },
            proxy_url,
        )
        headers = [h.get("name", "") for h in data.get("columnHeaders", [])]
        rows = data.get("rows", [])
        if rows:
            row = rows[0]
            return {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
        return {}
    except RuntimeError as e:
        if "403" in str(e) or "Forbidden" in str(e):
            logger.warning("[yt-analytics] Analytics API 403 -- 可能需要 yt-analytics.readonly scope")
            return {"error": "YouTube Analytics API 权限不足，请重新授权以获取分析数据"}
        raise


async def sync_youtube_account_data(
    account_data: Dict[str, Any],
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    """综合同步：视频列表 + 统计 + 频道分析。account_data 来自 youtube_accounts JSON 文件的一条账号记录。"""
    client_id = (account_data.get("oauth_client_id") or "").strip()
    client_secret = (account_data.get("oauth_client_secret") or "").strip()
    refresh_token = (account_data.get("refresh_token") or "").strip()

    if not refresh_token:
        return {"error": "账号未授权（无 refresh_token）"}

    access_token = await _refresh_access_token(client_id, client_secret, refresh_token, proxy_url)

    uploads_playlist = await get_channel_uploads_playlist(access_token, proxy_url)
    video_ids = await get_playlist_videos(uploads_playlist, access_token, proxy_url=proxy_url)
    videos = await get_videos_stats(video_ids, access_token, proxy_url)
    analytics = await get_channel_analytics(access_token, proxy_url=proxy_url)

    return {
        "videos": videos,
        "channel_analytics": analytics,
        "video_count": len(videos),
    }
