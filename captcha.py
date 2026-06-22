# -*- coding: utf-8 -*-
"""人机验证：图形验证码 + Cloudflare Turnstile。

- 图形验证码：生成混合数字与字母、带干扰线/噪点/扭曲的图片，答案存 session。
  设计目标是让自动化 OCR / AI 较难识别。
- Turnstile：服务端校验前端回传的 token。
"""

import base64
import io
import os
import random
import string

import requests

import settings_store


# 排除易混淆字符（0/O、1/l/I 等）
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789abcdefghijkmnpqrstuvwxyz"
_CAPTCHA_LEN = 5
_SESSION_KEY = "captcha_answer"

# 字体查找路径：仓库内置 > 常见 Linux(DejaVu，Docker 装 fonts-dejavu) > Windows
_BASE_DIR = os.path.abspath(os.path.dirname(__file__))
_FONT_CANDIDATES = [
    os.path.join(_BASE_DIR, "assets", "fonts", "DejaVuSans-Bold.ttf"),
    os.path.join(_BASE_DIR, "assets", "fonts", "captcha.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _load_font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def is_enabled() -> bool:
    return settings_store.get_bool("captcha_enabled")


def captcha_type() -> str:
    return settings_store.get_setting("captcha_type", "image")


def _random_code(n: int = _CAPTCHA_LEN) -> str:
    return "".join(random.choice(_ALPHABET) for _ in range(n))


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def effective_type() -> str:
    """实际生效的验证码类型。

    若配置为图形验证码但服务器缺少 Pillow，则降级为算术文本验证码（text），
    避免登录/注册页因图片生成失败而 500。
    """
    t = captcha_type()
    if t == "image" and not pillow_available():
        return "text"
    return t


def generate_image():
    """生成验证码图片，返回 (data_uri, answer)。需要 Pillow。"""
    from PIL import Image, ImageDraw, ImageFilter

    code = _random_code()
    width, height = 160, 56
    bg = (random.randint(235, 255), random.randint(235, 255), random.randint(235, 255))
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    font = _load_font(34)

    # 干扰线
    for _ in range(6):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=(random.randint(120, 200), random.randint(120, 200), random.randint(120, 200)),
            width=2,
        )
    # 噪点
    for _ in range(450):
        draw.point((random.randint(0, width), random.randint(0, height)),
                   fill=(random.randint(80, 200),) * 3)

    # 逐字符随机颜色 + 旋转后贴上，增加识别难度
    x = 12
    for ch in code:
        ch_img = Image.new("RGBA", (32, 44), (0, 0, 0, 0))
        ch_draw = ImageDraw.Draw(ch_img)
        color = (random.randint(0, 110), random.randint(0, 110), random.randint(0, 110))
        ch_draw.text((4, 2), ch, font=font, fill=color)
        ch_img = ch_img.rotate(random.randint(-32, 32), expand=1, resample=Image.BICUBIC)
        img.paste(ch_img, (x, random.randint(2, 12)), ch_img)
        x += random.randint(26, 32)

    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return data_uri, code


def generate_text_challenge():
    """无 Pillow 时的降级方案：返回 (question, answer) 的算术题。"""
    a, b = random.randint(1, 9), random.randint(1, 9)
    return f"{a} + {b} = ?", str(a + b)


def verify(form, session) -> bool:
    """统一校验入口：根据配置类型校验。未开启则直接通过。"""
    if not is_enabled():
        return True
    ctype = captcha_type()
    if ctype == "turnstile":
        return _verify_remote(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            "turnstile_secret_key", form.get("cf-turnstile-response", ""))
    if ctype == "hcaptcha":
        return _verify_remote(
            "https://api.hcaptcha.com/siteverify",
            "hcaptcha_secret_key", form.get("h-captcha-response", ""))
    if ctype == "recaptcha":
        return _verify_remote(
            "https://www.recaptcha.net/recaptcha/api/siteverify",
            "recaptcha_secret_key", form.get("g-recaptcha-response", ""))
    # 图形验证码 / 算术文本验证码（无 Pillow 时降级），都用 session 存答案比对
    answer = (session.pop(_SESSION_KEY, "") or "").lower()
    user = (form.get("captcha", "") or "").strip().lower()
    return bool(answer) and user == answer


def store_answer(session, answer: str):
    session[_SESSION_KEY] = answer


def _verify_remote(url: str, secret_key_name: str, token: str) -> bool:
    """通用第三方验证码服务端校验（Turnstile / hCaptcha / reCAPTCHA 同协议）。"""
    if not token:
        return False
    secret = settings_store.get_secret(secret_key_name)
    if not secret:
        return False
    try:
        r = requests.post(url, data={"secret": secret, "response": token}, timeout=10)
        return bool(r.json().get("success"))
    except Exception:
        return False


def site_key() -> str:
    """当前验证码类型对应的前端 site key。"""
    ctype = captcha_type()
    return {
        "turnstile": settings_store.get_setting("turnstile_site_key", ""),
        "hcaptcha": settings_store.get_setting("hcaptcha_site_key", ""),
        "recaptcha": settings_store.get_setting("recaptcha_site_key", ""),
    }.get(ctype, "")


def turnstile_site_key() -> str:
    return settings_store.get_setting("turnstile_site_key", "")
