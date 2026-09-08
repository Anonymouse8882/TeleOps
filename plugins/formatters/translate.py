"""翻译：把正文（可选连标题）译成指定语言。

两个引擎：
- 机器翻译：走 Google 的公开翻译接口，不需要 API Key，开箱即用。这是个未公开
  文档的接口，量大时可能被限流；它不属于任何人的 SLA，别把它当保证。
- OpenRouter：走大模型，需要你自己的 API Key。贵一些，但能理解上下文、能保住
  HTML 标签、也能按你的提示词调语气。

注意：翻译一定要把正文发到第三方服务器（Google 或 OpenRouter）。搬运私密内容
时请自行判断。
"""
from __future__ import annotations

import asyncio
import html as html_mod
import re
from html.parser import HTMLParser

from teleops.core import FormatterPlugin, Item, field

FREE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

#: 一次请求最多送多少字符。免费接口实测两千字没问题，留一半余量，
#: 且优先在段落、句子边界切开——从句子中间切开会让译文读起来是断的。
FREE_CHUNK = 1500

_TAG_RE = re.compile(r"<[^>]+>")
#: Telegram 认的标签就这些，多余的标签发出去会被判成非法实体
_TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
            "a", "code", "pre", "blockquote", "span", "tg-spoiler", "tg-emoji", "br"}

LANGS = [
    {"value": "zh-CN", "label": "简体中文"},
    {"value": "zh-TW", "label": "繁体中文"},
    {"value": "en", "label": "英语"},
    {"value": "ja", "label": "日语"},
    {"value": "ko", "label": "韩语"},
    {"value": "ru", "label": "俄语"},
    {"value": "fr", "label": "法语"},
    {"value": "de", "label": "德语"},
    {"value": "es", "label": "西班牙语"},
    {"value": "pt", "label": "葡萄牙语"},
    {"value": "it", "label": "意大利语"},
    {"value": "ar", "label": "阿拉伯语"},
    {"value": "vi", "label": "越南语"},
    {"value": "th", "label": "泰语"},
    {"value": "id", "label": "印尼语"},
    {"value": "custom", "label": "自定义（填下面的语言代码）"},
]


