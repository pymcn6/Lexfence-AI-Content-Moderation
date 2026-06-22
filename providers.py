# -*- coding: utf-8 -*-
"""AI 服务商适配层。

统一封装 OpenAI / Claude / Gemini / OpenAI 兼容服务的两类能力：
- list_models(): 拉取该渠道可用模型列表（用于「一键获取模型」）。
- chat(): 发送一次分类对话，返回 {text, usage_tokens}。

所有网络请求使用 requests；不在此层做业务判定，仅做协议适配。
"""

from typing import Dict, List, Optional

import requests

DEFAULT_TIMEOUT = 120  # 大兜底连接超时（秒）——保留以防连接层极端卡死


def is_safe_public_url(url: str) -> bool:
    """SSRF 基础校验：仅允许 http(s)，拒绝指向私网/环回/链路本地的地址。

    用于校验管理员可填写的外呼地址（渠道 base_url / models_endpoint /
    GitHub 代理前缀）。空字符串视为合法（表示用默认值）。
    注意：这是基于主机名/字面 IP 的轻量防护，不防 DNS 重绑定。
    """
    import ipaddress
    from urllib.parse import urlparse

    if not url or not url.strip():
        return True
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    low = host.lower()
    if low == "localhost" or low.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    except ValueError:
        pass  # 主机名（非字面 IP）：交由网络层解析，此处仅做协议/字面 IP 拦截
    return True


# ---------- 默认 base_url ----------
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "",
    "claude": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def normalize_base_url(provider: str, base_url: Optional[str]) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URLS.get(provider, "").rstrip("/")
    return url


# ---------- 模型列表拉取 ----------
def list_models(provider: str, base_url: str, api_key: str,
                timeout: int = 30, models_endpoint: str = "") -> List[str]:
    """拉取渠道可用模型。

    策略：无论选哪种 provider，都先按所选格式尝试，失败再回退尝试其它格式。
    - 候选请求端点：自定义端点（若填） + 该 provider 默认端点 + 其它常见端点。
    - 响应解析使用通用解析器，兼容 OpenAI({data:[{id}]})、AIHUBMIX
      ({data:[{model_id}]})、Claude、Gemini({models:[{name}]})、纯数组等结构。
    任一候选成功返回非空列表即采用；全部失败则抛出最后一个异常。
    """
    provider = (provider or "openai").lower()
    base = normalize_base_url(provider, base_url)
    endpoint = (models_endpoint or "").strip()

    # 构造候选（请求方式, URL）列表，按优先级排序
    candidates = []

    def _abs(ep):
        if ep.startswith("http://") or ep.startswith("https://"):
            return ep
        return base.rstrip("/") + "/" + ep.lstrip("/") if base else ep

    # 1) 用户自定义端点最优先
    if endpoint:
        candidates.append(("auto", _abs(endpoint)))

    # 2) 当前 provider 的默认端点
    provider_default = {
        "openai": ("openai", f"{base}/models"),
        "openai_compatible": ("openai", f"{base}/models"),
        "claude": ("claude", f"{base}/models"),
        "gemini": ("gemini", f"{base}/models"),
    }.get(provider)
    if provider_default and base:
        candidates.append(provider_default)

    # 3) 其它常见端点回退（OpenAI 风格 /models 与 /v1/models）
    if base:
        for url in (f"{base}/models", f"{base}/v1/models"):
            if not any(c[1] == url for c in candidates):
                candidates.append(("auto", url))

    last_err = None
    for mode, url in candidates:
        try:
            body = _fetch_models_raw(mode if mode != "auto" else provider, url, api_key, timeout)
            names = _parse_models(body)
            if names:
                return names
        except Exception as exc:  # 试下一个候选
            last_err = exc
    if last_err:
        raise last_err
    return []


