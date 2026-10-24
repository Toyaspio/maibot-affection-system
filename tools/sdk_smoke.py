"""Run with the TEST container's real Python/SDK. No host data reads or LLM RPCs.

    python /path/to/plugin/tools/sdk_smoke.py

This checks installed-SDK imports, signatures and decorators, not live host routing.
It deliberately does not monkeypatch missing dependencies or install anything.
"""
import importlib
import asyncio
import importlib.metadata
import inspect
import json
import sys
import types
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    from maibot_sdk.capabilities.message import MessageCapability
    from maibot_sdk.capabilities.llm import LLMCapability
    from maibot_sdk.capabilities.chat import ChatCapability
    from maibot_sdk.capabilities.person import PersonCapability
    from maibot_sdk.capabilities.database import DatabaseCapability
    from maibot_sdk.context import PluginContext

    print("Python:", sys.version.split()[0])
    print("SDK:", importlib.metadata.version("maibot-plugin-sdk"))
    inspect.signature(MessageCapability.get_by_time_in_chat).bind(
        None, "synthetic-test-stream", start_time=1.0, end_time=2.0,
        limit=500, limit_mode="latest", filter_mai=True, filter_command=True)
    inspect.signature(LLMCapability.generate).bind(
        None, "synthetic test", model="utils", temperature=0.6, max_tokens=900)
    inspect.signature(ChatCapability.get_group_streams).bind(None, platform="qq")
    inspect.signature(PersonCapability.get_id).bind(None, platform="qq", user_id="10001")
    inspect.signature(PersonCapability.get_value).bind(None, "synthetic-person", "memory_points")
    inspect.signature(DatabaseCapability.get).bind(None, "BotPlatformAccount", filters={"platform": "qq", "disabled": False}, limit=20)
    package = types.ModuleType("affection_sdk_smoke")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    module = importlib.import_module(package.__name__ + ".plugin")
    config = importlib.import_module(package.__name__ + ".config").AffectionConfig()
    assert not config.impression.enabled
    module.create_plugin()  # SDK base class and decorator declarations, no on_load.
    async def real_context_mention_check():
        calls = []
        async def fake_rpc(*args, **kwargs):
            calls.append((args, kwargs))
            return [{"platform": "qq", "account_id": "90001", "disabled": False}]
        context = PluginContext(plugin_id="feyumy.affection-system", rpc_call=fake_rpc)
        assert isinstance(context.db, DatabaseCapability)
        receiver = types.SimpleNamespace(ctx=context, _impression_bot_ids=set(),
            _impression_bot_lookup_at=0.0, _impression_bot_lookup_lock=asyncio.Lock())
        await module.AffectionPlugin._refresh_impression_bot_ids(receiver)
        assert receiver._impression_bot_ids == {"90001"}, "real SDK context bot-account lookup failed"
        assert len(calls) == 1
        messages = importlib.import_module(package.__name__ + ".impression_messages")
        message = {"raw_message": [{"type": "at", "data": {"target_user_id": "90001"}},
                                   {"type": "text", "data": "好感度查询"}]}
        assert messages.self_query_message_text(message, "阿羽", receiver._impression_bot_ids)
    asyncio.run(real_context_mention_check())
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    assert "message.get_by_time_in_chat" in manifest["capabilities"]
    assert {"chat.get_group_streams", "person.get_id", "person.get_value", "database.get"}.issubset(manifest["capabilities"])
    print("PASS: imports, declarations, config defaults, RPC signatures and real PluginContext.db mention authentication")
    print("NOT TESTED: runner hooks, capability permissions, host history format, real LLM")


if __name__ == "__main__":
    main()
