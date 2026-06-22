# -*- coding: utf-8 -*-
"""
文栅内容安全 核心检测器。

检测流程（纯 AI 模型，不使用本地 Transformer）：
1. 极轻量前置：只拦机械刷屏垃圾（省一次模型调用）。
2. AI 模型检测：intern 付费模型优先，失败回退 aihubmix 免费模型，
   利用内容过滤器拦截信号 + 模型判定。
3. 模型全部不可用时按兜底策略处理。
"""

from typing import Dict, Optional

try:
    from .gpt_classifier import GPTClassifier
except Exception:  # pragma: no cover
    GPTClassifier = None

from .prefilter import is_obvious_garbage


class SpamNoteFighter:
    """
    垃圾/违禁文本检测系统。

    检测类别：porn 色情 / political 政治 / advertisement 广告（允许） /
    spam 垃圾无意义 / insult 辱骂 / normal 正常
    """

    CATEGORIES = ["porn", "political", "advertisement", "spam", "insult", "normal"]

    def __init__(
        self,
        gpt_classifier: Optional["GPTClassifier"] = None,
        **_ignore,
    ):
        # 仅使用 AI 模型，不加载本地 Transformer
        self.gpt = gpt_classifier

    def detect(self, text: str, labels=None, extra_prompt: str = "") -> Dict:
        """
        检测单条文本。

        labels: 自定义标签集 [{"label","definition","blocked"}]；为空用内置 6 类。
        extra_prompt: 追加引导语。
        返回：is_spam / category / confidence / allowed / source / details
        """
        if not text or not text.strip():
            default = labels[0]["label"] if labels else "normal"
            return self._result(default, 1.0, "rule", {"reason": "empty_text"}, allowed=True)

        # 是否启用乱码前置过滤：仅当标签集包含 spam（内置全集或自定义含 spam）时启用，
        # 否则（如昵称场景不含 spam）跳过，避免把正常昵称误判为无意义内容
        spam_in_labels = (not labels) or any(
            str(it.get("label", "")).strip() == "spam" for it in labels
        )
        if spam_in_labels and is_obvious_garbage(text):
            return self._result("spam", 0.99, "prefilter", {"reason": "obvious_garbage"}, allowed=False)

        # AI 模型检测（intern 优先，免费模型回退）
        if self.gpt is not None:
            r = self.gpt.classify(text, labels=labels, extra_prompt=extra_prompt)
            details = {"blocked": r.get("blocked", False), "model": r.get("model")}
            conf = 0.99 if r.get("source") == "filter" else 0.9
            allowed = not r.get("blocked", False)
            return self._result(r["label"], conf, r.get("source", "model"), details, allowed=allowed)

        # 未配置任何模型：放行
        default = labels[0]["label"] if labels else "normal"
        return self._result(default, 0.0, "none", {}, allowed=True)

    @staticmethod
    def _result(cat: str, conf: float, source: str, details: Dict, allowed: bool = None) -> Dict:
        if allowed is None:
            allowed = cat in ("normal", "advertisement")
        return {
            "is_spam": not allowed,
            "category": cat,
            "confidence": round(float(conf), 4),
            "allowed": allowed,
            "source": source,
            "details": details,
        }

    def is_fitted(self) -> bool:
        return self.gpt is not None
