from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.features.base import Feature, FeatureConfig
from app.features.captcha import (
    CAPTCHA_METHOD_KEYS,
    CAPTCHA_METHODS,
    DEFAULT_CAPTCHA_METHOD,
)

WaitUntil = Literal["domcontentloaded", "load", "networkidle"]
SelectorValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SelectorCandidates = list[SelectorValue]
TextValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TextCandidates = list[TextValue]
SELECTOR_HINT = "按顺序尝试，第一个命中的选择器生效"


class FlowOpenConfig(FeatureConfig):
    url: str = Field(
        default="{base_url}",
        min_length=1,
        description="要打开的地址，支持 {base_url} 占位符",
        json_schema_extra={"ui:widget": "text"},
    )
    wait_until: WaitUntil = Field(
        default="domcontentloaded",
        description="页面加载完成的判定条件",
        json_schema_extra={"ui:widget": "select"},
    )
    retry_count: int = Field(
        default=5,
        ge=0,
        le=20,
        description="首次访问失败后的重试次数；网络错误或站点错误页均重试，错误页重试时临时禁用当前标签页缓存；0 表示不重试",
    )


class AuthFormLoginConfig(FeatureConfig):
    username_selector: SelectorCandidates = Field(
        default_factory=lambda: ["input[name='username']"],
        min_length=1,
        description=f"用户名输入框选择器；{SELECTOR_HINT}",
    )
    password_selector: SelectorCandidates = Field(
        default_factory=lambda: ["input[name='password']"],
        min_length=1,
        description=f"密码输入框选择器；{SELECTOR_HINT}",
    )
    submit_selector: SelectorCandidates = Field(
        default_factory=lambda: ["button[type='submit']"],
        min_length=1,
        description=f"登录按钮选择器；{SELECTOR_HINT}",
    )
    username_field: str = Field(default="username", description="表单用户名字段名")
    password_field: str = Field(default="password", description="表单密码字段名")
    success_selector: SelectorCandidates = Field(
        default_factory=list, description=f"登录成功后出现的元素；{SELECTOR_HINT}"
    )


class AuthTotpLoginConfig(FeatureConfig):
    code_selector: SelectorCandidates = Field(
        default_factory=lambda: ["input[name='code']"],
        min_length=1,
        description=f"动态验证码输入框选择器；{SELECTOR_HINT}",
    )
    submit_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"可选提交按钮选择器；留空只填写，交由后续登录步骤提交；{SELECTOR_HINT}",
    )
    success_selector: SelectorCandidates = Field(
        default_factory=list, description=f"验证成功后出现的元素；{SELECTOR_HINT}"
    )
    service_hint: str = Field(
        default="",
        description="所有 2FA 提供商必须填写的服务匹配名称",
    )
    username_hint: str = Field(default="", description="可选的 2FA 账户用户名，用于区分同一服务的账户")
    totp_provider_id: str = Field(default="builtin.twofauth", pattern=r"^(builtin\.twofauth|plugin\.[a-z][a-z0-9-]{0,62})$")

    @model_validator(mode="after")
    def _validate_provider(self):
        if not self.service_hint.strip():
            raise ValueError("service_hint is required for every 2FA provider")
        return self


class ChallengeTurnstileConfig(FeatureConfig):
    wait_seconds: int = Field(
        default=15,
        ge=1,
        le=120,
        description=(
            "从步骤开始计算的 Turnstile 总等待上限；"
            "包含 DOM 检测、CFbox 识别和点击后的等待"
        ),
    )


