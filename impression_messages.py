"""Conservative, independent evidence and self-query checks (no SDK imports)."""
from __future__ import annotations

import math
import re
import time
from collections import OrderedDict


def own_text(message: dict) -> str:
    """Never use host-expanded quotations when raw segments are available."""
    raw = message.get("raw_message")
    if isinstance(raw, list):
        parts = []
        for segment in raw:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                data = data.get("text")
            if isinstance(data, str):
                parts.append(data)
        return " ".join(parts).strip()
    # Unknown raw format may contain a reply/forward: fail closed.
    if raw is not None:
        return ""
    text = message.get("processed_plain_text")
    if not isinstance(text, str):
        return ""
    if re.search(r"[\[【](?:回复|引用)|^>|<message|转发消息", text):
        return ""
    return text.strip()


def is_self_query(text: str, character_name: str = "阿羽") -> bool:
    """Small allowlist; ambiguity costs functionality, never another user's data."""
    text = text.strip()
    if len(text) > 120:
        return False
    # A literal role name is an address, never the queried person's identity.
    if character_name:
        text = re.sub(r"^@?" + re.escape(character_name) + r"[，,：:\s]*", "", text, count=1, flags=re.I)
    if re.search(r"[\d@<>\n\"'“”‘’]|他|她|别人|其他|我们|咱们", text):
        return False
    if text == "/affection":
        return True
    text = re.sub(r"[，,。？?！!～~\s]", "", text)
    if text in {"好感度", "亲密度", "好感值", "好感度查询", "查询好感度", "印象查询", "查询印象"}:
        return True
    polite = r"(?:请|麻烦你?|能不能|可以|能)?(?:帮我)?"
    verb = r"(?:查询一下|查一下|查询|查查|查看|看看|查|告诉我|说说|我想知道|想知道|我想问问|想问问)"
    subject = r"(?:(?:现在|目前)?(?:你对我|对我|我(?:的)?|我和你|我跟你|咱俩)(?:的)?|(?:现在|目前)(?:的)?)"
    metric = r"(?:好感度|亲密度|好感值|印象)"
    suffix = r"(?:查询|(?:现在|目前)?(?:是|有)?(?:多少|几分|几点|什么等级|哪个等级|怎么样|如何|怎样))?(?:呀|啊|呢|嘛|吗|呗|吧|谢谢)?"
    if re.fullmatch(polite + verb + subject + "?" + metric + suffix, text):
        return True
    if re.fullmatch(polite + subject + metric + suffix, text):
        return True
    if re.fullmatch(polite + r"(?:现在|目前)?(?:好感度|亲密度|好感值)" + suffix, text):
        return True
    return bool(re.fullmatch(
        polite + r"(?:说说|谈谈|告诉我|评价一下)?(?:你对我|对我)(?:的)?"
        r"(?:(?:印象|感觉)(?:怎么样|如何|怎样|咋样|是什么样的|是什么)?|(?:是什么|有什么)(?:印象|感觉))"
        r"(?:呀|啊|呢|吗|吧)?", text))


def self_query_message_text(message: dict, character_name: str, bot_ids: set[str]) -> str:
    """Accept only genuine self-query text and mentions of trusted bot accounts."""
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for segment in raw:
            if not isinstance(segment, dict):
                return ""
            if segment.get("type") not in {"text", "at"}:
                return ""
            if segment.get("type") == "at":
                data = segment.get("data")
                target = str(data.get("target_user_id") or "").strip() if isinstance(data, dict) else ""
                if not target.isascii() or not target.isdigit() or target not in bot_ids:
                    return ""
    text = own_text(message)
    return text if is_self_query(text, character_name) else ""


