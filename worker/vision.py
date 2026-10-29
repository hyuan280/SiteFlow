"""Standalone worker image client; never expose provider errors or response bodies."""
import base64
import json
import re
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization to the redirect destination.
        return None


urlopen = build_opener(_NoRedirect).open

DEFAULT_PROMPT = "Read the captcha in this image. Return only its characters, without explanation, spaces, or Markdown. Preserve letter case."
DEFAULT_IMAGE_QUIZ_PROMPT = (
    "你是一个识图答题助手。根据图片内容回答题目，选择最符合图片的选项。"
)


class ImageQuizUnclear(ValueError):
    """The model could see the image but could not determine an answer."""


def answer_image_quiz(
    image: bytes,
    question: str,
    options: list[str],
    config: dict,
    custom_prompt: str | None = None,
) -> tuple[int, str]:
    try:
        numbered = "\n".join(f"{index}: {value}" for index, value in enumerate(options, 1))
        instruction = (custom_prompt or DEFAULT_IMAGE_QUIZ_PROMPT).strip()
        prompt = (
            f"{instruction}\n"
            "只返回一个 JSON 对象，不要输出 Markdown。\n"
            'JSON 格式：{"is_clear": true, "selected_index": 1, '
            '"need_refresh": false, "reason": "简短依据"}\n'
            "selected_index 从 1 开始，必须对应下面选项之一；"
            "图片模糊、无法看清或无法确定时把 need_refresh 设为 true。\n"
            f"题目：{question.strip() or '请根据图片选择正确的选项。'}\n"
            f"选项：\n{numbered}"
        )
        body = {
            "model": config["model"],
            "max_tokens": 2048,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                        },
                    ],
                }
            ],
        }
        request = Request(
            config["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config["api_key"],
            },
            method="POST",
        )
        with urlopen(request, timeout=config.get("timeout", 30)) as response:
            choice = json.load(response)["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError()
        text = choice["message"].get("content")
        if isinstance(text, list):
            if not all(
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                for part in text
            ):
                raise ValueError()
            text = "".join(part["text"] for part in text)
        if not isinstance(text, str) or len(text) > 32000:
            raise ValueError()
        text = text.strip()
        while text.startswith("<think>"):
            end = text.find("</think>")
            if end < 0 or "<think>" in text[len("<think>") : end]:
                raise ValueError()
            text = text[end + len("</think>") :].strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise ValueError()
        result = json.loads(match.group())
        if not isinstance(result, dict):
            raise ValueError()
        reason = str(result.get("reason") or "").strip()[:500]
        if result.get("need_refresh") is True or result.get("is_clear") is False:
            raise ImageQuizUnclear(reason or "视觉模型无法确定答案")
        selected = result.get("selected_index")
        if (
            not isinstance(selected, int)
            or isinstance(selected, bool)
            or not 1 <= selected <= len(options)
        ):
            raise ValueError()
        return selected, reason
    except ImageQuizUnclear:
        raise
    except Exception:
        raise ValueError(
            "Vision provider request failed or returned invalid output"
        ) from None


def recognize(image: bytes, config: dict, prompt: str) -> str:
    try:
        body = {"model": config["model"], "max_tokens": 128, "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}}]}]}
        request = Request(config["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
            "Authorization": "Bearer " + config["api_key"]}, method="POST")
        with urlopen(request, timeout=config.get("timeout", 30)) as response:
            choice = json.load(response)["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError()
        # reasoning_content is deliberately never a source of captcha characters.
        text = choice["message"].get("content")
        if isinstance(text, list):
            if not all(isinstance(part, dict) and part.get("type") == "text"
                       and isinstance(part.get("text"), str) for part in text):
                raise ValueError()
            text = "".join(part["text"] for part in text)
        if not isinstance(text, str) or len(text) > 32000:
            raise ValueError()
        text = text.strip()
        # Accept only balanced leading reasoning blocks. Never salvage characters
        # from malformed, nested, trailing, or unterminated reasoning wrappers.
        while text.startswith("<think>"):
            end = text.find("</think>")
            if end < 0 or "<think>" in text[len("<think>"):end]:
                raise ValueError()
            text = text[end + len("</think>"):].strip()
        if not re.fullmatch(r"[A-Za-z0-9]{1,512}", text):
            raise ValueError()
        return text
    except Exception:
        raise ValueError("Vision provider request failed or returned invalid output") from None