class ChallengeAltchaConfig(FeatureConfig):
    widget_selector: SelectorCandidates = Field(
        default_factory=lambda: ["altcha-widget"],
        min_length=1,
        description=f"ALTCHA 组件选择器；{SELECTOR_HINT}",
    )
    checkbox_selector: SelectorCandidates = Field(
        default_factory=lambda: [
            "altcha-widget .altcha-checkbox-wrap label",
            "altcha-widget .altcha-checkbox input[type='checkbox']",
        ],
        min_length=1,
        description=f"ALTCHA 验证复选框或标签选择器；{SELECTOR_HINT}",
    )
    token_selector: SelectorCandidates = Field(
        default_factory=lambda: ["altcha-widget input[name='altchaPayload']"],
        min_length=1,
        description=f"ALTCHA 验证通过后写入 Token 的隐藏输入框；{SELECTOR_HINT}",
    )
    success_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"额外成功元素选择器，可选；{SELECTOR_HINT}",
    )
    wait_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        description="等待 ALTCHA 验证通过的最长秒数",
    )


class ChallengeSliderConfig(FeatureConfig):
    container_selector: SelectorCandidates = Field(
        default_factory=lambda: ["#nc_1_wrapper"],
        min_length=1,
        description=f"滑块容器选择器；{SELECTOR_HINT}",
    )
    handle_selector: SelectorCandidates = Field(
        default_factory=lambda: ["#nc_1_n1z"],
        min_length=1,
        description=f"滑块手柄选择器；{SELECTOR_HINT}",
    )
    success_selector: SelectorCandidates = Field(
        default_factory=list, description=f"验证成功后出现的元素；{SELECTOR_HINT}"
    )


CaptchaCharset = Literal[
    "alnum",
    "digits",
    "lower",
    "upper",
    "letters",
    "digits_lower",
    "digits_upper",
    "any",
]


class ImageCaptchaFieldsConfig(FeatureConfig):
    captcha_method: str = Field(
        default=DEFAULT_CAPTCHA_METHOD,
        description=(
            "图形验证码识别方式；可选值来自系统内置的识别方法，"
            "新方法加入后会自动出现在下拉选项中"
        ),
        json_schema_extra={
            "enum": list(CAPTCHA_METHOD_KEYS),
            "enum_titles": {method.key: method.label for method in CAPTCHA_METHODS},
            "enum_descriptions": {
                method.key: method.description for method in CAPTCHA_METHODS
            },
        },
    )
    image_selector: SelectorCandidates = Field(
        default_factory=lambda: ["img.captcha"],
        min_length=1,
        description=f"验证码图片选择器；{SELECTOR_HINT}",
    )
    input_selector: SelectorCandidates = Field(
        default_factory=lambda: ["input[name='captcha']"],
        min_length=1,
        description=f"验证码输入框选择器；{SELECTOR_HINT}",
    )
    refresh_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"刷新验证码的按钮或图片选择器；留空则尝试点击验证码图片；{SELECTOR_HINT}",
    )
    min_confidence: float = Field(
        default=0.95,
        ge=0,
        le=1,
        description="ddddocr 最低置信度；低于该值时刷新验证码，无法刷新则重新开始任务",
    )
    charset: CaptchaCharset = Field(
        default="alnum",
        description="识别结果必须符合的字符集；不符合时按低置信度处理",
        json_schema_extra={
            "enum_titles": {
                "alnum": "数字 + 大小写字母",
                "digits": "纯数字",
                "lower": "纯小写字母",
                "upper": "纯大写字母",
                "letters": "大小写字母",
                "digits_lower": "数字 + 小写字母",
                "digits_upper": "数字 + 大写字母",
                "any": "不限制",
            }
        },
    )
    length: int = Field(
        default=0,
        ge=0,
        le=20,
        description="期望字符数；0 表示不校验长度",
    )

    @field_validator("captcha_method")
    @classmethod
    def _validate_captcha_method(cls, value: str) -> str:
        from app.features.plugin_registry import plugin_captcha_method_keys

        keys = [*CAPTCHA_METHOD_KEYS, *sorted(plugin_captcha_method_keys())]
        if value not in keys:
            raise ValueError(f"未知的验证码识别方式: {value}；可选值: {', '.join(keys)}")
        return value


class ChallengeImageCaptchaConfig(ImageCaptchaFieldsConfig):
    submit_selector: SelectorCandidates = Field(
        default_factory=list, description=f"验证码提交按钮选择器；{SELECTOR_HINT}"
    )


