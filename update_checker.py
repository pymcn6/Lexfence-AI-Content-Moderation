# -*- coding: utf-8 -*-
"""GitHub Release 更新检测。

- 通过 GitHub API 获取最新 Release，比较版本号判断是否有更新。
- 支持用户自定义代理前缀（加速 GitHub 访问，如 https://ghproxy.com/）。
- 返回最新版本、当前版本、是否有更新、更新日志（Release body）。
- 结果带短缓存，避免频繁请求 GitHub API 触发限流。
"""

import re
import time

import requests

import config
import settings_store


_cache = {"ts": 0.0, "data": None}
_CACHE_TTL = 3600  # 秒


def _proxy_prefix() -> str:
    """用户自定义的 GitHub 代理前缀（设置项 github_proxy）。"""
    p = (settings_store.get_setting("github_proxy", "") or "").strip()
    if p and not p.endswith("/"):
        p += "/"
    return p


def _parse_version(tag: str):
    """把 'v1.2.3' 解析成 (1,2,3) 便于比较；无法解析返回 (0,)。"""
    if not tag:
        return (0,)
    nums = re.findall(r"\d+", tag)
    return tuple(int(n) for n in nums) if nums else (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def check(force: bool = False) -> dict:
    """检测更新，返回结构化结果（带缓存）。"""
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["data"]

    current = config.APP_VERSION
    repo = config.GITHUB_REPO
    proxy = _proxy_prefix()
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    url = proxy + api if proxy else api

    result = {
        "current": current,
        "latest": current,
        "has_update": False,
        "changelog": "",
        "release_url": config.GITHUB_URL + "/releases",
        "error": "",
        "checked_at": int(now),
    }
    try:
        r = requests.get(url, timeout=12,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
        latest = (data.get("tag_name") or data.get("name") or "").strip()
        result["latest"] = latest or current
        result["changelog"] = (data.get("body") or "").strip()
        result["release_url"] = data.get("html_url") or result["release_url"]
        result["has_update"] = _is_newer(latest, current)
    except Exception as exc:
        result["error"] = str(exc)[:200]

    _cache["data"] = result
    _cache["ts"] = now
    return result
