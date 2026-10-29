from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptchaMethod:
    key: str
    label: str
    description: str


CAPTCHA_METHODS: tuple[CaptchaMethod, ...] = (
    CaptchaMethod(
        key="ddddocr",
        label="ddddocr",
        description="通用的 OCR 识别工具，简单但准确度不高。",
    ),
    CaptchaMethod(
        key="vision",
        label="视觉模型识别",
        description="使用视觉大模型识别，适应大部分站点，需要配置模型并开启支持视觉。",
    ),
    CaptchaMethod(
        key="nexusphp_gd5",
        label="nexusphp_gd5",
        description=(
            "稳定识别 NexusPHP 站点生成的 GD 5 号内置字体验证码；"
            "支持 6 位字符，并自动适配网站缩放后的图片尺寸。"
        ),
    ),
)

CAPTCHA_METHOD_KEYS: tuple[str, ...] = tuple(method.key for method in CAPTCHA_METHODS)
DEFAULT_CAPTCHA_METHOD = "ddddocr"