class SignButtonConfig(FeatureConfig):
    button_selector: SelectorCandidates = Field(
        default_factory=lambda: ["button[type='submit']"],
        min_length=1,
        description=f"签到按钮选择器；{SELECTOR_HINT}",
    )
    confirm_selector: SelectorCandidates = Field(
        default_factory=list, description=f"二次确认按钮选择器；{SELECTOR_HINT}"
    )
    success_signals: list[str] = Field(default_factory=list, description="签到成功页面信号")


class FlowJumpConfig(FeatureConfig):
    texts: TextCandidates = Field(
        default_factory=list,
        description="页面文字包含任意一项时命中；不区分大小写",
    )
    selectors: SelectorCandidates = Field(
        default_factory=list,
        description=f"页面存在任意一个选择器时命中；{SELECTOR_HINT}",
    )
    action: Literal["jump", "restart"] = Field(
        default="jump",
        description="命中后跳转到指定步骤，或重新开始整个任务",
        json_schema_extra={
            "enum_titles": {
                "jump": "跳转到指定步骤",
                "restart": "重新开始任务",
            }
        },
    )
    target_step_key: str = Field(
        default="",
        max_length=64,
        description="跳转目标步骤；动作=跳转到指定步骤时必填",
        json_schema_extra={"ui:widget": "step-key"},
    )
    max_jumps: int = Field(
        default=10,
        ge=0,
        le=100,
        description="本次任务最多跳转次数；防止条件持续命中导致死循环",
    )

    @model_validator(mode="after")
    def _validate_jump(self) -> "FlowJumpConfig":
        self.target_step_key = self.target_step_key.strip()
        if not self.texts and not self.selectors:
            raise ValueError("至少需要配置一项页面文字或选择器")
        if self.action == "jump" and not self.target_step_key:
            raise ValueError("跳转到指定步骤时需要填写目标步骤 key")
        if self.target_step_key and ":" in self.target_step_key:
            raise ValueError("目标步骤 key 不能包含冒号")
        return self


class SignIframeImageCaptchaConfig(ImageCaptchaFieldsConfig):
    iframe_selector: SelectorCandidates = Field(
        default_factory=lambda: ["iframe"],
        min_length=1,
        description=f"签到表单 iframe 选择器；{SELECTOR_HINT}",
    )
    image_selector: SelectorCandidates = Field(
        default_factory=lambda: [
            "img[alt*='CAPTCHA']",
            "img[src*='regimage']",
            "img[src*='image.php']",
            "img[src*='captcha']",
            "img.captcha",
        ],
        min_length=1,
        description=f"iframe 内验证码图片选择器；{SELECTOR_HINT}",
    )
    input_selector: SelectorCandidates = Field(
        default_factory=lambda: ["input[name='imagestring']", "input[name='captcha']"],
        min_length=1,
        description=f"iframe 内验证码输入框选择器；{SELECTOR_HINT}",
    )
    submit_selector: SelectorCandidates = Field(
        default_factory=lambda: ["button[id='ok']", "button[type='submit']"],
        min_length=1,
        description=f"iframe 内提交按钮选择器；{SELECTOR_HINT}",
    )
    fields: dict[str, str] = Field(default_factory=dict, description="iframe 内需要填写的静态字段")


