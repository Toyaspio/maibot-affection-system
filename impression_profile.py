"""Optional, read-only compatibility reader for an existing native profile cache.

The caller must verify the QQ user <-> person_id mapping before calling. Returned
text is untrusted internal context, never a public quote or a relationship fact.
No native memory engine, SDK initialization, or database creation is involved.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .impression_messages import _INJECTION, _PRIVATE


_PROVIDER = "native_profile_snapshot"
_PID = re.compile(r"[0-9a-fA-F]{32}\Z", re.ASCII)
_SECTIONS = ("身份设定", "稳定了解", "相处偏好", "不确定信息")
_SECTION = re.compile(
    r"^(身份设定|稳定了解|相处偏好|不确定信息|关系设定|近期互动)"
    r"(?:[（(][^（）()\n]{0,24}[）)])?\s*(?:[:：]\s*(.*))?$"
)
_META = re.compile(
    r"person[_ -]?id|user[_ -]?id|\bqq\b|\buid\b|别名|昵称|用户[号名]|"
    r"用户\s*ID|身份标识|档案[号版]|维护|备注|更新于|更新时间|创建时间|过期时间|"
    r"profile_version|updated_at|expires_at|[0-9a-f]{32}", re.I
)
_SENSITIVE = re.compile(
    r"地址|住址|住在|居住|现居|定居|籍贯|户籍|老家|家在|来自|旅居|暂住|租住|"
    r"常驻|居于|生活在|所在地|位置|学校|大学|中学|小学|学院|校区|院校|高校|"
    r"初中|高中|附中|一中|二中|三中|中专|专科|职高|就学|求学|上学|念书|"
    r"攻读|在读|就读|入学|毕业|学历|教育背景|师范|理工|职业技术|"
    r"职业|工作|公司|单位|实习|上班|任职|任教|供职|就职|受雇|雇主|"
    r"教师|老师|医生|律师|工程师|程序员|学生|"
    r"研究生|本科|硕士|博士|生日|出生|年龄|电话|手机号|身份证|护照|银行卡|"
    r"宿舍|小区|街道|车站|机场|医院|商场|北京|上海|天津|重庆|广州|深圳|"
    r"杭州|南京|成都|武汉|西安|苏州|长沙|郑州|厦门|福州|济南|青岛|"
    r"云南|四川|贵州|黑龙江|吉林|辽宁|河北|河南|山东|山西|陕西|甘肃|青海|"
    r"广东|广西|海南|福建|浙江|江苏|安徽|江西|湖北|湖南|内蒙古|西藏|宁夏|"
    r"新疆|香港|澳门|台湾|日本|韩国|美国|英国|法国|德国|加拿大|澳大利亚|"
    r"新加坡|俄罗斯|泰国|越南|马来西亚|东京|大阪|京都|首尔|纽约|伦敦|巴黎|"
    r"[\u4e00-\u9fff]{2,12}(?:省|市|县|镇|村|路|大街|园区|大学|中学)"
)
_EXPERIENCE = re.compile(
    r"曾经|曾在|曾到|去过|参加过|见过|遇到过|发生过|经历|昨天|前天|上周|"
    r"上次|去年|昨晚|那天|当天|那次|这次|上个月|最近|刚刚|当时|具体事件|"
    r"前往|来自|籍贯|老家|常驻|居于|旅居|任职|供职|在读|求学|教育背景|院校|高中|师范|一中|"
    r"在.{1,16}(?:生活|居住|工作|读书)|"
    # Movement/residence predicates do not require a known destination name or
    # past-tense marker: e.g. a name followed by "去云南" is still a concrete fact.
    # This deliberately drops some benign "去了解" wording rather than admitting
    # personal events as personality evidence.
    r"(?:去(?:了|过|往|到)?|回(?:了|过|到|趟)|(?<!记)住(?:了|在|进|于)?|"
    r"搬(?:了|到|去)|迁(?:居|往|到)|抵达|到达|到过|途经|途径|出差|旅行|旅游|"
    r"探亲|访学|留学)\s*[\u4e00-\u9fffA-Za-z]|"
    r"\d{2,4}年|\d{1,2}月\d{1,2}[日号]|\d{1,2}[:：]\d{2}"
)
_THIRD_PERSON = re.compile(
    r"他|她|别人|第三人|某人|某某|同学|朋友|家人|父母|父亲|母亲|哥哥|姐姐|"
    r"弟弟|妹妹|男友|女友|对象|伴侣|同事|同班|同桌|室友|舍友|校友|网友|"
    r"群友|管理员|陌生人|"
    r"(?:和|与|跟).{1,16}(?:一起|交往|聊天|见面|争执|吵架|结识)|"
    r"小[\u4e00-\u9fff]{1,2}(?:说|表示|认为|觉得|曾|是)"
)
_RELATION = re.compile(
    r"恋人|情侣|老婆|老公|妻子|丈夫|亲密|感情|好感|关系|亲属|闺蜜|约会|"
    r"恋爱|喜欢你|爱你|共同|我们|咱们|私聊|私信|私下|\bcp\b", re.I
)
_GENERIC_HEADER = re.compile(r"^(?:#{1,6}\s*|[【\[]).+|^[^。！？!?]{2,16}[:：]\s*$")


def _header(line: str):
    """Recognize explicit native sections; never infer sections from prose."""
    value = line.strip()
    for _ in range(3):
        value = re.sub(r"^\s*(?:#{1,6}\s*|[-*•]\s+|\d+[.、)）]\s*|[一二三四五六七八九十]+[、.]\s*)", "", value)
    value = value.replace("**", "").strip()
    value = re.sub(r"^[【\[]([^】\]]+)[】\]]\s*[:：]?\s*", r"\1：", value)
    return _SECTION.fullmatch(value)


def _clean_profile(raw: str, max_chars: int) -> str:
    selected = {section: [] for section in _SECTIONS}
    current = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = _header(line)
        if heading:
            name, inline = heading.groups()
            current = name if name in selected else None
            line = (inline or "").strip()
            if not line:
                continue
        elif _GENERIC_HEADER.fullmatch(line):
            current = None
            continue
        if current is None:
            continue
        if _META.search(line):
            # Maintenance/identity blocks have no usable continuation until an
            # explicit allowed section starts again.
            current = None
            continue
        for sentence in re.split(r"[。！？!?；;]+", line):
            sentence = re.sub(r"^\s*(?:[-*•]+|\d+[.、)）])\s*", "", sentence).strip()
            if not sentence or any(pattern.search(sentence) for pattern in (
                    _META, _INJECTION, _SENSITIVE, _EXPERIENCE, _THIRD_PERSON, _RELATION)):
                continue
            sentence = _PRIVATE.sub("[隐去]", sentence)
            sentence = re.sub(r"\s+", " ", sentence).strip(" `*_，,:：")
            meaningful = re.sub(r"[\W_]+", "", sentence.replace("[隐去]", ""))
            if len(meaningful) < 4 or sentence in selected[current]:
                continue
            selected[current].append(sentence)
    output = []
    remaining = max_chars
    for section, sentences in selected.items():
        if not sentences:
            continue
        label = "不确定信息（未核实）" if section == "不确定信息" else section
        prefix = ("\n" if output else "") + f"【{label}】\n"
        # Do not truncate midway through a sentence or its uncertainty label.
        accepted = []
        for sentence in sentences:
            piece = sentence + "。"
            cost = len(prefix if not accepted else "") + len(piece)
            if cost > remaining:
                continue
            remaining -= cost
            accepted.append(piece)
        if accepted:
            output.append(prefix + "".join(accepted))
    return "".join(output)


def _is_stale(expires_at) -> bool:
    """Unknown or malformed expiry is conservatively marked stale."""
    try:
        stamp = float(expires_at)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return True
            stamp = parsed.astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError):
            return True
    return not math.isfinite(stamp) or stamp <= time.time()


def read_native_profile(path, person_id, *, scope="verified_groups", max_chars=1200) -> dict:
    """Read the latest exact-person snapshot, with bounded internal-only text.

    Only ``global_internal`` authorizes this cache, whose original chat source is
    unavailable. A stale snapshot is marked, not silently treated as current.
    Status failures contain no profile text or filesystem/database error detail.
    """
    result = {"provider": _PROVIDER, "status": "scope_not_authorized", "text": "",
              "person_id": "", "profile_version": None, "stale": True}
    if scope != "global_internal":
        return result
    if not isinstance(person_id, str) or not _PID.fullmatch(person_id):
        result["status"] = "invalid_person_id"
        return result
    result["person_id"] = person_id
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 8000:
        result["status"] = "invalid_limit"
        return result
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            result["status"] = "invalid_path"
            return result
        if not candidate.is_file():
            result["status"] = "file_unavailable"
            return result
        uri = candidate.resolve().as_uri() + "?mode=ro"
    except (TypeError, ValueError, OSError):
        result["status"] = "invalid_path"
        return result
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        deadline = time.monotonic() + 0.25
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 100")
        connection.execute("PRAGMA trusted_schema = OFF")
        table = connection.execute(
            "SELECT type FROM sqlite_schema WHERE name = ?", ("person_profile_snapshots",)
        ).fetchone()
        if not table or table[0] != "table":
            result["status"] = "schema_unavailable"
            return result
        columns = {item[1] for item in connection.execute("PRAGMA table_info(person_profile_snapshots)")}
        if not {"person_id", "profile_version", "profile_text", "updated_at", "expires_at"} <= columns:
            result["status"] = "schema_unavailable"
            return result
        row = connection.execute(
            "SELECT profile_version, substr(profile_text, 1, 8000), expires_at "
            "FROM person_profile_snapshots WHERE person_id = ? "
            "ORDER BY profile_version DESC LIMIT 1", (person_id,)
        ).fetchone()
        if row is None:
            result["status"] = "no_profile"
            return result
        version, raw, expires_at = row
        if not isinstance(version, int) or isinstance(version, bool) or version < 0 or not isinstance(raw, str):
            result["status"] = "invalid_snapshot"
            return result
        result["profile_version"] = version
        result["stale"] = _is_stale(expires_at)
        result["text"] = _clean_profile(raw, max_chars)
        result["status"] = "ok" if result["text"] else "empty_after_filter"
        return result
    except sqlite3.Error as exc:
        # Only classify errors; profile text, paths, and exception messages are
        # deliberately never logged or returned.
        code = getattr(exc, "sqlite_errorcode", None)
        result["status"] = "read_timeout" if code in {
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT
        } else "read_unavailable"
        result["text"] = ""
        return result
    finally:
        if connection is not None:
            connection.close()
