from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

Category = Literal["flow", "auth", "challenge", "sign", "action", "post"]
Cardinality = Literal["single", "multiple"]
Requirement = Literal["vision", "totp", "cfbox"]
ErrorPolicy = Literal["stop", "continue", "defer", "escalate_interaction"]
Lane = Literal["general", "interaction", "proxy_general", "proxy_interaction"]
LanePolicy = Literal["auto", "general", "interaction", "proxy_general", "proxy_interaction"]

ERROR_POLICIES: tuple[str, ...] = ("stop", "continue", "defer", "escalate_interaction")
LANE_POLICIES: tuple[str, ...] = (
    "auto",
    "general",
    "interaction",
    "proxy_general",
    "proxy_interaction",
)

IMAGE_CAPTCHA_FEATURES = frozenset(
    {"challenge.image_captcha", "sign.iframe.image_captcha"}
)
VISION_FEATURES = IMAGE_CAPTCHA_FEATURES | {"sign.image_quiz"}
EXECUTION_SETTINGS_FEATURES = VISION_FEATURES | {"flow.result"}
CAPTCHA_DEFAULT_PROMPT = (
    "Read the captcha in this image. Return only its characters, without "
    "explanation, spaces, or Markdown. Preserve letter case."
)
IMAGE_QUIZ_DEFAULT_PROMPT = (
    "你是一个识图答题助手。根据图片内容回答题目，选择最符合图片的选项。"
)
RESULT_DEFAULT_PROMPT = (
    "检查这个页面，判断网站是否已登录，并仔细检查是否存在未签到的标记。"
    "如果没有登录或尚未签到，则输出 False；只有明确已登录且已签到才输出 True。"
    "必须明确输出结论，只返回 True 或 False，不要解释。"
)


class FeatureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeatureConfigError(ValueError):
    pass


FIELD_TITLES: dict[str, str] = {
    "url": "地址",
    "wait_until": "加载完成条件",
    "retry_count": "重试次数",
    "username_selector": "用户名选择器",
    "password_selector": "密码选择器",
    "submit_selector": "提交按钮选择器",
    "username_field": "用户名表单字段",
    "password_field": "密码表单字段",
    "success_selector": "成功元素选择器",
    "success_when_missing": "成功后消失的元素",
    "success_wait_seconds": "成功等待秒数",
    "code_selector": "验证码输入框选择器",
    "service_hint": "2FA 服务名（必须）",
    "username_hint": "2FA 用户名（可选）",
    "totp_provider_id": "2FA 提供商",
    "iframe_selector": "iframe 选择器",
    "wait_seconds": "等待秒数",
    "container_selector": "容器选择器",
    "widget_selector": "ALTCHA 组件选择器",
    "checkbox_selector": "验证复选框选择器",
    "token_selector": "ALTCHA Token 选择器",
    "handle_selector": "手柄选择器",
    "success_signals": "成功信号",
    "image_selector": "图片选择器",
    "input_selector": "输入框选择器",
    "refresh_selector": "刷新验证码选择器",
    "answer_selector": "答题选项选择器",
    "question_selector": "题目文字选择器",
    "message_selector": "留言输入框选择器",
    "message_text": "留言内容",
    "max_refresh": "最大刷新次数",
    "captcha_method": "验证码识别方式",
    "min_confidence": "最低置信度",
    "charset": "验证码字符集",
    "length": "验证码长度",
    "button_selector": "按钮选择器",
    "confirm_selector": "确认按钮选择器",
    "fields": "静态字段",
    "claim_selector": "Claim 按钮选择器",
    "selector": "元素选择器",
    "button": "鼠标按键",
    "click_count": "点击次数",
    "value": "填写值",
    "clear": "填写前清空",
    "seconds": "等待秒数",
    "texts": "页面文字",
    "selectors": "页面选择器",
    "action": "命中后的动作",
    "target_step_key": "目标步骤 key",
    "max_jumps": "最大跳转次数",
    "shop_url": "商店地址",
    "card_title": "补签卡商品标题",
    "option_selector": "商品 option 选择器",
    "price_column_index": "价格表格列",
    "points_regex": "魔力值正则",
    "card_count_regex": "补签卡数量正则",
    "exchange_action": "兑换 action",
    "submit_value": "兑换提交值",
    "max_price": "单张价格上限",
    "purchase_interval_seconds": "购买间隔秒数",
    "attendance_url": "签到页地址",
    "calendar_title_selector": "日历标题选择器",
    "calendar_prev_selector": "上一月选择器",
    "calendar_next_selector": "下一月选择器",
    "calendar_day_selector": "日历日期选择器",
    "calendar_day_number_selector": "日期数字选择器",
    "calendar_event_selector": "签到事件选择器",
    "calendar_block_selector": "已签到事件选择器",
    "calendar_dot_selector": "漏签事件选择器",
    "ajax_path": "补签 AJAX 地址",
    "ajax_action": "补签 action",
    "ajax_date_param": "补签日期参数",
    "output_value": "输出值",
    "message": "输出信息",
    "llm_enabled": "大模型结果判断",
    "llm_prompt": "判断提示词（可选）",
}


@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    category: Category
    cardinality: Cardinality
    requires: frozenset[Requirement]
    config_model: type[BaseModel]
    description: str = ""
    version: int = 1

    def config_schema(self) -> dict[str, Any]:
        schema = self.config_model.model_json_schema()
        properties = schema.get("properties", {})
        for key, title in FIELD_TITLES.items():
            if key in properties:
                properties[key]["title"] = title
        captcha = properties.get("captcha_method")
        if isinstance(captcha, dict):
            from app.features.plugin_registry import plugin_captcha_methods

            extra = [
                method
                for method in plugin_captcha_methods()
                if method.key not in (captcha.get("enum") or [])
            ]
            if extra:
                captcha["enum"] = [*(captcha.get("enum") or []), *(item.key for item in extra)]
                captcha.setdefault("enum_titles", {})
                captcha.setdefault("enum_descriptions", {})
                for item in extra:
                    captcha["enum_titles"][item.key] = item.label
                    captcha["enum_descriptions"][item.key] = item.description
        return schema

    def validate_override(self, config: dict[str, Any]) -> dict[str, Any]:
        """Validate a step-level config without requiring inherited fields.

        A site step may override only one key of a template step. ``None`` is
        allowed at this stage because it is the documented "clear inherited
        value" marker. Full validation happens after merge in the resolver.
        """
        if not isinstance(config, dict):
            raise FeatureConfigError("config 必须是 JSON 对象")
        fields = self.config_model.model_fields
        unknown = sorted(set(config) - set(fields))
        if unknown:
            raise FeatureConfigError(f"未知配置字段: {', '.join(unknown)}")
        for key, value in config.items():
            if value is None:
                continue
            try:
                TypeAdapter(fields[key].annotation).validate_python(value)
            except ValidationError as exc:
                message = exc.errors()[0].get("msg", str(exc))
                raise FeatureConfigError(f"{key}: {message}") from exc
        return config

    def validate_full(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            model = self.config_model.model_validate(config)
        except ValidationError as exc:
            message = exc.errors()[0].get("msg", str(exc))
            location = ".".join(str(part) for part in exc.errors()[0].get("loc", ()))
            raise FeatureConfigError(f"{location}: {message}" if location else message) from exc
        return model.model_dump(mode="json")