class SignImageQuizConfig(FeatureConfig):
    image_selector: SelectorCandidates = Field(
        default_factory=lambda: ["img[src*='image.php']", "img[alt='captcha']"],
        min_length=1,
        description=f"答题图片选择器；{SELECTOR_HINT}",
    )
    answer_selector: SelectorCandidates = Field(
        default_factory=lambda: ["input[type='submit'][name^='captcha_']"],
        min_length=1,
        description=f"答题选项选择器；按页面顺序读取文本并点击命中项；{SELECTOR_HINT}",
    )
    question_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"题目文字选择器，可选；留空时只根据图片和选项作答；{SELECTOR_HINT}",
    )
    message_selector: SelectorCandidates = Field(
        default_factory=lambda: ["textarea[name='message']"],
        description=f"留言输入框选择器，可选；{SELECTOR_HINT}",
    )
    message_text: str = Field(
        default="每天长知识！",
        max_length=500,
        description="提交答题前填写的留言；留空则不填写",
    )
    refresh_selector: SelectorCandidates = Field(
        default_factory=lambda: [
            "a[onclick*='reload_adbc2']",
            "a[onclick*='reload']",
        ],
        description=f"刷新答题图片或题目的选择器；模型无法确定答案时点击；{SELECTOR_HINT}",
    )
    submit_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"答题提交按钮选择器，可选；留空时点击选项本身提交；{SELECTOR_HINT}",
    )
    success_signals: list[str] = Field(
        default_factory=list,
        description="答题提交成功后页面出现的文本；任一命中即成功（不区分大小写）",
    )
    success_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"答题提交成功后出现的元素；任一命中即成功；{SELECTOR_HINT}",
    )
    success_when_missing: SelectorCandidates = Field(
        default_factory=list,
        description=f"答题提交成功后消失的元素；任一消失即成功；{SELECTOR_HINT}",
    )
    success_wait_seconds: int = Field(
        default=10,
        ge=1,
        le=60,
        description="等待答题成功条件的最长秒数",
    )
    max_refresh: int = Field(
        default=3,
        ge=0,
        le=10,
        description="视觉模型无法确定答案时最多刷新题目次数",
    )


class SignUnit3dClaimConfig(FeatureConfig):
    claim_selector: SelectorCandidates = Field(
        default_factory=lambda: ["button[data-action='claim']"],
        description=f"UNIT3D Claim 按钮选择器；{SELECTOR_HINT}",
    )
    success_signals: list[str] = Field(
        default_factory=list,
        description="领取成功后页面出现的文本；任一命中即成功（不区分大小写）",
    )
    success_selector: SelectorCandidates = Field(
        default_factory=list,
        description=f"领取成功后出现的元素；任一命中即成功；{SELECTOR_HINT}",
    )
    success_when_missing: SelectorCandidates = Field(
        default_factory=list,
        description=f"领取成功后消失的元素；任一消失即成功；{SELECTOR_HINT}",
    )
    success_wait_seconds: int = Field(
        default=10,
        ge=1,
        le=60,
        description="等待成功条件的最长秒数",
    )


class ActionNavigateConfig(FeatureConfig):
    url: str = Field(
        default="{base_url}",
        min_length=1,
        description="目标地址，支持 {base_url} 占位符",
    )
    wait_until: WaitUntil = Field(default="domcontentloaded", description="页面加载完成条件")


class ActionClickConfig(FeatureConfig):
    selector: SelectorCandidates = Field(
        min_length=1, description=f"点击目标选择器；{SELECTOR_HINT}"
    )
    button: Literal["left", "right", "middle"] = Field(default="left", description="鼠标按键")
    click_count: int = Field(default=1, ge=1, le=3, description="点击次数")


class ActionFillConfig(FeatureConfig):
    selector: SelectorCandidates = Field(
        min_length=1, description=f"输入框选择器；{SELECTOR_HINT}"
    )
    value: str = Field(default="", description="要填入的值")
    clear: bool = Field(default=True, description="填写前是否清空")


class ActionWaitConfig(FeatureConfig):
    seconds: int = Field(default=2, ge=1, le=300, description="等待秒数")


class FlowResultConfig(FeatureConfig):
    output_value: bool = Field(
        default=True,
        description="写入步骤结果和站点任务结果的布尔值",
        json_schema_extra={
            "ui:widget": "switch-floating",
            "ui:trueLabel": "输出成功",
            "ui:falseLabel": "输出失败",
        },
    )
    message: str = Field(
        default="",
        max_length=2000,
        description="写入步骤结果和站点任务结果的信息；留空时使用输出值",
    )


