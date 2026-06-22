# -*- coding: utf-8 -*-
"""
极简垃圾前置过滤。

只拦截「百分百确定」的机械刷屏，以节省 GPT API 调用；其余一切（包括各类
乱码、夹带乱码的中文、类密码串等）一律交给 AI 模型判断——AI 判断更稳定，
代码规则难以穷尽所有乱码形态。

拦截范围（命中即判 spam）：
1. 连续重复同一字符 8 次以上（如 1111111111、aaaaaaaa、啊啊啊啊啊啊）。
2. 超长纯数字刷屏（>=16 位且全为数字）。
3. 字符种类极少的超长串（>=20 位且只有 1-2 种字符，如 ababab...）。
"""

import re

# 连续 8+ 个相同字符
_REPEAT_RE = re.compile(r"(.)\1{7}")


def is_obvious_garbage(text: str) -> bool:
    """只判机械刷屏，保守为主，其余交给 AI。"""
    if not text:
        return False
    s = text.strip()
    length = len(s)
    if length < 8:
        return False

    # 1) 连续重复同一字符 8 次以上
    if _REPEAT_RE.search(s):
        return True

    # 2) 超长纯数字（>=16 位且全是数字）
    if length >= 16 and s.isdigit():
        return True

    # 3) 字符种类极少的超长串（如 ababababab...）
    if length >= 20 and len(set(s)) <= 2:
        return True

    return False