def _auth_headers(provider: str, api_key: str) -> dict:
    if provider == "claude":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _fetch_models_raw(provider: str, url: str, api_key: str, timeout: int):
    """发起请求并返回 JSON 体。Gemini 用 query key，其余用各自鉴权头。"""
    headers = _auth_headers(provider, api_key)
    params = {"key": api_key} if provider == "gemini" else None
    # 即使 provider 不是 gemini，也带上 Authorization；若该 URL 是 gemini 风格则用 params
    if "generativelanguage.googleapis.com" in url and not params:
        params = {"key": api_key}
    r = requests.get(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _parse_models(body) -> List[str]:
    """通用模型列表解析器：兼容多种返回结构。

    支持：
    - 纯数组：["gpt-4o", ...] 或 [{"id"/"model_id"/"name": ...}, ...]
    - {"data": [...]}（OpenAI / AIHUBMIX，元素含 id 或 model_id）
    - {"models": [...]}（Gemini，元素含 name，可能带 "models/" 前缀）
    - {"result"/"list": [...]} 等常见包装
    """
    def _name_of(item):
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            name = (item.get("id") or item.get("model_id") or item.get("name")
                    or item.get("model") or "")
            name = str(name).strip()
            # 去掉 gemini 的 "models/" 前缀
            return name.split("/")[-1] if "/" in name else name
        return ""

    items = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        for key in ("data", "models", "result", "list", "items"):
            val = body.get(key)
            if isinstance(val, list):
                items = val
                break
        # 兜底：某些接口直接是 {model_name: {...}} 映射
        if not items and all(isinstance(v, dict) for v in body.values()) and body:
            return sorted(k for k in body.keys() if k)

    out = []
    for it in items:
        n = _name_of(it)
        if n:
            out.append(n)
    # 去重保序后排序
    return sorted(set(out))


# ---------- 对话调用 ----------
def chat(provider: str, base_url: str, api_key: str, model: str,
         system_prompt: str, user_text: str,
         max_tokens: int, thinking_mode: bool = False,
         timeout: int = DEFAULT_TIMEOUT) -> Dict:
    """返回 {status, text, usage_tokens}。status: ok|blocked|rate|quota|error。"""
    provider = (provider or "openai").lower()
    base = normalize_base_url(provider, base_url)
    try:
        if provider in ("openai", "openai_compatible"):
            return _chat_openai(base, api_key, model, system_prompt, user_text,
                                max_tokens, thinking_mode, timeout)
        if provider == "claude":
            return _chat_claude(base, api_key, model, system_prompt, user_text,
                                max_tokens, timeout)
        if provider == "gemini":
            return _chat_gemini(base, api_key, model, system_prompt, user_text,
                                max_tokens, timeout)
        return {"status": "error", "text": "", "usage_tokens": 0}
    except requests.exceptions.HTTPError as e:
        return _classify_http_error(e)
    except (requests.exceptions.RequestException, ValueError):
        return {"status": "error", "text": "", "usage_tokens": 0}


def _classify_http_error(e) -> Dict:
    code = getattr(e.response, "status_code", 0)
    body = ""
    try:
        body = e.response.text.lower()
    except Exception:
        pass
    if "content_filter" in body or "responsible" in body or "safety" in body:
        return {"status": "blocked", "text": "", "usage_tokens": 0}
    if code == 429 or "rate limit" in body or "too many requests" in body:
        if any(k in body for k in ("quota", "insufficient", "balance", "exceeded")):
            return {"status": "quota", "text": "", "usage_tokens": 0}
        return {"status": "rate", "text": "", "usage_tokens": 0}
    if any(k in body for k in ("quota", "insufficient", "balance")):
        return {"status": "quota", "text": "", "usage_tokens": 0}
    return {"status": "error", "text": "", "usage_tokens": 0}


def _chat_openai(base, api_key, model, sys_p, text, max_tokens, thinking, timeout) -> Dict:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": text}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    if thinking:
        payload["thinking_mode"] = True
    r = requests.post(f"{base}/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    choice = body["choices"][0]
    if choice.get("finish_reason") == "content_filter":
        return {"status": "blocked", "text": "", "usage_tokens": 0}
    content = choice["message"]["content"] or ""
    usage = (body.get("usage") or {}).get("total_tokens", 0)
    return {"status": "ok", "text": content.strip(), "usage_tokens": usage}


def _chat_claude(base, api_key, model, sys_p, text, max_tokens, timeout) -> Dict:
    payload = {
        "model": model,
        "system": sys_p,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    r = requests.post(f"{base}/messages",
                      headers={"x-api-key": api_key,
                               "anthropic-version": "2023-06-01",
                               "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    parts = body.get("content", [])
    content = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    usage = body.get("usage", {})
    total = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
    return {"status": "ok", "text": content.strip(), "usage_tokens": total}


def _chat_gemini(base, api_key, model, sys_p, text, max_tokens, timeout) -> Dict:
    payload = {
        "systemInstruction": {"parts": [{"text": sys_p}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens},
    }
    r = requests.post(f"{base}/models/{model}:generateContent",
                      params={"key": api_key},
                      headers={"Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    cands = body.get("candidates", [])
    if not cands:
        return {"status": "blocked", "text": "", "usage_tokens": 0}
    parts = cands[0].get("content", {}).get("parts", [])
    content = "".join(p.get("text", "") for p in parts)
    usage = body.get("usageMetadata", {}).get("totalTokenCount", 0)
    return {"status": "ok", "text": content.strip(), "usage_tokens": usage}