class PostBuyCardConfig(FeatureConfig):
    shop_url: str = Field(
        default="{base_url}/mybonus.php",
        min_length=1,
        description="补签卡商店地址，支持 {base_url} 占位符",
    )
    card_title: TextCandidates = Field(
        default_factory=lambda: ["购买补签卡"],
        min_length=1,
        description="商店中补签卡商品的标题候选，按顺序匹配",
    )

    @field_validator("card_title", mode="before")
    @classmethod
    def _normalize_card_title(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value
    option_selector: str = Field(default='input[name="option"]', description="商品 option 输入框选择器")
    price_column_index: int = Field(default=2, ge=0, le=10, description="旧版表格价格列索引；卡片布局自动读取金币价格")
    points_regex: str = Field(
        default=r"魔力值[^\d]*(\d[\d,.]*\d)",
        description="页面魔力值正则，第一个分组为数值",
    )
    card_count_regex: str = Field(
        default=r"[补補][签簽]卡[:：]\s*(\d+)",
        description="页面补签卡数量正则，第一个分组为数量",
    )
    exchange_action: str = Field(default="exchange", description="无表单时的兑换 action；非默认值覆盖表单 action 参数")
    submit_value: str = Field(default="交换", description="无表单时的提交值；非默认值覆盖表单按钮值")
    max_price: int | None = Field(default=None, ge=1, description="单张补签卡最高价格")
    purchase_interval_seconds: int = Field(
        default=12, ge=0, le=120, description="两次购买之间的间隔秒数"
    )


class PostResignConfig(FeatureConfig):
    attendance_url: str = Field(
        default="{base_url}/attendance.php",
        min_length=1,
        description="签到/日历页地址，支持 {base_url} 占位符",
    )
    card_count_regex: str = Field(
        default=r"[补補][签簽]卡[:：]\s*(\d+)",
        description="页面补签卡数量正则，第一个分组为数量",
    )
    calendar_title_selector: str = Field(
        default=".fc-toolbar-title", description="FullCalendar 月份标题选择器"
    )
    calendar_prev_selector: str = Field(
        default=".fc-prev-button", description="FullCalendar 上一月按钮选择器"
    )
    calendar_next_selector: str = Field(
        default=".fc-next-button", description="FullCalendar 下一月按钮选择器"
    )
    calendar_day_selector: str = Field(
        default=".fc-daygrid-day", description="FullCalendar 日期单元格选择器"
    )
    calendar_day_number_selector: str = Field(
        default=".fc-daygrid-day-number", description="日期数字元素选择器"
    )
    calendar_event_selector: str = Field(
        default=".fc-daygrid-event-harness a", description="日期内签到事件选择器"
    )
    calendar_block_selector: str = Field(
        default=".fc-daygrid-block-event", description="已签到事件 class"
    )
    calendar_dot_selector: str = Field(
        default=".fc-daygrid-dot-event", description="漏签事件 class"
    )
    ajax_path: str = Field(default="ajax.php", description="补签 AJAX 请求地址")
    ajax_action: str = Field(default="attendanceRetroactive", description="补签 action 参数")
    ajax_date_param: str = Field(default="params[date]", description="补签日期参数名")


def _feature(
    key: str,
    name: str,
    category: str,
    cardinality: str,
    config_model: type[FeatureConfig],
    *,
    requires: tuple[str, ...] = (),
    description: str = "",
) -> Feature:
    return Feature(
        key=key,
        name=name,
        category=category,  # type: ignore[arg-type]
        cardinality=cardinality,  # type: ignore[arg-type]
        requires=frozenset(requires),  # type: ignore[arg-type]
        config_model=config_model,
        description=description,
    )


FEATURE_LIST: tuple[Feature, ...] = (
    _feature(
        "flow.open",
        "打开站点",
        "flow",
        "single",
        FlowOpenConfig,
        description="打开站点首页或指定入口页。",
    ),
    _feature(
        "auth.form_login",
        "表单登录",
        "auth",
        "single",
        AuthFormLoginConfig,
        description="使用站点用户名和密码登录。",
    ),
    _feature(
        "challenge.2fa",
        "两步验证",
        "challenge",
        "multiple",
        AuthTotpLoginConfig,
        requires=("totp",),
        description="从 2FAuth 获取动态验证码完成二次验证。",
    ),
    _feature(
        "challenge.turnstile",
        "Turnstile 验证",
        "challenge",
        "multiple",
        ChallengeTurnstileConfig,
        requires=("cfbox",),
        description="处理 Cloudflare Turnstile 或类似的人机验证；通过 CFbox 识别截图复选框位置。",
    ),
    _feature(
        "challenge.altcha",
        "ALTCHA 验证",
        "challenge",
        "multiple",
        ChallengeAltchaConfig,
        description="定位并点击 ALTCHA 验证复选框，等待组件完成验证。",
    ),
    _feature(
        "challenge.slider",
        "滑块验证",
        "challenge",
        "multiple",
        ChallengeSliderConfig,
        description="拖动滑块完成验证。",
    ),
    _feature(
        "challenge.image_captcha",
        "图形验证码",
        "challenge",
        "multiple",
        ChallengeImageCaptchaConfig,
        description="选择识别方式并填写图形验证码；默认使用 ddddocr。",
    ),
    _feature(
        "sign.button",
        "点击签到按钮",
        "sign",
        "multiple",
        SignButtonConfig,
        description="点击页面上的签到或打卡按钮。",
    ),
    _feature(
        "sign.iframe.image_captcha",
        "iframe 图像验证码签到",
        "sign",
        "multiple",
        SignIframeImageCaptchaConfig,
        description="在 iframe 中填写字段，选择识别方式并提交图像验证码；默认使用 ddddocr。",
    ),
    _feature(
        "sign.image_quiz",
        "图片答题签到",
        "sign",
        "multiple",
        SignImageQuizConfig,
        requires=("vision",),
        description="提取页面图片和选项，使用视觉模型选择答案并完成签到；未配置视觉模型时转人工处理。",
    ),
    _feature(
        "sign.unit3d_claim",
        "UNIT3D 领取",
        "sign",
        "multiple",
        SignUnit3dClaimConfig,
        description="处理 UNIT3D 站点的 Claim 按钮。",
    ),
    _feature(
        "action.navigate",
        "导航",
        "action",
        "multiple",
        ActionNavigateConfig,
        description="打开指定页面。",
    ),
    _feature(
        "action.click",
        "点击",
        "action",
        "multiple",
        ActionClickConfig,
        description="点击指定元素。",
    ),
    _feature(
        "action.fill",
        "填写",
        "action",
        "multiple",
        ActionFillConfig,
        description="填写输入框。",
    ),
    _feature(
        "action.wait",
        "等待",
        "action",
        "multiple",
        ActionWaitConfig,
        description="固定等待一段时间。",
    ),
    _feature(
        "flow.jump",
        "条件跳转",
        "flow",
        "multiple",
        FlowJumpConfig,
        description="根据页面文字或选择器跳转到任务中的任意步骤，或重新开始任务。",
    ),
    _feature(
        "flow.result",
        "输出结果",
        "flow",
        "multiple",
        FlowResultConfig,
        description="按条件输出布尔结果和信息并结束当前站点任务；输出 false 时任务失败。",
    ),
    _feature(
        "post.buy_card",
        "购买补签卡",
        "post",
        "multiple",
        PostBuyCardConfig,
        description="按待补签日期自动补齐补签卡数量。",
    ),
    _feature(
        "post.resign",
        "补签",
        "post",
        "multiple",
        PostResignConfig,
        description="读取漏签日期并执行 NexusPHP 日历补签。",
    ),
)

FEATURES: dict[str, Feature] = {feature.key: feature for feature in FEATURE_LIST}


FEATURE_ALIASES = {"sign.iframe_form": "sign.iframe.image_captcha"}


def get_feature(key: str):
    from app.features.plugin_registry import get_plugin_feature

    return FEATURES.get(FEATURE_ALIASES.get(key, key)) or get_plugin_feature(key)


def list_features() -> list:
    from app.features.plugin_registry import plugin_features

    return [*FEATURE_LIST, *plugin_features()]