class QueryRegistry:
    """Only real receive-hook messages enter this bounded, expiring registry."""

    def __init__(self, clock=time.time, ttl=600, capacity=512):
        self.clock, self.ttl, self.capacity = clock, ttl, capacity
        self.entries = OrderedDict()

    def remember(self, stream: str, mid: str, uid: str, text: str, name: str, group_id: str = ""):
        self._expire()
        key = (stream, mid)
        if key in self.entries:  # A replay cannot reset the display claim.
            return
        if stream and mid and uid.isascii() and uid.isdigit() and is_self_query(text, name):
            self.entries[key] = [uid, self.clock(), False, group_id]
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)

    def claim(self, stream: str, mid: str, uid: str) -> bool:
        self._expire()
        entry = self.entries.get((stream, mid))
        if not entry or entry[0] != uid or entry[2]:
            return False
        entry[2] = True
        return True

    def group_id(self, stream, mid, uid):
        self._expire()
        entry = self.entries.get((stream, mid))
        return entry[3] if entry and entry[0] == uid else ""

    def _expire(self):
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if now - entry[1] >= self.ttl:
                del self.entries[key]


_INJECTION = re.compile(r"忽略.{0,12}(?:规则|指令|提示)|系统提示|提示词|system\s*prompt|ignore.{0,20}instructions|写我天下第一", re.I)
_PRIVATE = re.compile(r"https?://\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]+\b|\d{5,}|(?:密码|密钥|token|api_key)\s*[:：=]\s*\S+", re.I)
_ACKNOWLEDGEMENTS = {"好的", "好吧", "好哒", "嗯嗯", "嗯哼", "哈哈", "哈哈哈", "呵呵", "收到", "可以", "是的", "没错", "了解", "ok", "okay"}


def evidence(raw, uid: str, stream: str, start: float, end: float,
             processed: dict, max_chars: int, max_messages: int = 500, min_valid_chars: int = 6) -> list[dict]:
    if isinstance(raw, dict):
        if raw.get("success") is False:
            raise ValueError("history request rejected")
        raw = raw.get("messages")
    if not isinstance(raw, list):
        raise ValueError("unsupported history envelope")
    if len(raw) > max_messages:
        raise ValueError("host exceeded history limit")
    found, texts = {}, set()
    for msg in raw:
        if not isinstance(msg, dict) or any(msg.get(k) for k in ("is_command", "is_notify", "is_mai", "is_bot")):
            continue
        info = msg.get("message_info")
        user = info.get("user_info") if isinstance(info, dict) else None
        if not isinstance(user, dict) or str(user.get("user_id") or "").strip() != uid:
            continue
        platform = user.get("platform") or info.get("platform") or msg.get("platform")
        if platform and str(platform).lower() != "qq":
            continue
        if any(msg.get(k) and str(msg[k]) != stream for k in ("session_id", "stream_id", "chat_id")):
            continue
        mid = str(msg.get("message_id") or "").strip()
        try:
            ts = float(msg.get("timestamp"))
        except (TypeError, ValueError):
            continue
        if not mid or mid in processed or not math.isfinite(ts) or not start <= ts < end:
            continue
        text = own_text(msg)
        if text.startswith("/") or _INJECTION.search(text) or re.search(r"好感度|亲密度|好感值", text):
            continue
        text = _PRIVATE.sub("[隐去]", text)
        text = re.sub(r"\s+", " ", text).strip()[:max_chars]
        # Redaction markers are not evidence. Lowering the first-message limit
        # must not turn a removed phone number/credential into a personality cue.
        content = text.replace("[隐去]", "")
        words = "".join(re.findall(r"[^\W_]+", content, flags=re.UNICODE))
        if len(words) < max(2, min_valid_chars) or text in texts:
            continue
        folded = words.casefold()
        only_ack = any(len(folded) % len(word) == 0
                       and folded == word * (len(folded) // len(word)) for word in _ACKNOWLEDGEMENTS)
        if only_ack or words.isdigit() or len(set(folded)) == 1:
            continue
        texts.add(text)
        found[mid] = {"id": mid, "time": ts, "text": text}
    return sorted(found.values(), key=lambda m: (m["time"], m["id"]))
