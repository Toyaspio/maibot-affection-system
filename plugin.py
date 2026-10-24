"""好感度系统插件 — MaiBot SDK v2

为 MaiBot 角色扮演提供好感度追踪与长期记忆功能。

功能特性：
    - 记录不同用户与角色的好感度值（范围 -20 ~ 100）
    - 六级好感度划分：不喜 / 正常 / 熟悉 / 亲近 / 亲密 / 思慕
    - 根据角色性格和用户行为动态调整好感度
    - 不同好感度等级对应不同的回应风格提示
    - 好感度数据持久化存储于插件数据目录
    - 支持 WebUI 可视化配置与管理：
        * 「好感度管理」配置节以可编辑表格展示各用户好感度
        * 在 WebUI 中可直接查看并手动修改好感度，保存后立即生效
        * 锁定条目将禁止 LLM 自动调整
        * 运行时变化会自动同步回配置表，保证界面展示最新数值
    - 提供 Tool 供 LLM Agent 查询和调整好感度
    - 提供 /affection 命令供用户查询自己的好感度

用法示例：
    - LLM 调用 query_affection 工具：获取指定用户的好感度及回应风格提示
    - LLM 调用 adjust_affection 工具：根据交互质量调整好感度
    - 用户发送 /affection 命令：查看自己与角色的好感度
    - 在 WebUI 插件配置页的「好感度管理」表格中：查看/新增/修改/删除各用户好感度
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import tomlkit

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import AffectionConfig
from .impression_manager import ImpressionManager
from .impression_messages import QueryRegistry, own_text, is_self_query, self_query_message_text

# ════════════════════════════════════════════════════════════════
# 好感度等级定义 (下界含, 上界不含, 等级名称, 等级描述)
# 区间为左闭右开 [min, max)：边界值归属上一个等级，避免 0/10/20 等
# 边界值被同时匹配两个等级。仅最后一个等级（思慕）右端含 100。
# ════════════════════════════════════════════════════════════════

AFFECTION_LEVELS: list[tuple[int, int, str, str]] = [
    (-20, 0, "不喜", "对你没什么好感，态度冷淡疏远"),
    (0, 10, "正常", "普普通通的认识关系，礼貌相待"),
    (10, 20, "熟悉", "已经比较熟悉了，能自然交流"),
    (20, 40, "亲近", "关系亲近，愿意多聊天分享"),
    (40, 70, "亲密", "亲密无间，很信任你"),
    (70, 100, "思慕", "对你有着特别的依恋与情感"),
]

# ════════════════════════════════════════════════════════════════
# 性格类型对应的好感度倍率映射
# ════════════════════════════════════════════════════════════════

PERSONALITY_MULTIPLIERS: dict[str, float] = {
    "默认": 1.0,
    "傲娇": 0.6,
    "温柔": 1.0,
    "高冷": 0.4,
    "开朗": 1.5,
    "毒舌": 0.7,
}


def _get_affection_level_info(affection_value: float) -> dict[str, Any]:
    """根据好感度数值获取等级信息。

    Args:
        affection_value: 好感度数值

    Returns:
        包含等级名称、描述、范围的字典
    """
    clamped = max(-20.0, min(100.0, affection_value))
    last_index = len(AFFECTION_LEVELS) - 1
    for index, (min_val, max_val, name, description) in enumerate(AFFECTION_LEVELS):
        # 左闭右开 [min, max)；最后一个区间右端含 100
        in_range = min_val <= clamped < max_val if index < last_index else min_val <= clamped <= max_val
        if in_range:
            # 计算在该等级内的进度百分比
            progress = (clamped - min_val) / (max_val - min_val) if max_val > min_val else 0
            return {
                "value": round(clamped, 1),
                "level_name": name,
                "level_description": description,
                "level_min": min_val,
                "level_max": max_val,
                "level_progress": round(progress * 100, 1),
            }
    # 兜底：理论上不会到这里
    return {
        "value": round(clamped, 1),
        "level_name": "未知",
        "level_description": "无法确定",
        "level_min": -20,
        "level_max": 100,
        "level_progress": 0,
    }


class AffectionPlugin(MaiBotPlugin):
    """好感度系统插件主类。

    负责好感度数据的加载、持久化、查询和修改。
    """

    config_model = AffectionConfig

    def __init__(self) -> None:
        super().__init__()
        # 好感度数据：{ user_id: { affection: float, history: list, last_updated: str, daily_count: int, last_modified_date: str } }
        self._affection_data: dict[str, dict[str, Any]] = {}
        # 冷却时间记录：{ user_id: float (timestamp) }
        self._cool_down_map: dict[str, float] = {}
        # 数据文件路径（在 on_load 中初始化）
        self._data_file_path: str = ""
        # 文件写入锁，避免并发写入冲突
        self._file_lock: asyncio.Lock = asyncio.Lock()
        # 同步配置表时的去重记录：避免重复写入 config.toml
        self._last_config_sync_key: str = ""
        # 会话 → 最近发言用户映射缓存：{ session_id: user_id }
        # 群聊流的 user_id 为 None，无法从 stream 直接反查；该缓存由
        # before_request 每轮解析成功时刷新，代表「当前轮发言人」。
        self._session_user_map: dict[str, str] = {}
        # 接收索引：{ session_id: deque[(message_id, user_id)] }
        # 在 chat.receive.after_process 时建立（消息到达即记录，不依赖入库时机），
        # 供 before_request 按 prompt 中的 msg_id 精确反查当前发言人。
        self._session_message_index: dict[str, deque[tuple[str, str]]] = {}
        # 自动评价后台任务：{ session_id: asyncio.Task }
        # 由 chat.receive.after_process 调度，任务内调用 LLM 对互动做上下文评价，
        # 不阻塞消息接收链路；on_unload 时统一取消。
        self._evaluation_tasks: dict[str, asyncio.Task] = {}
        # 精确好感度问询索引：仅记录当前真实消息 ID，对查询失败或身份未知不猜数值。
        # key=(session_id, msg_id), value=是否明确在询问当前发言人自身的好感度。
        self._affection_numeric_requests: dict[tuple[str, str], bool] = {}
        # 只用于工具/身份核实失败时：轮换不同表达策略，避免固定台词反复出现。
        self._affection_fallback_strategies: list[str] = []
        self._last_affection_fallback_strategy: str = ""
        self._impression_manager: ImpressionManager | None = None
        self._impression_queries = QueryRegistry()
        self._impression_bot_ids: set[str] = set()
        self._impression_bot_lookup_at = 0.0
        self._impression_bot_lookup_lock = asyncio.Lock()

    # ════════════════════════════════════════════════════════════════
    # 生命周期方法
    # ════════════════════════════════════════════════════════════════

    async def on_load(self) -> None:
        """插件加载时初始化数据文件并读取已有数据。"""
        # 确定数据文件路径
        data_dir = self.ctx.paths.data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._data_file_path = os.path.join(data_dir, "affection_data.json")
        # 迁移旧版插件 ID 的数据目录（避免改 ID 后历史数据丢失）
        await self._migrate_legacy_data_dir()
        # 加载已有数据
        await self._load_data()
        # 迁移历史昵称记录到 QQ 号（避免同一人被拆成两条记录）
        await self._migrate_nickname_records()
        # 执行衰减检查
        await self._apply_decay_check()
        # 将运行时数据同步到 config.toml，让 WebUI 管理表展示最新好感度
        await self._sync_runtime_to_config()
        # Independent subsystem: a failure must never prevent affection startup.
        try:
            self._impression_manager = ImpressionManager(
                self.ctx, Path(data_dir) / "impression_data.json", self._impression_config)
            await self._impression_manager.load()
            if self._impression_config() and self._impression_config().enabled:
                await self._refresh_impression_bot_ids()
        except Exception as exc:
            self._impression_manager = None
            self.ctx.logger.warning("印象模块初始化已降级: %s", type(exc).__name__)
        self.ctx.logger.info(
            "好感度系统插件已加载 (角色: %s, 已记录 %d 位用户的好感度)",
            self.config.personality.character_name if self.config else "未知",
            len(self._affection_data),
        )

    async def on_unload(self) -> None:
        """插件卸载时取消后台任务、写入磁盘，并将运行时数据同步回配置。"""
        if self._impression_manager is not None:
            try:
                await self._impression_manager.close()
            except Exception as exc:
                self.ctx.logger.warning("印象模块卸载已降级: %s", type(exc).__name__)
        for session_id, task in list(self._evaluation_tasks.items()):
            task.cancel()
            self._evaluation_tasks.pop(session_id, None)
        await self._sync_runtime_to_config()
        await self._save_data()
        self.ctx.logger.info("好感度系统插件已卸载，数据已保存")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        """配置热更新时处理。

        当用户通过 WebUI 修改「好感度管理」表后，Host 会把新配置写回
        config.toml 并触发本回调。这里把 WebUI 中手动修改的好感度
        同步到运行时数据（即「修改生效」的方向）。
        """
        del version
        self.ctx.logger.info(
            "好感度系统配置已热更新, scope=%s", scope
        )
        if scope == "self":
            await self._sync_config_to_runtime(config_data)

    # ════════════════════════════════════════════════════════════════
    # 数据持久化（JSON 文件方式）
    # ════════════════════════════════════════════════════════════════

    async def _migrate_legacy_data_dir(self) -> None:
        """将旧版插件 ID 的数据目录迁移到当前数据目录。

        早期版本插件 ID 首字母大写（Feyumy.affection-system），Host 据此分配的
        数据目录与当前小写 ID（feyumy.affection-system）不同。这里在首次以新 ID
        加载时，把旧目录中的 affection_data.json 复制到新目录，避免历史好感度丢失。
        仅在当前数据文件不存在且旧数据存在时执行，迁移后不做删除，保证安全。
        """
        if not self._data_file_path:
            return
        if os.path.exists(self._data_file_path):
            return
        data_dir = os.path.dirname(self._data_file_path)
        legacy_dir = os.path.join(os.path.dirname(data_dir), "Feyumy.affection-system")
        legacy_file = os.path.join(legacy_dir, "affection_data.json")
        if not os.path.exists(legacy_file):
            return
        try:
            os.makedirs(data_dir, exist_ok=True)
            loop = asyncio.get_running_loop()

            def _copy() -> None:
                with open(legacy_file, "r", encoding="utf-8") as f:
                    content = f.read()
                with open(self._data_file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            await loop.run_in_executor(None, _copy)
            self.ctx.logger.info("已从旧版数据目录迁移好感度数据: %s", legacy_dir)
        except Exception as e:
            self.ctx.logger.warning("迁移旧版好感度数据失败: %s", e)

    async def _load_data(self) -> None:
        """从磁盘加载好感度数据。"""
        if not self._data_file_path:
            return
        try:
            if os.path.exists(self._data_file_path):
                loop = asyncio.get_running_loop()

                def _read() -> dict[str, dict[str, Any]]:
                    with open(self._data_file_path, "r", encoding="utf-8") as f:
                        return json.load(f)

                data = await loop.run_in_executor(None, _read)
                if isinstance(data, dict):
                    self._affection_data = data
                    self.ctx.logger.debug("已加载 %d 条好感度记录", len(data))
                else:
                    self._affection_data = {}
            else:
                self._affection_data = {}
                self.ctx.logger.debug("好感度数据文件不存在，将创建新文件")
        except json.JSONDecodeError as e:
            self.ctx.logger.warning("好感度数据文件损坏，将重置: %s", e)
            self._affection_data = {}
        except Exception as e:
            self.ctx.logger.error("加载好感度数据失败: %s", e)
            self._affection_data = {}

    async def _migrate_nickname_records(self) -> None:
        """把历史遗留的昵称记录迁移/合并到对应的 QQ 号记录。

        早期版本在群聊中可能用昵称作为 user_id 存储，私聊中用 QQ 号存储，
        导致同一个人被拆成两条记录。这里在加载后尝试把昵称记录解析为 QQ 号，
        若 QQ 号记录已存在则合并，否则将昵称键替换为 QQ 号键。

        昵称无法解析时保留原记录，避免数据丢失。
        """
        # 找出非纯数字的 user_id（视为昵称记录）
        nickname_keys = [
            uid for uid in self._affection_data
            if str(uid or "").strip() and not str(uid).strip().isdigit()
        ]
        if not nickname_keys:
            return

        migrated = 0
        for nick in nickname_keys:
            try:
                person_id = await self.ctx.person.get_id_by_name(nick)
            except Exception as e:
                self.ctx.logger.debug("迁移昵称记录失败 (%s): %s", nick, e)
                continue
            if not person_id:
                continue
            person_id_str = str(person_id).strip()
            if not person_id_str:
                continue
            if person_id_str == nick:
                continue

            nick_record = self._affection_data.pop(nick)
            if person_id_str in self._affection_data:
                # 已存在 QQ 号记录：合并（取好感度较高者，历史合并）
                existing = self._affection_data[person_id_str]
                old_affection = existing.get("affection", 0.0)
                nick_affection = nick_record.get("affection", 0.0)
                # 历史记录合并
                merged_history = list(existing.get("history", [])) + list(
                    nick_record.get("history", [])
                )
                if len(merged_history) > 200:
                    merged_history = merged_history[-200:]
                existing["history"] = merged_history
                # 记录一次迁移事件
                existing["history"].append({
                    "time": datetime.now().isoformat(),
                    "change": 0.0,
                    "actual_change": 0.0,
                    "type": "migration",
                    "description": f"合并昵称记录「{nick}」到 QQ 号 {person_id_str}",
                    "old_value": round(old_affection, 1),
                    "new_value": round(old_affection, 1),
                })
                # 取两者中较高好感度，避免历史拆分后丢失
                existing["affection"] = max(old_affection, nick_affection)
                self.ctx.logger.info(
                    "合并昵称记录: %s -> %s (好感度取较高值 %.1f)",
                    nick, person_id_str, existing["affection"],
                )
            else:
                # 直接替换键为 QQ 号
                self._affection_data[person_id_str] = nick_record
                self.ctx.logger.info(
                    "迁移昵称记录: %s -> %s", nick, person_id_str
                )
            migrated += 1

        if migrated > 0:
            await self._save_data()
            self.ctx.logger.info("昵称记录迁移完成: 共 %d 条", migrated)

    async def _save_data(self) -> None:
        """将好感度数据写入磁盘（带锁保护）。"""
        if not self._data_file_path:
            return
        async with self._file_lock:
            try:
                loop = asyncio.get_running_loop()

                def _write() -> None:
                    # 先写入临时文件，成功后再替换，避免写入中断导致文件损坏
                    tmp_path = self._data_file_path + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(self._affection_data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, self._data_file_path)

                await loop.run_in_executor(None, _write)
                self.ctx.logger.debug("好感度数据已保存 (%d 条记录)", len(self._affection_data))
            except Exception as e:
                self.ctx.logger.error("保存好感度数据失败: %s", e)

    # ════════════════════════════════════════════════════════════════
    # WebUI 可视化配置双向同步
    # ════════════════════════════════════════════════════════════════
    # 设计说明：
    #   - 「好感度管理」配置节（user_management.managed_users）在 WebUI 中
    #     渲染为可编辑表格，是可视化查看/修改的界面。
    #   - 运行时好感度数据存于 affection_data.json（唯一事实来源）。
    #   - 双向同步：
    #       1. 运行时 → config.toml：LLM 通过 Tool 调整好感度后，把最新数值
    #          写回自身 config.toml，保证 WebUI 表格展示最新数据。
    #       2. config.toml → 运行时：用户在 WebUI 表格中手动修改并保存后，
    #          Host 触发 on_config_update，把修改应用到运行时数据。
    #   - 同步目标路径：plugin.py 同目录的 config.toml（即插件自身配置）。

    def _get_config_path(self) -> Path:
        """返回插件自身 config.toml 的路径。

        Returns:
            Path: config.toml 绝对路径
        """
        return Path(__file__).resolve().parent / "config.toml"

    async def _sync_runtime_to_config(self) -> None:
        """将运行时好感度数据同步回 config.toml 的管理表。

        仅在开启了「自动同步到管理表」且数据确实发生变化时才会写文件，
        避免反复触发配置热更新。
        """
        if self.config is None:
            return
        try:
            if not self.config.user_management.sync_to_config:
                return
        except AttributeError:
            return

        # 构造当前运行时数据的同步快照
        entries = []
        for user_id, record in self._affection_data.items():
            entries.append({
                "user_id": user_id,
                "affection": round(float(record.get("affection", 0.0) or 0.0), 1),
                "locked": bool(record.get("locked", False)),
                "note": str(record.get("note", "") or ""),
            })
            # 标记该用户已进入配置管理表，供 _sync_config_to_runtime 清理逻辑使用
            record["managed_in_config"] = True
        # 按好感度降序排列，方便在 WebUI 中浏览
        entries.sort(key=lambda e: e["affection"], reverse=True)

        # 计算同步键，用于跳过重复写入
        sync_key = json.dumps(entries, ensure_ascii=False, sort_keys=True)
        if sync_key == self._last_config_sync_key:
            return
        self._last_config_sync_key = sync_key

        try:
            config_path = self._get_config_path()
            if not config_path.exists():
                return

            loop = asyncio.get_running_loop()

            def _write_sync() -> None:
                # 使用 tomlkit 保留原有注释与格式，仅更新 user_management 节
                with open(config_path, "r", encoding="utf-8") as f:
                    document = tomlkit.parse(f.read())
                # 确保 user_management 节存在
                um = document.get("user_management")
                if um is None or not isinstance(um, dict):
                    um = tomlkit.table()
                    document["user_management"] = um
                # 将好感度条目写入配置表
                user_table = tomlkit.aot()
                for entry in entries:
                    row = tomlkit.table()
                    row["user_id"] = entry["user_id"]
                    row["affection"] = entry["affection"]
                    row["locked"] = entry["locked"]
                    row["note"] = entry["note"]
                    user_table.append(row)
                um["managed_users"] = user_table
                # 原子写入
                tmp_path = config_path.with_suffix(".toml.tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(tomlkit.dumps(document))
                os.replace(tmp_path, config_path)

            await loop.run_in_executor(None, _write_sync)
            self.ctx.logger.debug("好感度已同步到配置表 (%d 位用户)", len(entries))
        except Exception as e:
            self.ctx.logger.warning("同步好感度到配置表失败: %s", e)

    async def _sync_config_to_runtime(self, config_data: dict[str, object]) -> None:
        """将 WebUI 中手动修改的好感度配置同步到运行时数据。

        用户在 WebUI 表格中修改/新增/删除好感度并保存后，Host 会触发
        on_config_update，这里把改动应用进运行时数据，并持久化。

        Args:
            config_data: Host 传来的最新插件配置字典
        """
        if not isinstance(config_data, dict):
            return
        um = config_data.get("user_management")
        if not isinstance(um, dict):
            return
        managed_users = um.get("managed_users")
        if not isinstance(managed_users, list):
            return

        # 记录本配置表中出现的用户 ID，用于清理被删除的条目
        active_user_ids: set[str] = set()
        changed = False

        for entry in managed_users:
            if not isinstance(entry, dict):
                continue
            user_id = str(entry.get("user_id", "") or "").strip()
            if not user_id:
                continue
            active_user_ids.add(user_id)

            record = self._get_user_record(user_id)
            # 应用 WebUI 中设置的好感度值
            try:
                new_affection = float(entry.get("affection", 0.0) or 0.0)
            except (TypeError, ValueError):
                new_affection = 0.0
            new_affection = max(-20.0, min(100.0, new_affection))

            if abs(record.get("affection", 0.0) - new_affection) > 1e-9:
                # 记录一条手动修改的历史
                history = record.get("history", [])
                history.append({
                    "time": datetime.now().isoformat(),
                    "change": round(new_affection - record.get("affection", 0.0), 1),
                    "actual_change": round(new_affection - record.get("affection", 0.0), 1),
                    "type": "manual",
                    "description": "WebUI 手动修改好感度",
                    "old_value": round(record.get("affection", 0.0), 1),
                    "new_value": round(new_affection, 1),
                })
                if len(history) > 200:
                    history = history[-200:]
                record["history"] = history
                record["affection"] = new_affection
                changed = True

            # 应用锁定状态
            locked = bool(entry.get("locked", False))
            if record.get("locked") != locked:
                record["locked"] = locked
                changed = True

            # 应用备注
            note = str(entry.get("note", "") or "")
            if record.get("note") != note:
                record["note"] = note
                changed = True

            record["last_updated"] = datetime.now().isoformat()

        # 清理管理表中已删除的用户（仅删除由该表管理过的记录）
        removed_ids = [
            user_id for user_id in list(self._affection_data.keys())
            if user_id not in active_user_ids
            and self._affection_data[user_id].get("managed_in_config", False)
        ]
        for user_id in removed_ids:
            self._affection_data.pop(user_id, None)
            changed = True

        if changed:
            await self._save_data()
            self.ctx.logger.info(
                "已应用 WebUI 手动好感度修改 (%d 位用户)", len(active_user_ids)
            )

    # ════════════════════════════════════════════════════════════════
    # 好感度查询与控制逻辑
    # ════════════════════════════════════════════════════════════════

    def _get_user_record(self, user_id: str) -> dict[str, Any]:
        """获取指定用户的好感度记录，不存在则返回默认记录。

        Args:
            user_id: 用户唯一标识

        Returns:
            用户好感度记录字典
        """
        if user_id not in self._affection_data:
            initial = 10.0
            if self.config is not None:
                try:
                    initial = float(self.config.affection_rule.initial_affection or 10.0)
                except (AttributeError, TypeError, ValueError):
                    initial = 10.0
            initial = max(-20.0, min(100.0, initial))
            self._affection_data[user_id] = {
                "affection": initial,
                "history": [],
                "last_updated": datetime.now().isoformat(),
                "daily_count": 0,
                "last_modified_date": datetime.now().strftime("%Y-%m-%d"),
                "locked": False,
                "note": "",
                "managed_in_config": False,
            }
        return self._affection_data[user_id]

    async def _resolve_user_id(
        self,
        requested_user_id: str,
        stream_id: str = "",
        **kwargs: Any,
    ) -> str:
        """将互动对象统一解析为稳定、唯一的 QQ 号标识。

        背景：群聊中 LLM 可能通过昵称识别互动对象，私聊中可能通过 QQ 号识别，
        导致同一个人被当成两个人分别记录。这里统一识别口径。

        解析优先级：
            1. 运行时注入的 user_id（消息元数据中的真实平台 ID，即 QQ 号）
            2. 显式传入且为纯数字的 user_id（视为 QQ 号）
            3. 尝试通过 person 能力把昵称解析为人物 ID
            4. 当前轮发言人（before_request 已按 prompt 锚定刷新）→ 接收索引
               最近发言人 → 消息库最近消息（群聊关键：Tool 调用上下文在群聊
               中不携带 user_id）
            5. 兜底：原样返回

        Args:
            requested_user_id: LLM 传入的 user_id 参数（可能是昵称或 QQ 号）
            stream_id: 当前聊天流 ID（由调用方传入，用于群聊反查）
            **kwargs: 运行时注入的上下文参数（含权威 user_id 等）

        Returns:
            归一化后的稳定用户标识（优先为 QQ 号）
        """
        # 1. 运行时注入的 user_id 是消息元数据中的真实平台 ID，优先采用。
        #    注意：当 Tool 方法签名含 user_id 参数时，注入值会绑定到命名参数
        #    而非 kwargs，因此此处同时兼容两种传递方式。
        injected = str(kwargs.get("user_id", "") or "").strip()
        if injected and injected.isdigit():
            self.ctx.logger.debug("使用运行时注入的用户标识: %s", injected)
            return injected

        # 2. 显式传入的纯数字参数直接视为 QQ 号（可能是注入绑定值或 LLM 填的）
        requested = str(requested_user_id or "").strip()
        if requested.isdigit():
            sid = str(stream_id or kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
            speaker = self._lookup_message_speaker(sid, requested) if sid else ""
            if speaker:
                self.ctx.logger.warning(
                    "好感度参数为消息ID，已转换为QQ: %s -> %s",
                    requested, speaker
                )
                return speaker
            return requested

        # 3. 尝试把昵称解析为人物 ID（人物 ID 通常为纯数字，即平台 user_id）
        if requested:
            try:
                person_id = await self.ctx.person.get_id_by_name(requested)
                if person_id:
                    person_id_str = str(person_id).strip()
                    if person_id_str.isdigit():
                        self.ctx.logger.debug(
                            "已通过昵称解析用户标识: %s -> %s", requested, person_id_str
                        )
                        return person_id_str
                    # 人物 ID 非纯数字时仍可安全使用（唯一且稳定）
                    return person_id_str
            except Exception as e:
                self.ctx.logger.debug("昵称解析失败 (%s): %s", requested, e)

        # 4. 群聊兜底：使用当前会话已解析的发言人。
        #    群聊会话按群维度建立（session_id 只含群号），Tool 调用上下文里的
        #    chat_stream.user_id 恒为空。优先使用 before_request 每轮解析并
        #    刷新的「当前轮发言人」缓存，其次为接收索引中的最近发言人，
        #    最后退回消息库最近消息反查。
        resolved_stream_id = str(stream_id or "").strip() or str(
            kwargs.get("stream_id") or kwargs.get("chat_id") or ""
        ).strip()
        if resolved_stream_id:
            # 4.1 当前轮发言人缓存（before_request 已按 prompt 锚定刷新）
            cached_user = self._session_user_map.get(resolved_stream_id, "")
            if cached_user:
                self.ctx.logger.debug(
                    "使用当前轮发言人缓存: %s -> %s", resolved_stream_id, cached_user
                )
                return cached_user

            # 4.2 接收索引最近发言人（近似）
            indexed_user = self._lookup_latest_session_speaker(resolved_stream_id)
            if indexed_user:
                self.ctx.logger.debug(
                    "使用接收索引最近发言人: %s -> %s", resolved_stream_id, indexed_user
                )
                return indexed_user

            # 4.3 消息库最近消息反查（兜底）
            try:
                session_user = await self._resolve_user_id_from_recent_messages(
                    resolved_stream_id
                )
                if session_user:
                    self.ctx.logger.debug(
                        "已通过会话最近消息解析用户标识: %s -> %s",
                        resolved_stream_id,
                        session_user,
                    )
                    return session_user
            except Exception as e:
                self.ctx.logger.debug(
                    "会话最近消息解析失败 (%s): %s", resolved_stream_id, e
                )

        # 5. 兜底：返回注入值或原参数
        return injected or requested

    def _check_cool_down(self, user_id: str) -> bool:
        """检查用户是否处于好感度变更冷却期。

        Args:
            user_id: 用户唯一标识

        Returns:
            True 表示在冷却期（不可变更），False 表示不在冷却期（可变更）
        """
        if self.config is None:
            return False
        cool_down = self.config.affection_rule.cool_down_seconds
        now = time.time()
        last_time = self._cool_down_map.get(user_id, 0.0)
        return (now - last_time) < cool_down

    def _check_daily_limit(self, user_id: str) -> bool:
        """检查用户今日好感度变更是否已达上限。

        Args:
            user_id: 用户唯一标识

        Returns:
            True 表示已达上限，False 表示未达上限
        """
        if self.config is None:
            return False
        record = self._get_user_record(user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        last_date = record.get("last_modified_date", "")
        daily_count = record.get("daily_count", 0)
        if last_date != today:
            # 新的一天，重置计数（含负面螺旋计数）
            record["daily_count"] = 0
            record["daily_negative_count"] = 0
            record["last_modified_date"] = today
            return False
        limit = self.config.affection_rule.daily_limit
        return daily_count >= limit

    def _apply_personality_multiplier(self, change_value: float) -> float:
        """根据配置的性格类型计算实际好感度变化值。

        Args:
            change_value: 原始变化值

        Returns:
            应用性格倍率后的变化值
        """
        if self.config is None:
            return change_value
        personality = self.config.personality.personality_type
        multiplier = PERSONALITY_MULTIPLIERS.get(personality, 1.0)
        # 再乘上用户配置的倍率
        cfg_multiplier = self.config.personality.affection_gain_multiplier
        return change_value * multiplier * cfg_multiplier

    def _apply_relationship_damping(
        self,
        change_value: float,
        record: dict[str, Any],
        interaction_type: str,
    ) -> float:
        """根据当前关系状态对好感度变化施加阻尼。

        两种阻尼（均只作用于负面扣分）：
            1. 高位阻尼：好感度较高（关系亲密）时，单次扣分按系数缩小，
               亲密关系经得起小摩擦；
            2. 负面螺旋阻尼：同一天连续负面调整超过上限后，后续扣分按系数
               逐次递减，防止短时间内好感度断崖式下跌。

        Args:
            change_value: 应用性格倍率后的变化值
            record: 用户好感度记录
            interaction_type: 互动类型（negative 才应用阻尼）

        Returns:
            应用关系阻尼后的变化值
        """
        if self.config is None:
            return change_value
        if change_value >= 0 or interaction_type != "negative":
            return change_value

        adjusted = change_value
        cfg = self.config.affection_rule
        affection = float(record.get("affection", 0.0) or 0.0)

        # 1. 高位阻尼
        if affection >= cfg.high_affection_negative_damper_threshold:
            adjusted *= cfg.high_affection_negative_damper_multiplier

        # 2. 负面螺旋阻尼（daily_negative_count 已在调用前自增，含本次）
        negative_count = int(record.get("daily_negative_count", 0) or 0)
        if negative_count > cfg.negative_spiral_limit:
            adjusted *= cfg.negative_spiral_decay ** (negative_count - cfg.negative_spiral_limit)

        return adjusted

    async def _update_affection(
        self,
        user_id: str,
        change_value: float,
        interaction_type: str,
        description: str,
    ) -> dict[str, Any]:
        """更新指定用户的好感度。

        包含冷却检查、每日上限检查、历史记录追加和数据保存。

        Args:
            user_id: 用户唯一标识
            change_value: 好感度变化值（正值增加，负值减少）
            interaction_type: 互动类型（positive/neutral/negative/special）
            description: 互动简述

        Returns:
            更新结果字典
        """
        # 参数校验
        if not user_id or not user_id.strip():
            return {"success": False, "error": "用户 ID 不能为空"}

        # 冷却检查
        if self._check_cool_down(user_id):
            remaining = self.config.affection_rule.cool_down_seconds - (
                time.time() - self._cool_down_map.get(user_id, 0.0)
            )
            return {
                "success": False,
                "error": f"变更冷却中，请等待 {max(0, int(remaining))} 秒后再试",
            }

        # 每日上限检查（仅统计增加/减少的变更，查询不在此列）
        if change_value != 0 and self._check_daily_limit(user_id):
            return {
                "success": False,
                "error": "今日好感度变更次数已达上限",
            }

        # 获取用户记录
        record = self._get_user_record(user_id)

        # 应用性格倍率
        adjusted_change = self._apply_personality_multiplier(change_value)

        # 负面互动先累计当天负面计数（供负面螺旋阻尼使用），再应用关系阻尼
        if interaction_type == "negative":
            record["daily_negative_count"] = record.get("daily_negative_count", 0) + 1
        adjusted_change = self._apply_relationship_damping(adjusted_change, record, interaction_type)

        # 计算新的好感度值
        old_affection = record["affection"]
        new_affection = max(-20.0, min(100.0, old_affection + adjusted_change))
        actual_change = round(new_affection - old_affection, 1)

        # 更新记录
        record["affection"] = new_affection
        record["last_updated"] = datetime.now().isoformat()
        record["daily_count"] = record.get("daily_count", 0) + 1
        record["last_modified_date"] = datetime.now().strftime("%Y-%m-%d")

        # 追加历史记录（最多保留 200 条）
        history_entry = {
            "time": datetime.now().isoformat(),
            "change": round(adjusted_change, 1),
            "actual_change": actual_change,
            "type": interaction_type,
            "description": description,
            "old_value": round(old_affection, 1),
            "new_value": round(new_affection, 1),
        }
        history = record.get("history", [])
        history.append(history_entry)
        # 限制历史记录数量
        if len(history) > 200:
            history = history[-200:]
        record["history"] = history

        # 记录冷却时间
        self._cool_down_map[user_id] = time.time()

        # 持久化保存
        await self._save_data()

        # 将最新好感度同步回 config.toml 管理表（供 WebUI 展示）
        await self._sync_runtime_to_config()

        # 获取旧等级和新等级信息
        old_level = _get_affection_level_info(old_affection)
        new_level = _get_affection_level_info(new_affection)
        level_changed = old_level["level_name"] != new_level["level_name"]

        # 生成变更摘要
        change_direction = "增加" if actual_change > 0 else ("减少" if actual_change < 0 else "不变")
        result = {
            "success": True,
            "user_id": user_id,
            "old_affection": round(old_affection, 1),
            "new_affection": round(new_affection, 1),
            "actual_change": actual_change,
            "change_direction": change_direction,
            "current_level": new_level["level_name"],
            "current_level_description": new_level["level_description"],
            "level_changed": level_changed,
        }
        if level_changed:
            result["old_level"] = old_level["level_name"]
            result["level_change_message"] = (
                f"好感度等级从「{old_level['level_name']}」变为「{new_level['level_name']}」"
            )

        self.ctx.logger.info(
            "好感度变更: user=%s, %s %.1f (%.1f → %.1f), 等级=%s, 原因=%s",
            user_id,
            change_direction,
            actual_change,
            old_affection,
            new_affection,
            new_level["level_name"],
            description,
        )

        return result

    async def _apply_decay_check(self) -> None:
        """检查并应用好感度衰减。

        如果启用了衰减功能，遍历所有用户记录，对超过衰减触发天数未互动的用户降低好感度。
        """
        if self.config is None:
            return
        if not self.config.personality.affection_decay_enabled:
            return

        decay_days = self.config.personality.affection_decay_days
        decay_rate = self.config.personality.affection_decay_rate
        now = datetime.now()
        decay_threshold = now - timedelta(days=decay_days)
        decayed_count = 0

        for user_id, record in list(self._affection_data.items()):
            try:
                # 已锁定的用户不受自然衰减影响
                if record.get("locked", False):
                    continue
                last_updated_str = record.get("last_updated", "")
                if not last_updated_str:
                    continue
                last_updated = datetime.fromisoformat(last_updated_str)
                if last_updated > decay_threshold:
                    continue
                # 已超过触发生天数，应用衰减
                old_val = record["affection"]
                new_val = max(-20.0, old_val - decay_rate)
                if new_val < old_val:
                    record["affection"] = new_val
                    record["last_updated"] = now.isoformat()
                    # 追加历史
                    history = record.get("history", [])
                    history.append({
                        "time": now.isoformat(),
                        "change": round(new_val - old_val, 1),
                        "actual_change": round(new_val - old_val, 1),
                        "type": "decay",
                        "description": f"超过 {decay_days} 天未互动，好感度自然衰减",
                        "old_value": round(old_val, 1),
                        "new_value": round(new_val, 1),
                    })
                    if len(history) > 200:
                        history = history[-200:]
                    record["history"] = history
                    decayed_count += 1
            except (ValueError, KeyError, TypeError):
                continue

        if decayed_count > 0:
            await self._save_data()
            await self._sync_runtime_to_config()
            self.ctx.logger.info("好感度衰减检查完成: %d 位用户好感度已衰减", decayed_count)

    def _get_response_hint(self, affection_value: float) -> str:
        """根据好感度值获取对应的回应风格提示。

        Args:
            affection_value: 好感度数值

        Returns:
            回应风格提示字符串
        """
        level_info = _get_affection_level_info(affection_value)
        level_name = level_info["level_name"]
        if self.config is None:
            return "请根据你的角色设定自然地回应"

        cfg = self.config.response
        hint_map = {
            "不喜": cfg.dislike_response_hint,
            "正常": cfg.normal_response_hint,
            "熟悉": cfg.familiar_response_hint,
            "亲近": cfg.close_response_hint,
            "亲密": cfg.intimate_response_hint,
            "思慕": cfg.adore_response_hint,
        }
        return hint_map.get(level_name, "请根据你的角色设定自然地回应")

    # ════════════════════════════════════════════════════════════════
    # Tool: 查询好感度
    # ════════════════════════════════════════════════════════════════

    @Tool(
        "query_affection",
        description=(
            "查询指定用户与角色的当前好感度。返回好感度数值、等级名称、等级描述、"
            "在该等级内的进度百分比、最近几次变化记录、以及根据当前等级提供给角色的回应风格提示。"
            "在角色回复用户之前应调用此工具，以确定合适的语气和态度。"
            "当用户询问「你对我感觉怎样」「我们关系如何」等涉及关系评价的问题时也应调用。"
            "注意：通常无需填写 user_id，运行时会自动注入当前互动对象的 QQ 号；"
            "如需查询指定对象请填写其 QQ 号（不要填昵称）。"
        ),
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="要查询好感度的用户 QQ 号。通常可留空，运行时会自动注入当前互动对象的 QQ 号。",
                required=False,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，由运行时自动注入，无需手动填写",
                required=False,
            ),
        ],
    )
    async def query_affection(
        self,
        user_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """查询指定用户的好感度。

        Args:
            user_id: 用户 QQ 号或昵称（可留空，运行时注入当前对象；会统一解析为 QQ 号）
            stream_id: 聊天流 ID（由运行时注入）
            **kwargs: 额外参数

        Returns:
            好感度查询结果
        """
        # 统一识别：优先使用运行时注入的真实 QQ 号，避免群聊昵称/私聊 QQ 号歧义
        resolved_user_id = await self._resolve_user_id(user_id, stream_id, **kwargs)
        if not resolved_user_id:
            return {"success": False, "error": "无法确定互动对象的用户标识"}

        # 记录「会话 ↔ 用户」映射，供注入 Hook 反查使用
        # 查询对象不一定是当前发言者，禁止污染会话身份缓存。

        # 获取好感度数据
        record = self._get_user_record(resolved_user_id)
        affection_value = record["affection"]
        level_info = _get_affection_level_info(affection_value)
        response_hint = self._get_response_hint(affection_value)

        # 获取最近几条历史记录
        history = record.get("history", [])
        recent_history = history[-5:] if history else []

        # 拼接角色名
        char_name = self.config.personality.character_name if self.config else "角色"

        # 构建返回结果
        result = {
            "success": True,
            "user_id": resolved_user_id,
            "affection_value": level_info["value"],
            "affection_level": level_info["level_name"],
            "level_description": level_info["level_description"],
            "level_progress_percent": level_info["level_progress"],
            "level_range": f"{level_info['level_min']} ~ {level_info['level_max']}",
            "response_hint": response_hint,
            "character_name": char_name,
            "locked": bool(record.get("locked", False)),
            "note": str(record.get("note", "") or ""),
            "recent_changes": [
                {
                    "change": h.get("change"),
                    "type": h.get("type"),
                    "description": h.get("description"),
                    "time": h.get("time"),
                }
                for h in recent_history
            ],
            "suggestion": (
                f"当前好感度等级为「{level_info['level_name']}」，"
                f"{level_info['level_description']}。"
                f"回应时应：{response_hint}"
            ),
        }
        if result["locked"]:
            result["suggestion"] = (
                f"该用户的好感度已被管理员锁定（当前 {result['affection_value']} 点，"
                f"「{level_info['level_name']}」），你无法自动调整它，请在回复中保持当前态度。"
            )

        self.ctx.logger.debug(
            "查询好感度: user=%s, affection=%.1f, level=%s",
            user_id,
            affection_value,
            level_info["level_name"],
        )

        return result

    # ════════════════════════════════════════════════════════════════
    # Tool: 调整好感度
    # ════════════════════════════════════════════════════════════════

    @Tool(
        "adjust_affection",
        description=(
            "根据一次有实质意义的互动，调整该用户的好感度。"
            "好感度范围从 -20（极度反感）到 100（思慕倾心），初始为 10。"
            "判断依据（不要机械套用行为词表，应结合对话上下文与当前关系综合判断）：\n"
            "1. 用户的真实意图（善意 / 恶意 / 中性）；\n"
            "2. 互动是否推进关系：主动关心、记得角色说过的小事、顺着角色的话题、"
            "对角色情绪做出恰当回应、产生默契、主动陪伴、配合角色性格的玩笑等；\n"
            "3. 是否尊重角色边界：恶意贬低、背叛信任、无视边界、持续骚扰、恶意争吵。\n"
            "关系解读：同一言行在不同关系下意义不同。在「亲近」「亲密」「思慕」等级下，"
            "用户的玩笑、打趣、暧昧乃至略带露骨的调侃是关系亲密的体现，应视为正面或中性，"
            "只有越界、贬低或骚扰才是负面；在较低等级下，同样的言行才更容易构成冒犯。\n"
            "日常琐碎闲聊、单句问候、无实质内容的互动不需要调整好感度。\n"
            "重大事件（如表白、送礼、重大帮助、背叛信任）可填写 change_value 给出精确变化值"
            "（如轻微调侃 -1、诚心道歉 +2、救命之恩 +8）；不填写时插件按互动类型在配置范围内自动取值，"
            "并受角色性格倍率与关系阻尼限制，请勿填写极端数值。\n"
            "注意：插件会自动评价大多数日常互动，本工具主要用于重大事件或自动评价遗漏的情形。\n"
            "注意：通常无需填写 user_id，运行时会自动注入当前互动对象的 QQ 号；"
            "如需调整指定对象请填写其 QQ 号（不要填昵称）。"
        ),
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="要调整好感度的用户 QQ 号。通常可留空，运行时会自动注入当前互动对象的 QQ 号。",
                required=False,
            ),
            ToolParameterInfo(
                name="interaction_type",
                param_type=ToolParamType.STRING,
                description=(
                    "互动类型: positive(正面), neutral(中性), "
                    "negative(负面), special(特殊)"
                ),
                required=True,
            ),
            ToolParameterInfo(
                name="description",
                param_type=ToolParamType.STRING,
                description="简短描述本次互动的内容（如「用户赞美了角色的新衣服」「用户送了生日礼物」）",
                required=True,
            ),
            ToolParameterInfo(
                name="change_value",
                param_type=ToolParamType.FLOAT,
                description=(
                    "可选的精确变化值（可正可负，如：轻微调侃 -1、诚心道歉 +2、"
                    "救命之恩 +8）。按互动的重要程度填写；不填时插件按互动类型自动取值。"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，由运行时自动注入，无需手动填写",
                required=False,
            ),
        ],
    )
    async def adjust_affection(
        self,
        user_id: str = "",
        interaction_type: str = "",
        description: str = "",
        change_value: float | None = None,
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """根据互动调整用户好感度。

        Args:
            user_id: 用户 QQ 号或昵称（可留空，运行时注入当前对象；会统一解析为 QQ 号）
            interaction_type: 互动类型
            description: 互动描述
            change_value: 可选的精确变化值（可正可负）；不填时按互动类型自动取值
            stream_id: 聊天流 ID（由运行时注入）
            **kwargs: 额外参数

        Returns:
            调整结果
        """
        # 统一识别：优先使用运行时注入的真实 QQ 号，避免群聊昵称/私聊 QQ 号歧义
        resolved_user_id = await self._resolve_user_id(user_id, stream_id, **kwargs)
        if not resolved_user_id:
            return {"success": False, "error": "无法确定互动对象的用户标识"}

        # 记录「会话 ↔ 用户」映射，供注入 Hook 反查使用
        self._record_session_user(stream_id, resolved_user_id)

        # 参数校验
        valid_types = {"positive", "neutral", "negative", "special"}
        interaction_type = interaction_type.strip().lower()
        if interaction_type not in valid_types:
            return {
                "success": False,
                "error": f"无效的互动类型: {interaction_type}，有效类型为: {', '.join(sorted(valid_types))}",
            }

        if not description or not description.strip():
            return {"success": False, "error": "互动描述不能为空"}

        if self.config is None:
            return {"success": False, "error": "插件配置未加载"}

        # 锁定检查：WebUI 中锁定的用户，LLM 无法自动调整好感度
        existing = self._affection_data.get(resolved_user_id, {})
        if existing.get("locked", False):
            self.ctx.logger.debug(
                "用户 %s 的好感度已被锁定，拒绝 LLM 自动调整", resolved_user_id
            )
            return {
                "success": False,
                "error": "该用户的好感度已被管理员锁定，请在 WebUI 中手动修改",
            }

        # 根据互动类型和配置计算好感度变化值
        cfg = self.config.affection_rule
        requested_change = None
        if change_value is not None:
            try:
                requested_change = float(change_value)
            except (TypeError, ValueError):
                requested_change = None
            if requested_change is not None and not math.isfinite(requested_change):
                requested_change = None

        if requested_change is not None:
            # LLM 按互动重要程度给出的精确变化值，按类型约束在安全范围内
            if interaction_type == "negative":
                change_value = max(-10.0, min(requested_change, -0.1))
            elif interaction_type == "neutral":
                change_value = max(0.0, min(requested_change, 1.0))
            elif interaction_type == "special":
                change_value = max(1.0, min(requested_change, 20.0))
            else:
                change_value = max(0.1, min(requested_change, 10.0))
        elif interaction_type == "positive":
            min_val = cfg.positive_interaction_min
            max_val = cfg.positive_interaction_max
            change_value = random.uniform(min_val, max_val)
        elif interaction_type == "negative":
            min_val = cfg.negative_interaction_min  # 更负的值（如 -3.0）
            max_val = cfg.negative_interaction_max  # 较不值的值（如 -0.5）
            # 注意：min_val 比 max_val 更小，所以 random.uniform 需要正确顺序
            change_value = random.uniform(min_val, max_val)
        elif interaction_type == "special":
            change_value = cfg.special_interaction_value
        else:
            # neutral
            change_value = cfg.neutral_interaction_value

        # 执行更新
        result = await self._update_affection(
            user_id=resolved_user_id,
            change_value=change_value,
            interaction_type=interaction_type,
            description=description,
        )

        return result

    # ════════════════════════════════════════════════════════════════
    # Command: /affection 查询当前好感度
    # ════════════════════════════════════════════════════════════════

    @Command(
        "check_affection",
        description="查询当前用户与角色的好感度",
        pattern=r"^(?:/affection|.*(?:好感度查询|查询好感度))\s*$",
    )
    async def check_affection(
        self, stream_id: str = "", **kwargs: Any
    ) -> tuple[bool, str, int]:
        """处理 /affection 命令，返回用户的好感度信息。

        Args:
            stream_id: 聊天流 ID（由运行时注入）
            **kwargs: 额外参数（包含 user_id 等）

        Returns:
            (success, message, priority) 元组
        """
        # 获取当前用户的 ID
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not user_id:
            message = "无法识别你的身份，请稍后再试。"
            if stream_id:
                await self.ctx.send.text(message, stream_id)
            return True, message, 1

        if self.config is None:
            message = "好感度系统配置未加载，请联系管理员。"
            if stream_id:
                await self.ctx.send.text(message, stream_id)
            return True, message, 1

        char_name = self.config.personality.character_name
        record = self._get_user_record(user_id)
        affection_value = record["affection"]
        level_info = _get_affection_level_info(affection_value)
        history = record.get("history", [])

        if not history:
            # 首次互动，没有历史记录
            message = (
                f"你和{char_name}的好感度为 {level_info['value']} 点，"
                f"属于「{level_info['level_name']}」等级。\n"
                f"{level_info['level_description']}。"
            )
        else:
            last_change = history[-1]
            change_time = last_change.get("time", "未知")
            try:
                dt = datetime.fromisoformat(change_time)
                change_time_str = dt.strftime("%m月%d日 %H:%M")
            except (ValueError, TypeError):
                change_time_str = change_time

            message = (
                f"你和{char_name}的好感度为 {level_info['value']} 点，"
                f"属于「{level_info['level_name']}」等级。\n"
                f"{level_info['level_description']}。\n"
                f"（最近一次变化: {change_time_str}）"
            )

        # Only host command context + an exact authenticated receive-hook message.
        mid = str(kwargs.get("message_id") or kwargs.get("msg_id") or "").strip()
        if not mid and isinstance(kwargs.get("message"), dict):
            mid = str(kwargs["message"].get("message_id") or "").strip()
        message += self._self_query_impression(stream_id, mid, user_id)
        if stream_id:
            await self.ctx.send.text(message, stream_id)

        self.ctx.logger.debug(
            "用户 %s 查询好感度: %.1f (%s)", user_id, affection_value, level_info["level_name"]
        )

        return True, message, 1

    # ════════════════════════════════════════════════════════════════
    # Hook: 接收时建立「会话 ↔ 消息 ↔ 发送者」索引
    # ════════════════════════════════════════════════════════════════
    # 群聊中会话按群维度建立，会话本身不携带发言人信息；唯一可靠的标识在
    # 每条消息的 message_info.user_info 中。本 Hook 在消息完成预处理后立即
    # 记录「会话 → (message_id, user_id)」映射（与入库、请求调度无关），
    # 之后 before_request 通过 prompt 中携带的 msg_id 精确反查当前发言人，
    # 不受「他人消息先于本轮请求写入消息库」的竞态影响。

    _PROMPT_MSG_ID_PATTERN = re.compile(r'msg_id="([^"]*)"')

    @HookHandler(
        "chat.receive.after_process",
        name="affection_index_speaker",
        description="在入站消息完成预处理后，记录会话内消息 ID 到发送者 QQ 号的映射，供好感度注入精确解析当前发言人",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def index_message_speaker(
        self,
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在消息接收阶段建立「会话 → 消息 ID → 发送者」索引。

        Args:
            message: 已完成预处理、序列化的入站 SessionMessage 字典
            **kwargs: 其他 Hook 参数

        Returns:
            Hook 分发结果：继续执行
        """
        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}

        session_id = str(message.get("session_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        user_id = ""
        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict):
                user_id = str(user_info.get("user_id") or "").strip()
        if not (session_id and message_id and user_id):
            return {"action": "continue"}

        # 保序去重：同一 message_id 重新接收时移到队尾
        dq = self._session_message_index.setdefault(session_id, deque(maxlen=100))
        for index, (existing_id, _existing_user) in enumerate(dq):
            if existing_id == message_id:
                del dq[index]
                break
        dq.append((message_id, user_id))

        # This registry is separate from the original identity/repair mechanisms.
        try:
            cfg = self._impression_config()
            info = message.get("message_info") or {}
            user = info.get("user_info") or {}
            platform = user.get("platform") or info.get("platform") or message.get("platform")
            if cfg and cfg.enabled and not message.get("is_notify") and (
                str(platform).lower() == "qq"
            ):
                name = self.config.personality.character_name
                raw = message.get("raw_message")
                has_at = isinstance(raw, list) and any(isinstance(s, dict) and s.get("type") == "at" for s in raw)
                if has_at and is_self_query(own_text(message), name):
                    await self._refresh_impression_bot_ids()
                text = self_query_message_text(message, name, self._impression_bot_ids)
                if text:
                    self._impression_queries.remember(
                        session_id, message_id, user_id, text, name,
                        str((info.get("group_info") or {}).get("group_id") or ""))
        except Exception as exc:
            self.ctx.logger.debug("印象查询认证已跳过: %s", type(exc).__name__)

        # 调度自动评价（后台任务，不阻塞消息接收链路）
        self._schedule_auto_evaluation(session_id, user_id, message)
        return {"action": "continue"}

    def _impression_config(self):
        if self.config is None or not self.config.plugin.enabled:
            return None
        return getattr(self.config, "impression", None)

    async def _refresh_impression_bot_ids(self):
        """Use the SDK's read-only confirmed-account model, never message/tool IDs."""
        async with self._impression_bot_lookup_lock:
            now = time.monotonic()
            if self._impression_bot_lookup_at and now - self._impression_bot_lookup_at < 60:
                return
            self._impression_bot_lookup_at = now
            try:
                rows = await asyncio.wait_for(self.ctx.db.get(
                    "BotPlatformAccount", filters={"platform": "qq", "disabled": False}, limit=20), timeout=0.8)
                if isinstance(rows, dict):
                    if rows.get("success") is False:
                        raise ValueError("account lookup rejected")
                    rows = rows.get("data", rows.get("result"))
                if not isinstance(rows, list):
                    raise ValueError("unsupported account response")
                self._impression_bot_ids = {
                    str(row.get("account_id") or "").strip() for row in rows
                    if isinstance(row, dict) and row.get("platform") == "qq" and row.get("disabled") is False
                    and str(row.get("account_id") or "").isascii() and str(row.get("account_id") or "").isdigit()}
            except Exception as exc:
                self._impression_bot_ids = set()
                self.ctx.logger.debug("印象机器人身份读取已降级: %s", type(exc).__name__)

    def _self_query_impression(self, stream_id: str, message_id: str, user_id: str) -> str:
        """Never use tool arguments, a nickname or the latest speaker as authority."""
        try:
            cfg = self._impression_config()
            manager = self._impression_manager
            if not cfg or not cfg.enabled or manager is None:
                return ""
            if self._lookup_message_speaker(stream_id, message_id) != user_id:
                return ""
            if not self._impression_queries.claim(stream_id, message_id, user_id):
                return ""
            personality = self.config.personality
            record = self._affection_data.get(user_id, {})
            level = _get_affection_level_info(float(record.get("affection", 10.0)))
            return manager.query(user_id, stream_id, {
                "group_id": self._impression_queries.group_id(stream_id, message_id, user_id),
                "character_name": personality.character_name,
                "personality_hint": personality.character_personality_hint or personality.personality_type,
                "relationship_level": level["level_name"],
                "response_style": self._get_response_hint(level["value"]),
            })
        except Exception as exc:
            self.ctx.logger.warning("印象查询已降级: %s", type(exc).__name__)
            return ""

    @HookHandler(
        "maisaka.planner.after_response",
        name="impression_explicit_self_query",
        description="仅为精确绑定真实本人查询消息的reply附加缓存印象并检查后台刷新",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_self_query_impression(self, tool_calls=None, session_id="", **kwargs):
        del kwargs
        if not isinstance(tool_calls, list):
            return {"action": "continue"}
        modified = list(tool_calls)
        changed = False
        for index, call in enumerate(tool_calls):
            func = call.get("function") if isinstance(call, dict) else None
            if not isinstance(func, dict) or func.get("name") != "reply":
                continue
            args = func.get("arguments")
            if not isinstance(args, dict):
                continue
            mid = str(args.get("msg_id") or "").strip()
            uid = self._lookup_message_speaker(session_id, mid) if mid else ""
            if not uid:
                continue
            text = self._self_query_impression(session_id, mid, uid)
            if not text:
                continue
            # Touch only this reply: other targets in the same batch receive no data.
            new_args = dict(args)
            new_args["reference_info"] = (
                str(args.get("reference_info") or "")
                + "\n【本轮本人主动查询的缓存印象】请在本轮好感度答复末尾原样附加以下正文，"
                  "不扩写、不推断共同经历，也不要重复此说明：" + text)
            modified[index] = {**call, "function": {**func, "arguments": new_args}}
            changed = True
        return ({"action": "continue", "modified_kwargs": {"tool_calls": modified}}
                if changed else {"action": "continue"})

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="impression_append_to_explicit_self_reply",
        description="兼容Item协议宿主，在精确本人查询的最终回复中确定性追加缓存印象",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def append_self_query_impression(
        self, response="", session_id="", reply_message_id="", **kwargs
    ):
        # Current hosts expose output_items in Planner hooks. Append to the final
        # string instead of guessing that schema or spending Replyer input tokens.
        del kwargs
        if not isinstance(response, str) or not response.strip():
            return {"action": "continue"}
        mid = str(reply_message_id or "").strip()
        uid = self._lookup_message_speaker(session_id, mid) if mid else ""
        if not uid:
            return {"action": "continue"}
        text = self._self_query_impression(session_id, mid, uid)
        if not text:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {"response": response + text}}

    @staticmethod
    def _extract_prompt_message_id(content: str) -> str:
        """从 planner prompt 消息内容中提取消息前缀携带的 msg_id。

        Maisaka 会把用户消息渲染为 `<message msg_id="xxx" time="..." user="...">\n内容`
        形式，序列化后的 content 保留完整前缀，因此可以从中解析出消息 ID，
        用于在接收索引中精确反查发送者。

        Args:
            content: prompt 中单条消息的 content 文本

        Returns:
            消息 ID；未找到时返回空字符串
        """
        match = AffectionPlugin._PROMPT_MSG_ID_PATTERN.search(content or "")
        return match.group(1).strip() if match else ""

    def _lookup_message_speaker(self, session_id: str, message_id: str) -> str:
        """按消息 ID 从接收索引反查发送者 QQ 号。

        Args:
            session_id: 会话 ID
            message_id: 目标消息 ID

        Returns:
            发送者用户标识；未命中时返回空字符串
        """
        dq = self._session_message_index.get(session_id)
        if not dq:
            return ""
        for indexed_id, indexed_user in dq:
            if indexed_id == message_id:
                return indexed_user
        return ""

    def _lookup_latest_session_speaker(self, session_id: str) -> str:
        """返回会话接收索引中最近一条消息的发送者（近似方案）。

        当无法从 prompt 精确锚定（例如 replyer 阶段无消息列表）时，
        使用「最近发言者」作为近似。

        Args:
            session_id: 会话 ID

        Returns:
            最近发言者用户标识；索引为空时返回空字符串
        """
        dq = self._session_message_index.get(session_id)
        if not dq:
            return ""
        return dq[-1][1]

    def _resolve_user_from_prompt_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """从 planner prompt 的最后一条用户消息精确锚定发言人。

        prompt 是按接收顺序构建的快照，晚于本轮构建到达的消息不会进入
        prompt；因此「最后一条用户消息」就是模型本轮实际看到并要回应的
        消息。解析其 msg_id 后在接收索引中反查发送者，即可规避
        「他人消息先入库」导致的错配。

        Args:
            session_id: 会话 ID
            messages: before_request 收到的序列化消息列表

        Returns:
            锚定到的用户标识；无法锚定时返回空字符串
        """
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            # 工具结果也会以 user 角色出现（带 tool_call_id），跳过
            if msg.get("tool_call_id"):
                continue
            content = str(msg.get("content") or "")
            message_id = self._extract_prompt_message_id(content)
            if not message_id:
                # 非真实会话消息（如参考消息、引导回复），继续向前找
                continue
            user_id = self._lookup_message_speaker(session_id, message_id)
            if user_id:
                return user_id
            # 最后一条真实用户消息的 msg_id 在索引中未命中（如插件热重载后
            # 索引尚未重建），不再向前匹配更早的消息，交由后续兜底逻辑处理，
            # 避免错配到历史发言人。
            return ""
        return ""

    # ════════════════════════════════════════════════════════════════
    # Hook: 注入好感度提示（参考 ns_auto-planning_schedule 的调用方式）
    # ════════════════════════════════════════════════════════════════
    # 目的：提高 LLM 对好感度的感知，从而：
    #   1. 按照对应好感度等级自然回应；
    #   2. 更倾向于在互动后调用 adjust_affection 工具更新好感度。
    #
    # 参考 AutoPlanning 插件经过验证的机制：
    #   - 主通道：maisaka.planner.after_response（BLOCKING），在 planner 决定调用
    #     reply 后，把好感度上下文注入 reply 工具的 reference_info 参数。replyer
    #     生成回复时会读取 reference_info，因此 LLM 一定能感知到好感度。
    #   - 辅助通道：maisaka.planner.before_request（BLOCKING），在 LLM 规划前
    #     向消息窗口插入一条系统提示，进一步促使 LLM 调用 adjust_affection。
    # 两条通道每次请求都会注入（不再走概率采样）。

    @HookHandler(
        "maisaka.planner.after_response",
        name="affection_inject_into_reply",
        description="在 planner 决定调用 reply 后，将当前用户好感度上下文注入 reply 的 reference_info，使 LLM 回复体现好感度并倾向调用 adjust_affection",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_affection_into_reply(
        self,
        tool_calls: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """参考 AutoPlanning 插件：在 planner 决定调用 reply 后注入好感度参考。

        找到 reply 工具调用，把好感度上下文注入其 reference_info 参数，
        使 replyer 生成回复时参考好感度等级。

        Args:
            tool_calls: planner 生成的工具调用列表
            session_id: 当前会话 ID
            **kwargs: 其他 Hook 参数

        Returns:
            Hook 分发结果：{"action": "continue", "modified_kwargs": {...}}
        """
        del kwargs
        if self.config is None:
            return {"action": "continue"}
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if not self.config.call_behavior.inject_affection_hint:
            return {"action": "continue"}
        if not isinstance(tool_calls, list) or not tool_calls:
            return {"action": "continue"}

        # 通过 session_id 反查发言者 QQ 号；解析失败时退化为通用好感度提示，
        # 确保注入始终发生（参考 AutoPlanning 插件每次请求都注入）。
        #
        # 注意：本 Hook 注入的是 replyer 参考信息。replyer 模型没有工具也没有
        # 工具 Schema，若提示里出现「请调用 adjust_affection 工具」之类的指令，
        # 模型会以 <function_calls> 文本形式模仿工具调用并原样发出，造成工具
        # 调用信息泄漏到聊天。因此这里只注入好感度态度提示，绝不包含工具调用引导。
        # 从 reply 工具的 msg_id 定位真正的回复对象。
        target_msg_id = ""
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if not isinstance(func, dict) or func.get("name") != "reply":
                continue
            args = func.get("arguments", {})
            if isinstance(args, dict):
                target_msg_id = str(args.get("msg_id") or "").strip()
            break

        user_id = (
            self._lookup_message_speaker(str(session_id or "").strip(), target_msg_id)
            if target_msg_id else ""
        )
        if not user_id:
            self.ctx.logger.warning(
                "reply好感度身份未能精确匹配: session=%s, msg_id=%s; 使用通用提示",
                session_id, target_msg_id
            )
        if user_id:
            hint_text = self._build_affection_hint(user_id, for_replyer=True)
        else:
            hint_text = self._build_generic_affection_hint(for_replyer=True)
            self.ctx.logger.info(
                "未解析到会话 %s 的用户标识，注入通用好感度提示", session_id
            )
        if not hint_text:
            return {"action": "continue"}

        # 兜底：Planner 未调用 query_affection 时，Replyer 仍能拿到插件内权威数值。
        # 只有「本轮本人数字查询」和「回复 msg_id 精确对应 QQ 号」同时满足才注入；
        # 无法确认身份或询问第三人时绝不代入其他群友的分数。
        key = (str(session_id or "").strip(), target_msg_id)
        if target_msg_id and key in self._affection_numeric_requests:
            is_self_query = self._affection_numeric_requests[key]
            if is_self_query and user_id:
                # 与 query_affection 一致的数据源；仅在精确定位身份后才读取。
                record = self._get_user_record(user_id)
                actual = _get_affection_level_info(float(record.get("affection", 0.0)))
                hint_text += (
                    f"\n【本轮好感度数值核实】这位用户对你而言的当前好感度为 "
                    f"{actual['value']}，等级「{actual['level_name']}」。"
                    "本数据直接来自插件当前存储值，而非模型猜测。对方如果询问具体数字，"
                    "必须以此数值为准。不能说查不到、没记录，也不要描述为翻账本或查系统；"
                    "请像在说自己心里的感觉那样自然表达。"
                )
                self.ctx.logger.info("好感度问询直接核实: session=%s, msg_id=%s, user=%s", session_id, target_msg_id, user_id)
            elif is_self_query:
                hint_text += (
                    "\n【具体好感度核实失败】本轮未能核实目标与当前分数。"
                    "不得猜测数字、等级或声称对方没有记录。你仍清楚自己对人的感受；"
                    "只是这次不能准确报出量化数值。不要说‘我不知道自己的心意’、"
                    "‘说不准对你的感觉’、‘账本’、‘系统’或‘翻错页’。"
                    "不必展开后台原因，也不必每次索爱、吃醋或追问。"
                    f"本轮表达方向：{self._next_affection_fallback_strategy()}"
                    "请结合当前对话自行措辞，不要照搬固定台词。"
                )
                self.ctx.logger.warning("好感度数值核实身份不足: session=%s, msg_id=%s", session_id, target_msg_id)

        modified_tool_calls = self._inject_into_reply_reference(tool_calls, hint_text)
        if modified_tool_calls is tool_calls:
            return {"action": "continue"}

        result = {"action": "continue", "modified_kwargs": {"tool_calls": modified_tool_calls}}
        self.ctx.logger.info(
            "已向会话 %s 的 reply 注入好感度参考 (%s)", session_id, user_id
        )
        return result

    @staticmethod
    def _inject_into_reply_reference(
        tool_calls: list[dict[str, Any]],
        hint_text: str,
    ) -> list[dict[str, Any]]:
        """把好感度提示注入 reply 工具的 reference_info 参数。

        参考 AutoPlanning 插件的 inject_schedule_into_reply 实现。

        Args:
            tool_calls: planner 生成的工具调用列表
            hint_text: 好感度提示文本

        Returns:
            修改后的工具调用列表；未找到 reply 时返回原列表
        """
        reply_idx = None
        for i, tc in enumerate(tool_calls):
            func = tc.get("function", {}) if isinstance(tc, dict) else {}
            if isinstance(func, dict) and func.get("name") == "reply":
                reply_idx = i
                break
        if reply_idx is None:
            return tool_calls

        modified = [dict(tc) for tc in tool_calls]
        tc = modified[reply_idx]
        func = dict(tc.get("function", {}))
        args = dict(func.get("arguments", {}))
        existing_ref = str(args.get("reference_info", "") or "").strip()
        if existing_ref:
            args["reference_info"] = f"{existing_ref}\n\n{hint_text}"
        else:
            args["reference_info"] = hint_text
        func["arguments"] = args
        tc["function"] = func
        modified[reply_idx] = tc
        return modified

    def _next_affection_fallback_strategy(self) -> str:
        """抽取一条不重复的兜底表达策略，而不是固定台词。

        注意：这是给 Replyer 的写作指引，不直接向 QQ 用户发送句子。
        """
        options = [
            "轻轻卖个关子，表示准确数字稍后再说；语气亲近但不故弄玄虚。",
            "调侃对方突然考你具体数字，但不报未经核实的分数；保持自然。",
            "干脆承认这次没法给准确数字，不解释技术原因，不延伸新话题。",
            "先接住对方的亲昵称呼，再表示精确数字这回先欠着；不索爱。",
            "像朋友开玩笑一样表示不能乱报数字，留一点小小的悬念。",
            "用慵懒又俏皮的语气把数字问题暂时放下，不怀疑自己的感受。",
            "爽快地表示暂时不报未经核实的数字，然后自然回应眼前这句话。",
        ]
        if not self._affection_fallback_strategies:
            self._affection_fallback_strategies = list(options)
            random.shuffle(self._affection_fallback_strategies)
            if (
                self._last_affection_fallback_strategy
                and len(self._affection_fallback_strategies) > 1
                and self._affection_fallback_strategies[-1] == self._last_affection_fallback_strategy
            ):
                self._affection_fallback_strategies[0], self._affection_fallback_strategies[-1] = (
                    self._affection_fallback_strategies[-1], self._affection_fallback_strategies[0]
                )
        strategy = self._affection_fallback_strategies.pop()
        self._last_affection_fallback_strategy = strategy
        return strategy

    @staticmethod
    def _detect_affection_number_request(message_content: str) -> tuple[bool, bool]:
        """判断当前用户消息是否明确询问好感度数字，及能否安全视为本人。

        返回 (is_lookup, is_self)。这只是回复可靠性的补充判断；
        不代替 Planner 对查询其他人的语义判断。
        """
        # <message msg_id="..."> 是 MaiBot 的外层包装，避免历史引文混入。
        content = str(message_content or "")
        opening_tag = re.search(r"<message\b[^>]*>", content)
        if opening_tag:
            content = content[opening_tag.end():]
            content = re.sub(r"</message>\s*$", "", content)
        # 不把长篇引用当作当前请求；只看靠前的正文。
        text = content.strip()[:280]
        if not re.search(r"好感度|亲密度|好感值", text):
            return False, False
        if not re.search(r"多少|几(?:点|分|级)?|数值|分数|等级|查询|查一下|查查|查|看看|看下|报(?:个|一下)?数|现在|目前|想知道|告诉我|想问|有多少", text):
            return False, False
        # 有明确第三方 QQ、第三人称或“某某的好感度”时不代入当前发言人。
        third_party = bool(re.search(
            r"(?:QQ|qq|用户|账号|号码)\s*[:：#]?\s*\d{5,12}"
            r"|(?:他|她|别人|其他人|那个人)的好感",
            text,
        ))
        # “托亚的好感度”是第三人，“你对我的好感度”则是本人。
        for match in re.finditer(r"([\u4e00-\u9fff]{1,12})的(?:好感度|亲密度|好感值)", text):
            subject = match.group(1)
            if not subject.endswith(("我", "你", "咱", "我们", "咱们", "本人")):
                third_party = True
                break
        # 省略主语的“好感度是多少呀”通常是在问当前发言者自己；
        # 不明言第三人时可以查当前发言人，避免无谓的查询失败。
        return True, not third_party

    @HookHandler(
        "maisaka.planner.before_request",
        name="affection_planner_injector",
        description="在 LLM 规划请求前，将当前用户的好感度提示注入消息窗口，提高 LLM 感知与 adjust_affection 工具调用频率",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_affection_before_request(
        self,
        messages: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """在 LLM 请求前注入好感度提示。

        每次都注入（不受概率控制）。通过 session_id 反查发言者 QQ 号，
        构造好感度系统提示并插入 messages 列表首部，确保 LLM 一定感知到。

        Args:
            messages: 即将发给模型的 PromptMessage 列表
            session_id: 当前会话 ID
            **kwargs: 其他 Hook 参数

        Returns:
            Hook 分发结果：{"action": "continue", "modified_kwargs": {...}}
        """
        del kwargs
        if self.config is None:
            return {"action": "continue"}
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        if not self.config.call_behavior.inject_affection_hint:
            return {"action": "continue"}
        if not isinstance(messages, list) or not messages:
            return {"action": "continue"}

        # 通过 session_id 反查发言者 QQ 号；优先用 prompt 中的 msg_id 精确锚定，
        # 解析失败时退化为通用好感度提示，确保注入始终发生。
        user_id = await self._resolve_user_id_from_session(session_id, messages)
        if user_id:
            hint_text = self._build_affection_hint(user_id)
        else:
            hint_text = self._build_generic_affection_hint()
            self.ctx.logger.info(
                "未解析到会话 %s 的用户标识，注入通用好感度提示", session_id
            )
        if not hint_text:
            return {"action": "continue"}

        # 从本轮 Prompt 最后一条带 msg_id 的真人消息检测精确好感度问询。
        # 使用 msg_id 键控，避免同群后来者的问询污染前一条消息的回复。
        current_msg_id = ""
        current_msg_body = ""
        for candidate in reversed(messages):
            if not isinstance(candidate, dict) or candidate.get("role") != "user" or candidate.get("tool_call_id"):
                continue
            body = str(candidate.get("content") or "")
            mid = self._extract_prompt_message_id(body)
            if mid:
                current_msg_id, current_msg_body = mid, body
                break
        is_numeric_query, is_self_query = self._detect_affection_number_request(current_msg_body)
        if current_msg_id and is_numeric_query:
            key = (str(session_id or "").strip(), current_msg_id)
            self._affection_numeric_requests[key] = is_self_query
            if len(self._affection_numeric_requests) > 512:
                self._affection_numeric_requests.pop(next(iter(self._affection_numeric_requests)))
            hint_text += (
                "\n【具体好感度问询】对方正在询问好感度数值或等级。请先调用 query_affection 获取实时数据，"
                "查询成功后才能报告准确数值。没有实际查询结果不能断言‘没记录’、不能编造分数，"
                "也不要说自己翻账本、翻档案或翻错页。好感度是你心里的感受，数值只是幕后量化。"
                "如果本轮确实无法核实准确数字，就暂时不报数字；不要把数值核实失败"
                "说成自己不确定感情，也不要编造好感度等级。等待 Replyer 侧的多样化兜底指引。"
            )
            self.ctx.logger.info("检测到具体好感度问询: session=%s, msg_id=%s, self=%s", session_id, current_msg_id, is_self_query)

        # 注入一条 system 提示。若消息中已有 system 消息，则插在最后一个
        # system 之后；否则直接作为首条。这样既不影响原有系统提示，又能
        # 保证 LLM 一定看到好感度提示。
        hint_msg = {"role": "system", "content": hint_text}
        last_system_idx = -1
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                last_system_idx = idx
        if last_system_idx < 0:
            modified_messages = [hint_msg] + list(messages)
        else:
            modified_messages = (
                list(messages[: last_system_idx + 1])
                + [hint_msg]
                + list(messages[last_system_idx + 1 :])
            )

        self.ctx.logger.info(
            "已向会话 %s 注入好感度提示 (%d 条消息 -> %d 条, user=%s)",
            session_id,
            len(messages),
            len(modified_messages),
            user_id,
        )
        return {"action": "continue", "modified_kwargs": {"messages": modified_messages}}

    async def _resolve_user_id_from_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """根据会话 ID 反查发言者的权威用户标识（QQ 号）。

        解析优先级：
            1. prompt 锚定：从 messages 中最后一条用户消息的 msg_id 精确反查
               （接收索引在 chat.receive.after_process 时建立，与入库时机无关）
            2. 接收索引中该会话最近一条消息的发送者（无 prompt 时的近似）
            3. 运行时 Tool 调用时缓存的「会话↔用户」映射
            4. 从消息库最近消息反查（兜底）
            5. 从聊天流列表反查（仅对私聊有效，群聊流 user_id 恒为空）

        Args:
            session_id: 会话 ID
            messages: 可选；planner before_request 收到的序列化消息列表，
                用于 msg_id 精确锚定

        Returns:
            用户平台 ID；无法解析时返回空字符串
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return ""

        # 1. prompt 锚定（最精确）：prompt 是接收顺序的快照，晚于本轮构建
        #    到达的消息不会进入 prompt；最后一条用户消息的 msg_id 即本轮
        #    要回应的消息，反查接收索引即可拿到其发送者。
        if isinstance(messages, list) and messages:
            anchored_user_id = self._resolve_user_from_prompt_messages(session_id, messages)
            if anchored_user_id:
                self._session_user_map[session_id] = anchored_user_id
                return anchored_user_id

            # Prompt 含真实消息ID但索引未命中，禁止回退到其他群友。
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "user" or msg.get("tool_call_id"):
                    continue
                mid = self._extract_prompt_message_id(str(msg.get("content") or ""))
                if mid:
                    self.ctx.logger.warning(
                        "Planner好感度身份锚定失败: session=%s, msg_id=%s",
                        session_id, mid
                    )
                    self._session_user_map.pop(session_id, None)
                    return ""

        # 2. 接收索引最近发言人（近似，replyer 等无 prompt 场景）
        latest_user_id = self._lookup_latest_session_speaker(session_id)
        if latest_user_id:
            self._session_user_map[session_id] = latest_user_id
            return latest_user_id

        # 3. 命中缓存（由 Tool 调用或之前轮次解析记录）
        cached = self._session_user_map.get(session_id, "")
        if cached:
            return cached

        # 4. 从消息库最近消息反查（兜底：插件热重载后索引尚未建立时）
        recent_user_id = await self._resolve_user_id_from_recent_messages(session_id)
        if recent_user_id:
            self._session_user_map[session_id] = recent_user_id
            return recent_user_id

        # 5. 从聊天流列表反查（私聊场景有效）
        try:
            streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
        except Exception as e:
            self.ctx.logger.debug("获取聊天流列表失败 (%s): %s", session_id, e)
            return ""
        if not isinstance(streams, list):
            return ""
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            sid = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if sid == session_id:
                uid = str(stream.get("user_id") or "").strip()
                if uid:
                    self._session_user_map[session_id] = uid
                return uid
        return ""

    async def _resolve_user_id_from_recent_messages(self, session_id: str) -> str:
        """从会话最近消息反查当前发言人用户标识（QQ 号）。

        群聊中会话按群维度建立（session_id 仅由平台+群号计算），发言人的
        用户标识只能从具体消息的 message_info.user_info 获取。触发本次请求的
        用户消息在进入 chat loop 前已写入消息库（见 heartflow_message_processor
        的 store_message_to_db），因此「最近一条用户消息」的发送者就是当前
        互动对象；私聊场景同样成立。

        Args:
            session_id: 会话 ID

        Returns:
            最近发言人平台 ID；无法解析时返回空字符串
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return ""
        try:
            raw = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=session_id,
                limit=8,
                filter_mai=True,
            )
        except Exception as e:
            self.ctx.logger.debug("查询会话 %s 最近消息失败: %s", session_id, e)
            return ""
        if isinstance(raw, dict):
            raw = raw.get("messages") or []
        if not isinstance(raw, list) or not raw:
            return ""
        # 消息按时间正序返回，倒序遍历取最近一条带发送者的用户消息
        for msg in reversed(raw):
            if not isinstance(msg, dict):
                continue
            message_info = msg.get("message_info")
            if not isinstance(message_info, dict):
                continue
            user_info = message_info.get("user_info")
            if not isinstance(user_info, dict):
                continue
            uid = str(user_info.get("user_id") or "").strip()
            if uid:
                return uid
        return ""

    def _record_session_user(self, session_id: str, user_id: str) -> None:
        """记录「会话 ID → 权威 QQ 号」映射，供注入 Hook 使用。

        Args:
            session_id: 会话 ID
            user_id: 权威用户标识（QQ 号）
        """
        session_id = str(session_id or "").strip()
        user_id = str(user_id or "").strip()
        if session_id and user_id:
            self._session_user_map[session_id] = user_id

    # ════════════════════════════════════════════════════════════════
    # 自动评价：基于上下文与关系的互动综合评价
    # ════════════════════════════════════════════════════════════════
    # 设计说明：
    #   - 由 chat.receive.after_process 在用户消息到达时调度为后台任务，
    #     不阻塞消息接收与后续 Planner/Reply 流程。
    #   - 评价不依赖 Planner 的工具调用意愿：插件直接调用 Host 的 LLM，
    #     输入最近对话上下文、角色性格与当前关系等级，输出是否需要变化、
    #     方向、幅度与理由，再走统一的 _update_affection 落库。
    #   - 任何异常都静默降级（记录 debug 日志），绝不影响聊天主流程。

    def _schedule_auto_evaluation(
        self,
        session_id: str,
        user_id: str,
        message: dict[str, Any] | None,
    ) -> None:
        """调度一次自动评价后台任务（带会话级去重）。

        Args:
            session_id: 会话 ID
            user_id: 消息发送者用户标识
            message: 触发消息字典（用于前置过滤）
        """
        if self.config is None:
            return
        try:
            if not self.config.plugin.enabled:
                return
            if not self.config.evaluation.enabled:
                return
        except AttributeError:
            return
        session_id = str(session_id or "").strip()
        user_id = str(user_id or "").strip()
        if not (session_id and user_id):
            return

        # 命令 / 通知消息不评价
        if isinstance(message, dict):
            if message.get("is_command") or message.get("is_notify"):
                return

        # 同一会话已有活跃评价任务则跳过，避免连续消息排队堆积
        existing = self._evaluation_tasks.get(session_id)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(self._auto_evaluate(session_id, user_id))
        self._evaluation_tasks[session_id] = task

    async def _auto_evaluate(self, session_id: str, user_id: str) -> None:
        """执行一次自动评价：取上下文 → LLM 综合评价 → 按需调整好感度。

        整个方法在任何环节失败时静默降级，不抛出异常影响主流程。

        Args:
            session_id: 会话 ID
            user_id: 待评价的用户标识（QQ 号）
        """
        try:
            if self.config is None:
                return
            # 冷却期内不评价（也省一次 LLM 调用）
            if self._check_cool_down(user_id):
                return
            # 当日次数已满不评价
            if self._check_daily_limit(user_id):
                return
            # 被锁定用户不评价
            record = self._affection_data.get(user_id)
            if record is not None and record.get("locked", False):
                return

            # 1. 取最近对话上下文（含角色回复，用于判断互动氛围）
            context_messages = await self._fetch_recent_context_messages(
                session_id, self.config.evaluation.max_context_messages
            )
            if not context_messages:
                return

            # 2. 构造评价请求并调用 LLM
            prompt = self._build_evaluation_prompt(user_id, context_messages)
            result = await self.ctx.llm.generate(
                prompt,
                model=str(self.config.evaluation.model_task or "").strip(),
                temperature=self.config.evaluation.temperature,
                max_tokens=self.config.evaluation.max_response_tokens,
            )
            if not isinstance(result, dict) or not result.get("success"):
                self.ctx.logger.debug(
                    "自动评价调用失败 (session=%s, user=%s): %s",
                    session_id, user_id, result.get("error") if isinstance(result, dict) else result,
                )
                return
            response_text = str(result.get("response") or "").strip()
            if not response_text:
                return

            # 3. 解析评价结果
            evaluation = self._parse_evaluation_result(response_text)
            if not evaluation:
                self.ctx.logger.debug(
                    "自动评价输出解析失败 (session=%s, user=%s): %.200s",
                    session_id, user_id, response_text,
                )
                return
            if not evaluation["need_change"] or evaluation["direction"] == "neutral":
                return

            # 4. 幅度映射并落库
            direction = evaluation["direction"]
            change_value = self._magnitude_to_change_value(direction, evaluation["magnitude"])
            description = evaluation["reason"] or "自动评价"
            result = await self._update_affection(
                user_id=user_id,
                change_value=change_value,
                interaction_type=direction,
                description=f"[自动评价] {description}",
            )
            if result.get("success"):
                self.ctx.logger.info(
                    "自动评价生效: user=%s, %s %.1f → %.1f, 理由=%s",
                    user_id,
                    direction,
                    result.get("old_affection"),
                    result.get("new_affection"),
                    description,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.ctx.logger.debug("自动评价异常 (session=%s, user=%s): %s", session_id, user_id, e)
        finally:
            # 仅当当前任务仍是注册中的任务时才清理，避免误删并发注册的新任务
            current = asyncio.current_task()
            if self._evaluation_tasks.get(session_id) is current:
                self._evaluation_tasks.pop(session_id, None)

    async def _fetch_recent_context_messages(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """获取会话最近消息用于评价上下文。

        Args:
            session_id: 会话 ID
            limit: 最多取多少条

        Returns:
            序列化消息字典列表（时间正序，不含命令/通知/无文本消息）
        """
        try:
            raw = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=session_id,
                hours=24.0,
                limit=max(2, limit),
                limit_mode="latest",
                filter_mai=False,
            )
        except Exception as e:
            self.ctx.logger.debug("获取评价上下文失败 (%s): %s", session_id, e)
            return []
        if isinstance(raw, dict):
            raw = raw.get("messages") or []
        if not isinstance(raw, list):
            return []

        cleaned: list[dict[str, Any]] = []
        for msg in raw:
            if not isinstance(msg, dict):
                continue
            if msg.get("is_command") or msg.get("is_notify"):
                continue
            content = str(msg.get("processed_plain_text") or msg.get("content") or "").strip()
            if not content:
                continue
            cleaned.append(msg)
        return cleaned

    def _build_evaluation_prompt(
        self,
        user_id: str,
        context_messages: list[dict[str, Any]],
    ) -> str:
        """构造自动评价提示词。

        Args:
            user_id: 待评价的用户标识
            context_messages: 最近的序列化消息列表

        Returns:
            评价提示词文本
        """
        record = self._get_user_record(user_id)
        affection_value = record.get("affection", 0.0)
        level_info = _get_affection_level_info(affection_value)
        char_name = self.config.personality.character_name
        personality_hint = str(self.config.personality.character_personality_hint or "").strip()
        if not personality_hint:
            personality_hint = self.config.personality.personality_type

        lines = [
            f"你是一位「{char_name}」的角色好感度评估助手，"
            f"负责判断用户刚才的互动是否应改变{char_name}对该用户的好感度。",
            f"角色性格概述：{personality_hint}",
            f"当前好感度：{level_info['value']} 点，等级「{level_info['level_name']}」"
            f"（{level_info['level_description']}）。",
            "",
            "判断规则：",
            "1. 同一言行在不同关系下的意义不同：在「亲近」「亲密」「思慕」等较高等级下，"
            "用户的关心、玩笑、打趣、暧昧、撒娇等是关系亲密的体现，应视为正向或中性；"
            "在较低等级下，轻浮或冒犯性的言行才更容易构成负面。",
            "2. 正向迹象：主动关心、记得角色说过的小事、顺着角色的话题、对角色情绪做出"
            "恰当回应、产生默契、主动陪伴、配合角色的玩笑等。",
            "3. 负面迹象：恶意贬低角色人格、背叛信任、无视边界、持续骚扰、恶意争吵。"
            "日常玩笑打闹与口是心非的傲娇互动不属于负面。",
            "4. 琐碎闲聊、单句问候、无实质内容的互动应判定为无需变化。",
            "",
            "最近对话：",
        ]
        for msg in context_messages:
            sender = self._extract_sender_label(msg)
            content = str(msg.get("processed_plain_text") or msg.get("content") or "").strip()
            lines.append(f"[{sender}] {content}")

        lines.append("")
        lines.append("请只输出一个 JSON 对象，不要输出任何其他内容：")
        lines.append(
            '{"need_change": true或false, "direction": "positive"/"neutral"/"negative", '
            '"magnitude": 1-3, "reason": "一句话中文理由"}'
        )
        lines.append(
            "其中 magnitude：1=轻微，2=普通，3=明显；日常互动最高给 2，"
            "重大事件（如表白、送礼、重大帮助）通常由角色主动处理，自动评价一般给 1~2。"
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_sender_label(msg: dict[str, Any]) -> str:
        """从序列化消息中提取发送者展示名。

        Args:
            msg: 序列化消息字典

        Returns:
            发送者昵称/群名片；无法识别时返回「未知用户」
        """
        message_info = msg.get("message_info")
        if not isinstance(message_info, dict):
            return "未知用户"
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return "未知用户"
        label = str(
            user_info.get("user_cardname") or user_info.get("user_nickname") or ""
        ).strip()
        return label or "未知用户"

    @staticmethod
    def _parse_evaluation_result(raw_text: str) -> dict[str, Any] | None:
        """从模型输出中容错解析评价 JSON。

        兼容模型输出带 markdown 代码块或前后解释性文字的情况。

        Args:
            raw_text: 模型原始输出

        Returns:
            规范化后的评价结果；解析失败返回 None
        """
        text = raw_text.strip()
        # 去掉 markdown 代码块围栏
        if text.startswith("```"):
            lines = text.splitlines()
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None

        need_change = bool(data.get("need_change", False))
        direction = str(data.get("direction", "neutral") or "").strip().lower()
        if direction not in {"positive", "negative", "neutral"}:
            direction = "neutral"
        try:
            magnitude = int(float(data.get("magnitude", 1)))
        except (TypeError, ValueError):
            magnitude = 1
        magnitude = max(1, min(5, magnitude))
        reason = str(data.get("reason", "") or "").strip()[:200]
        return {
            "need_change": need_change,
            "direction": direction,
            "magnitude": magnitude,
            "reason": reason,
        }

    def _magnitude_to_change_value(self, direction: str, magnitude: int) -> float:
        """把评价幅度映射为配置范围内的好感度变化值。

        magnitude 1~5 线性映射到配置区间：
            positive: [positive_interaction_min, positive_interaction_max]
            negative: [negative_interaction_max, negative_interaction_min]

        Args:
            direction: 互动方向（positive / negative）
            magnitude: 评价幅度 1~5

        Returns:
            配置范围内的变化值（含符号）
        """
        cfg = self.config.affection_rule
        ratio = (max(1, min(5, magnitude)) - 1) / 4.0
        if direction == "positive":
            return cfg.positive_interaction_min + (
                cfg.positive_interaction_max - cfg.positive_interaction_min
            ) * ratio
        if direction == "negative":
            return cfg.negative_interaction_max - (
                cfg.negative_interaction_max - cfg.negative_interaction_min
            ) * ratio
        return 0.0

    def _build_affection_hint(self, user_id: str, *, for_replyer: bool = False) -> str:
        """构造写入消息窗口的好感度提示文本。

        提示只包含当前等级与回应态度，不枚举行为清单——互动是否值得改变好感度
        由插件内自动评价与 Planner 按上下文综合判断，不在此处灌输规则。

        Args:
            user_id: 权威用户标识（QQ 号）
            for_replyer: 是否用于 replyer 参考信息。为 True 时只保留好感度态度提示，
                不包含「调用 adjust_affection 工具」的引导——replyer 模型没有工具，
                若提示它调用工具，模型会以 <function_calls> 文本形式模仿并原样发出。

        Returns:
            提示文本；无有效数据时返回空字符串
        """
        record = self._affection_data.get(user_id)
        if record is None:
            # 尚无记录，给出中性提示即可
            record = self._get_user_record(user_id)
        affection_value = record.get("affection", 0.0)
        level_info = _get_affection_level_info(affection_value)
        response_hint = self._get_response_hint(affection_value)

        char_name = self.config.personality.character_name if self.config else "角色"
        lines = [
            f"【好感度提示】你对当前这位用户的好感度为 {level_info['value']} 点，"
            f"属于「{level_info['level_name']}」等级：{level_info['level_description']}。",
            f"回应时应体现：{response_hint}",
        ]
        lines.append(
            "好感度代表你内心对这个人的感觉，不是现实中的账本、档案或纸质记录。"
            "日常聊天自然流露态度，不主动把好感度说成后台系统；"
            "只有对方明确询问数值时才按真实数据回答。"
        )
        if record.get("locked", False):
            lines.append("（该用户好感度已被管理员锁定，请保持当前态度，不要试图改变。）")
        elif (
            not for_replyer
            and self.config
            and self.config.call_behavior.encourage_tool_calls
        ):
            lines.append(
                "如果本次互动与当前关系明显相关（值得加深或需要警惕），"
                "在回应之后可调用 adjust_affection 工具调整好感度。"
            )
        return "\n".join(lines)

    def _build_generic_affection_hint(self, *, for_replyer: bool = False) -> str:
        """构造不绑定具体用户的好感度提示文本。

        当无法从会话解析出发言者 QQ 号时使用（例如首次群聊、缓存未建立时），
        仅提醒 LLM 感知好感度系统并保持角色一致的亲近程度。

        Args:
            for_replyer: 是否用于 replyer 参考信息。为 True 时不包含工具调用引导，
                避免 replyer（无工具）以 <function_calls> 文本形式模仿并泄漏。

        Returns:
            提示文本
        """
        char_name = self.config.personality.character_name if self.config else "角色"
        lines = [
            f"【好感度提示】你与这位用户的好感度系统已启用，请根据过往互动自然回应，"
            f"态度与亲近程度保持角色一致（{char_name}的性格）。",
        ]
        if not for_replyer and self.config and self.config.call_behavior.encourage_tool_calls:
            lines.append(
                "如果本次互动与当前关系明显相关（值得加深或需要警惕），"
                "在回应之后可调用 adjust_affection 工具调整好感度。"
            )
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 工厂函数（MaiBot 加载入口）
# ════════════════════════════════════════════════════════════════


def create_plugin() -> AffectionPlugin:
    """MaiBot 插件工厂函数。

    MaiBot 运行时会调用此函数来创建插件实例。
    """
    return AffectionPlugin()
