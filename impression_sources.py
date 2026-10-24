"""Bounded SDK history and corroborated native-person memory reads.

History uses SDK APIs; the optional native-profile adapter only reads its explicit cache path.
Cross-group collection requires a trusted incoming group
context, and group enumeration explicitly excludes private/unknown platforms.
"""
from __future__ import annotations

import asyncio
import json

from .impression_messages import evidence, _INJECTION, _PRIVATE
from .impression_profile import read_native_profile, _clean_profile


def unwrap(value, field):
    if isinstance(value, dict):
        if value.get("success") is False:
            raise ValueError("SDK source rejected")
        return value.get(field)
    return value


class ImpressionSources:
    def __init__(self, ctx):
        self.ctx = ctx

    async def long_memory(self, uid, cfg):
        """An explicitly authorized global reference, never publicly quoted."""
        empty = {"provider": "memory_points", "status": "disabled", "text": ""}
        if not cfg.native_memory_enabled or cfg.native_profile_scope != "global_internal":
            return empty
        try:
            pid = unwrap(await self.ctx.person.get_id(platform="qq", user_id=uid), "person_id")
            if not isinstance(pid, str) or not pid:
                return {**empty, "status": "person_not_found"}
            platform, owner = await asyncio.gather(
                self.ctx.person.get_value(pid, "platform"), self.ctx.person.get_value(pid, "user_id"))
            if str(unwrap(platform, "value")).lower() != "qq" or str(unwrap(owner, "value")) != uid:
                return {**empty, "status": "owner_mismatch"}
            points = unwrap(await self.ctx.person.get_value(pid, "memory_points"), "value")
            if isinstance(points, str):
                points = json.loads(points)
            if not isinstance(points, list):
                return {**empty, "status": "no_memory"}
            accepted = []
            for point in points[:100]:
                if not isinstance(point, str) or len(point) > 1000:
                    continue
                parts = point.split(":", 1)
                value = parts[1].rsplit(":", 1)[0].strip() if len(parts) == 2 else point.strip()
                # Separate each point with an explicit section: malicious headings
                # in a point cannot carry section state into a different point.
                clean = _clean_profile("## 稳定了解\n- " + value, cfg.max_memory_chars)
                if clean and clean not in accepted:
                    accepted.append(clean)
                if len(accepted) >= cfg.max_native_memory_points:
                    break
            text = "\n".join(accepted)[:cfg.native_profile_max_chars]
            return {**empty, "status": "ok" if text else "empty_after_filter", "text": text}
        except Exception as exc:
            self.ctx.logger.debug("印象长期记忆读取已降级: %s", type(exc).__name__)
            return {**empty, "status": "source_unavailable"}

    async def native_profile(self, uid, cfg):
        if not cfg.native_profile_enabled:
            return {"status": "disabled", "text": ""}
        if cfg.native_profile_scope != "global_internal":
            return {"status": "scope_not_authorized", "text": ""}
        try:
            pid = unwrap(await self.ctx.person.get_id(platform="qq", user_id=uid), "person_id")
            if not isinstance(pid, str) or not pid:
                return {"status": "person_not_found", "text": ""}
            platform, owner = await asyncio.gather(
                self.ctx.person.get_value(pid, "platform"), self.ctx.person.get_value(pid, "user_id"))
            if str(unwrap(platform, "value")).lower() != "qq" or str(unwrap(owner, "value")) != uid:
                return {"status": "owner_mismatch", "text": ""}
            return await asyncio.to_thread(read_native_profile, cfg.native_profile_cache_path, pid,
                                          scope=cfg.native_profile_scope, max_chars=cfg.native_profile_max_chars)
        except Exception as exc:
            self.ctx.logger.debug("印象原生画像读取已降级: %s", type(exc).__name__)
            return {"status": "source_unavailable", "text": ""}

    async def history(self, uid, stream, start, end, processed, cfg, group_id="", min_valid_chars=6):
        streams = [stream]
        cross_group = cfg.history_scope == "all_groups" and bool(group_id)
        if cross_group:
            try:
                raw = await self.ctx.chat.get_group_streams(platform="qq")
                raw = unwrap(raw, "streams")
                if not isinstance(raw, list):
                    raise ValueError("unsupported group list")
                others = set()
                for item in raw:
                    if not isinstance(item, dict) or str(item.get("platform", "")).lower() != "qq":
                        continue
                    if item.get("chat_type") == "private" or item.get("is_group_session") is False:
                        continue
                    if not item.get("group_id"):
                        continue
                    sid = str(item.get("session_id") or item.get("stream_id") or "").strip()
                    if sid and sid != stream:
                        others.add(sid)
                streams += sorted(others)[:max(0, cfg.max_history_groups - 1)]
            except Exception as exc:
                self.ctx.logger.debug("印象群枚举降级为当前群: %s", type(exc).__name__)
        remaining = cfg.max_total_history_messages if cross_group else cfg.max_query_scan_messages
        found, texts, failures = [], set(), 0
        scanned = 0
        for index, sid in enumerate(streams):
            # Share the global read budget fairly instead of letting one busy
            # group consume it all. Model evidence remains separately capped.
            limit = min(cfg.max_query_scan_messages, max(1, remaining // (len(streams) - index)))
            if remaining <= 0:
                break
            remaining -= limit  # Reserve attempted capacity even on timeout or a quiet group.
            try:
                raw = await self.ctx.message.get_by_time_in_chat(
                    sid, start_time=float(start), end_time=float(end), limit=limit,
                    limit_mode="latest", filter_mai=True, filter_command=True)
                raw = unwrap(raw, "messages")
                if not isinstance(raw, list) or len(raw) > limit:
                    raise ValueError("invalid bounded history response")
                scanned += len(raw)
                # Legacy current-chat cursors remain valid. New cross-group IDs
                # include their stream, preventing same-ID collisions across QQ groups.
                local_processed = dict(processed) if sid == stream else {}
                for mid in processed:
                    try:
                        identity = json.loads(mid)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(identity, list) and len(identity) == 2 and identity[0] == sid:
                        local_processed[str(identity[1])] = processed[mid]
                rows = evidence(raw, uid, sid, start, end, local_processed,
                                cfg.max_chars_per_message, limit, min_valid_chars)
                for row in rows:
                    if row["text"] in texts:
                        continue
                    texts.add(row["text"])
                    if cross_group:
                        row["id"] = json.dumps([sid, row["id"]], separators=(",", ":"))
                    found.append(row)
            except Exception as exc:
                failures += 1
                self.ctx.logger.debug("印象历史源读取失败: %s", type(exc).__name__)
        if failures == len(streams):
            raise ValueError("all history sources failed")
        return sorted(found, key=lambda row: (row["time"], row["id"])), {
            "history_groups_requested": len(streams), "history_sources_failed": failures,
            "history_messages_scanned": scanned, "valid_messages_found": len(found)}

    async def memory(self, uid, samples, cfg, group_id=""):
        # Native points have no private/group provenance. Only admit a point
        # when its text is corroborated by this user's selected group evidence.
        if not cfg.native_memory_enabled or not group_id or not samples:
            return []
        try:
            pid = unwrap(await self.ctx.person.get_id(platform="qq", user_id=uid), "person_id")
            if not isinstance(pid, str) or not pid:
                return []
            platform, owner = await asyncio.gather(
                self.ctx.person.get_value(pid, "platform"), self.ctx.person.get_value(pid, "user_id"))
            if str(unwrap(platform, "value")).lower() != "qq" or str(unwrap(owner, "value")) != uid:
                return []
            points = unwrap(await self.ctx.person.get_value(pid, "memory_points"), "value")
            if isinstance(points, str):
                points = json.loads(points)
            if not isinstance(points, list):
                return []
            accepted = []
            for point in points[:100]:
                if not isinstance(point, str) or len(point) > 1000:
                    continue
                parts = point.split(":", 1)
                text = parts[1].rsplit(":", 1)[0].strip() if len(parts) == 2 else point.strip()
                if len(text) < 6 or _INJECTION.search(text):
                    continue
                text = _PRIVATE.sub("[隐去]", text)[:cfg.max_memory_chars]
                # Exact corroboration, no model-based inference and no extra LLM call.
                if text and any(text in row["text"] for row in samples) and text not in accepted:
                    accepted.append(text)
                if len(accepted) >= cfg.max_native_memory_points:
                    break
            return accepted
        except Exception as exc:
            self.ctx.logger.debug("印象原生记忆读取已降级: %s", type(exc).__name__)
            return []
