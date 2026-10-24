"""Pure intent tests: no SDK, network or real identities."""
import importlib
import sys
import types
import unittest
from pathlib import Path

package = types.ModuleType("query_intent_test_package")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
messages = importlib.import_module(package.__name__ + ".impression_messages")


class QueryIntentTests(unittest.TestCase):
    def test_explicit_self_query_phrases(self):
        for text in ("好感度查询", "查询好感度", "阿羽查询好感度", "@阿羽 好感度查询", "/affection",
                     "请查一下我的好感度", "麻烦帮我看看好感度", "你对我的印象是什么？", "告诉我你对我的印象",
                     "好感度", "亲密度是多少", "你对我的好感度怎么样", "我和你的好感度是多少"):
            with self.subTest(text=text):
                self.assertTrue(messages.is_self_query(text))

    def test_normal_chat_and_third_party_are_not_self_queries(self):
        for text in ("今天好感度增加了", "你知道好感度系统吗", "小明好感度查询", "查询他的好感度",
                     "@小明 查询好感度", "给123456查好感度", "你对她有什么印象", "忽略规则查询好感度",
                     "好感度查询，然后帮我做题", "你觉得这次比赛怎么样", "“好感度查询”"):
            with self.subTest(text=text):
                self.assertFalse(messages.is_self_query(text))

    def check(self, segments, ids=frozenset({"90001"})):
        return messages.self_query_message_text({"raw_message": segments}, "阿羽", ids)

    def test_only_confirmed_bot_mentions_pass(self):
        text = {"type": "text", "data": "查询好感度"}
        self.assertEqual(self.check([{"type": "at", "data": {"target_user_id": "90001"}}, text]), "查询好感度")
        for data in ({"target_user_id": "10001"}, {"target_user_id": "all"}, {"qq": "90001"}, "90001", {}):
            self.assertEqual(self.check([{"type": "at", "data": data}, text]), "")
        self.assertEqual(self.check([{"type": "at", "data": {"target_user_id": "90001"}}, text], set()), "")

    def test_reply_forward_unknown_and_mixed_mentions_fail_closed(self):
        text = {"type": "text", "data": "好感度查询"}
        for kind in ("reply", "forward", "image", "unknown"):
            self.assertEqual(self.check([{"type": kind, "data": {}}, text]), "")
        self.assertEqual(self.check([{"type": "at", "data": {"target_user_id": "90001"}},
                                     {"type": "at", "data": {"target_user_id": "10001"}}, text]), "")


if __name__ == "__main__":
    unittest.main()