class _TagBalance(HTMLParser):
    """只回答一件事：翻完之后标签还配得上吗。

    机器翻译偶尔会把 <b>…</b> 译成 <b>…<b>，或者顺手吞掉一个尖括号。这种正文
    发给 Telegram 会直接被判非法实体、整条发不出去，而错误信息指向发送端，
    排查起来完全看不出是翻译干的。所以译完当场验一遍，坏了就退回纯文本。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.bad = False

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            return
        if tag not in _TG_TAGS:
            self.bad = True
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if not self.stack or self.stack[-1] != tag:
            self.bad = True
            return
        self.stack.pop()


def _html_ok(text: str) -> bool:
    p = _TagBalance()
    try:
        p.feed(text)
        p.close()
    except Exception:
        return False
    return not p.bad and not p.stack


def _to_plain(text: str) -> str:
    return html_mod.unescape(_TAG_RE.sub("", text or ""))


def _split(text: str, size: int) -> list[str]:
    """按段落、再按句子切块，尽量不在句子中间下刀。"""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    buf = ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(buf) + len(para) <= size:
            buf += para
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(para) <= size:
            buf = para
            continue
        # 单段就超长：按句末标点切
        for piece in re.split(r"(?<=[。！？!?；;\.\n])", para):
            if len(buf) + len(piece) > size and buf:
                out.append(buf)
                buf = ""
            # 一句话都超长（没有标点的长串）只能硬切
            while len(piece) > size:
                out.append(piece[:size])
                piece = piece[size:]
            buf += piece
    if buf:
        out.append(buf)
    return [c for c in out if c]


class TranslateError(RuntimeError):
    pass


class TranslateFormatter(FormatterPlugin):
    name = "translate"
    display_name = "翻译"
    version = "1.0.0"
    author = "TeleOps"
    description = (
        "把正文翻译成指定语言。可选免费机器翻译（无需 API Key）或 OpenRouter 大模型。"
        "正文会被发送到第三方翻译服务。"
    )

    config_schema = [
        field("target_lang", "翻译成", "select", default="zh-CN", options=LANGS, group="语言"),
        field("target_lang_code", "自定义语言代码", "string", default="",
              placeholder="例如 nl、tr、hi",
              help="上面选了「自定义」时才用这一栏。", group="语言"),
        field("source_lang", "原文语言", "string", default="auto",
              help="填 auto 自动识别；也可以写死，例如 en。", group="语言"),
        field("skip_if_same", "已经是目标语言就跳过", "bool", default=True,
              help="机器翻译引擎会回报识别到的语言；OpenRouter 引擎判断不了，此项对它无效。",
              group="语言"),

        field("provider", "翻译引擎", "select", default="free", options=[
            {"value": "free", "label": "机器翻译（免费，无需 API Key）"},
            {"value": "openrouter", "label": "OpenRouter AI（需要 API Key）"},
        ], group="引擎"),
        field("api_key", "OpenRouter API Key", "password", default="",
              placeholder="sk-or-v1-…", group="引擎"),
        field("model", "OpenRouter 模型", "string", default="openai/gpt-4o-mini",
              help="填 OpenRouter 上的模型 ID。填错的话这里会原样显示它返回的错误。",
              group="引擎"),
        field("prompt", "自定义提示词", "text", default="",
              placeholder="例如：翻译成简体中文，保持新闻电讯的语气，专有名词保留原文并在括号内注明。",
              help="只对 OpenRouter 生效。留空用内置提示词。", group="引擎"),

        field("translate_title", "标题也翻译", "bool", default=True, group="范围"),
        field("keep_html", "保留 HTML 标签", "bool", default=True,
              help="关掉就先转成纯文本再翻。开着时会校验译文的标签是否还配对，"
                   "坏了自动退回纯文本，不会让 Telegram 收到非法实体。", group="范围"),
        field("keep_original", "保留原文", "bool", default=False,
              help="译文在上、原文在下。", group="范围"),
        field("separator", "原文分隔线", "string", default="———", group="范围"),
        field("max_chars", "超长正文上限", "int", default=6000,
              help="超过就先截断再翻，0 表示不限。防止一条超长贴把配额烧光。", group="范围"),

        field("on_error", "翻译失败时", "select", default="fail", options=[
            {"value": "fail", "label": "报错（这条重试，多次失败后挂起）"},
            {"value": "keep", "label": "保留原文继续发"},
        ], help="默认报错：翻译不成还照发原文，等于悄悄发了一条没翻的，很难发现。",
              group="范围"),
    ]

    # ------------------------------------------------------------------ 主流程
    async def format(self, item: Item, ctx) -> Item:
        target = self._target()
        if not target:
            raise TranslateError("没有选目标语言")
        text = item.text or ""
        limit = int(self.get("max_chars", 6000) or 0)
        if limit and len(text) > limit:
            ctx.log.info("正文 %d 字超过上限 %d，先截断再翻", len(text), limit)
            text = text[:limit].rstrip() + "…"
        if not text.strip() and not (self.get("translate_title", True) and item.title):
            return item                       # 纯媒体条目没什么可翻的

        as_html = bool(self.get("keep_html", True)) and item.parse_mode == "html"
        payload = text if as_html else _to_plain(text)

        try:
            translated, detected = await self._translate(payload, target, ctx)
            if self.get("skip_if_same", True) and detected and self._same(detected, target):
                ctx.trace(item, f"已经是{target}，跳过翻译")
                return item
            title_out = item.title
            if self.get("translate_title", True) and item.title:
                title_out, _ = await self._translate(_to_plain(item.title), target, ctx)
        except Exception as e:
            if self.get("on_error", "fail") == "keep":
                ctx.log.warning("翻译失败，按配置保留原文继续：%s", e)
                item.meta["translate_error"] = str(e)[:300]
                return item
            raise TranslateError(f"翻译失败：{e}") from e

        if as_html and not _html_ok(translated):
            # 标签被译坏了：这条要么发不出去，要么带着乱码标签发出去。退回纯文本，
            # 丢的是格式，保住的是这条内容本身。
            ctx.log.warning("译文的 HTML 标签不配对，已退回纯文本")
            translated = _to_plain(translated)
            item.parse_mode = None
        elif not as_html:
            item.parse_mode = None

        if self.get("keep_original", False):
            sep = self.get("separator", "———") or ""
            translated = f"{translated}\n\n{sep}\n\n{payload}" if sep else f"{translated}\n\n{payload}"

        item.text = translated
        item.title = title_out
        item.meta["translated_to"] = target
        item.meta["translate_provider"] = self.get("provider", "free")
        if detected:
            item.meta["translate_from"] = detected
        item.meta["plain_text"] = _to_plain(translated)
        return item

    # ------------------------------------------------------------------ 引擎
    def _target(self) -> str:
        t = (self.get("target_lang", "zh-CN") or "").strip()
        if t == "custom":
            return (self.get("target_lang_code", "") or "").strip()
        return t

    @staticmethod
    def _same(detected: str, target: str) -> bool:
        """zh-CN 和 zh 算同一种；en-US 和 en 也算。"""
        d, t = detected.lower().replace("_", "-"), target.lower().replace("_", "-")
        return d == t or d.split("-")[0] == t.split("-")[0]

    async def _translate(self, text: str, target: str, ctx) -> tuple[str, str]:
        if not text.strip():
            return text, ""
        if self.get("provider", "free") == "openrouter":
            return await self._via_openrouter(text, target, ctx), ""
        return await self._via_free(text, target, ctx)

    async def _via_free(self, text: str, target: str, ctx) -> tuple[str, str]:
        source = (self.get("source_lang", "auto") or "auto").strip() or "auto"
        out: list[str] = []
        detected = ""
        chunks = _split(text, FREE_CHUNK)
        for i, chunk in enumerate(chunks):
            if i:
                await asyncio.sleep(0.4)      # 连着打这个公开接口容易被限流
            r = await ctx.http.post(
                FREE_ENDPOINT,
                params={"client": "gtx", "sl": source, "tl": target, "dt": "t"},
                data={"q": chunk},
            )
            if r.status_code == 429:
                raise TranslateError("免费翻译接口被限流了（429），过一会儿再试或改用 OpenRouter")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data or not isinstance(data[0], list):
                raise TranslateError(f"免费翻译接口返回了看不懂的结构：{str(data)[:200]}")
            out.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
            if not detected and len(data) > 2 and isinstance(data[2], str):
                detected = data[2]
        return "".join(out), detected

    async def _via_openrouter(self, text: str, target: str, ctx) -> str:
        key = (self.get("api_key", "") or "").strip()
        if not key:
            raise TranslateError("选了 OpenRouter 引擎但没填 API Key")
        model = (self.get("model", "") or "").strip()
        if not model:
            raise TranslateError("选了 OpenRouter 引擎但没填模型 ID")
        instruction = (self.get("prompt", "") or "").strip() or (
            f"You are a translator. Translate the user's message into {target}. "
            "Output only the translation, with no preamble, no explanation and no quotes. "
            "Preserve the original line breaks and paragraph structure. "
            "Preserve any HTML tags exactly as they appear. "
            "If the text is already in the target language, output it unchanged."
        )
        r = await ctx.http.post(
            OPENROUTER_ENDPOINT,
            headers={"Authorization": f"Bearer {key}",
                     "X-Title": "TeleOps"},
            json={"model": model, "temperature": 0,
                  "messages": [{"role": "system", "content": instruction},
                               {"role": "user", "content": text}]},
            timeout=90,
        )
        try:
            data = r.json()
        except Exception:
            raise TranslateError(f"OpenRouter 返回了非 JSON（HTTP {r.status_code}）：{r.text[:200]}") from None
        # 模型 ID 填错、余额不足、Key 无效都走这条，原样把它的话带出来，
        # 比翻译成"请求失败"有用得多
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise TranslateError(f"OpenRouter：{msg}")
        r.raise_for_status()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise TranslateError(f"OpenRouter 返回结构异常：{str(data)[:200]}") from None
        return (content or "").strip()
