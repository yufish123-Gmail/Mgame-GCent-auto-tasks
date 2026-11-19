"""Regression cases reconstructed from the November 19 device run."""
import subprocess
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gcenter as g


def node(parent, text="", kind="android.widget.TextView", bounds="[40,400][240,480]"):
    return ET.SubElement(parent, "node", {"class": kind, "text": text, "bounds": bounds})


def home_tree():
    tree = ET.Element("hierarchy")
    web = node(tree, kind="android.webkit.WebView", bounds="[0,0][1080,2400]")
    node(node(node(web, bounds="[0,0][1080,200]")), "玩游戏")
    recommended = node(web)
    node(recommended, "好游推荐")
    card = node(node(recommended))
    node(card, "试玩游戏")
    node(card, "试玩65秒+10")
    node(card, "去玩")
    editor = node(web)
    node(editor, "小编精选")
    for name in ("旧游戏", "新游戏"):
        card = node(editor)
        node(card, name, bounds="[38,2356][222,2356]")
        if name == "旧游戏":
            node(card, "+10")
    hot = node(web)
    node(hot, "热门内容")
    node(hot, "任务已完成，继续发现更多游戏")
    grid = node(node(hot), kind="android.widget.GridView")
    card = node(node(node(grid)))
    node(card, "指尖幻影")
    node(card, "+")
    node(card, "10")
    return tree


class GameCenterTests(unittest.TestCase):
    def test_completed_heading_keeps_badge_and_collapsed_editor_replacement(self):
        page = g.Page(home_tree())
        hot, = page.cards["hot"]
        self.assertIn("hot", page.completed)
        self.assertEqual((hot.name, hot.coins, hot.seconds, hot.duration_source),
                         ("指尖幻影", 10, 120, "default"))
        self.assertEqual([(c.name, c.coins) for c in page.cards["editor"]], [("新游戏", 0)])
        self.assertEqual(page.cards["recommend"][0].seconds, 65)

    @patch("gcenter.time.sleep")
    def test_incomplete_xml_and_timeout_retry(self, _sleep):
        phone = object.__new__(g.Phone)
        phone.call = Mock(side_effect=["<?xml version='1.0'?><hierarchy><node></hierarchy>",
                                       subprocess.TimeoutExpired("adb", 22),
                                       "<?xml version='1.0'?><hierarchy></hierarchy>"])
        self.assertEqual(phone.tree().tag, "hierarchy")
        self.assertEqual(phone.call.call_count, 3)

    @patch("gcenter.time.sleep")
    def test_disappearance_from_hot_view_is_inconclusive(self, _sleep):
        page = g.Page(home_tree())
        original = page.cards["hot"].pop()
        phone = Mock(width=1080)
        phone.page.return_value = page
        self.assertEqual(g.badge_result(phone, "hot", original), "card_not_located")
        page.cards["hot"].append(original)
        original.coins = 0
        self.assertEqual(g.badge_result(phone, "hot", original), "badge_removed")

    @patch("gcenter.time.sleep")
    def test_one_transient_clear_does_not_confirm(self, _sleep):
        marked = g.Page(home_tree())
        clear = g.Page(home_tree())
        clear.cards["hot"][0].coins = 0
        phone = Mock(width=1080)
        phone.page.side_effect = [marked, marked, clear]
        self.assertEqual(g.badge_result(phone, "hot", marked.cards["hot"][0]), "unstable_page")

    @patch("gcenter.time.sleep")
    def test_editor_replacement_requires_a_visible_slot(self, _sleep):
        page = g.Page(home_tree())
        card = page.cards["editor"][0]
        old = g.Card("旧游戏", 80, 10, card.target, card.area, card.layout)
        phone = Mock(width=1080)
        phone.page.return_value = page
        self.assertEqual(g.badge_result(phone, "editor", old), "card_not_located")
        old.target = card.target = (40, 400, 240, 480)
        self.assertEqual(g.badge_result(phone, "editor", old, page.anchors["editor"]), "slot_replaced")

    @patch("gcenter.return_to_center")
    def test_resume_requires_game_and_center_in_same_task(self, back):
        phone = Mock()
        ad = "com.sigmob.sdk.base.common.PortraitTransparentAdActivity"
        phone.activity.return_value = g.PKG, ad
        def entry(i, cls, task):
            return f"* Hist  #{i}: ActivityRecord{{abc u0 {g.PKG}/{cls} t{task}}}"
        stack = [entry(3, ad, 418), entry(2, g.GAME, 418)]
        phone.call.return_value = "\n".join(stack + [entry(1, g.CENTER, 419)])
        with self.assertRaises(RuntimeError):
            g.prepare_center(phone, Mock())
        back.assert_not_called()
        phone.call.return_value = "\n".join(stack + [entry(1, g.CENTER, 418)])
        self.assertIs(g.prepare_center(phone, Mock()), phone.page.return_value)
        back.assert_called_once_with(phone, g.GAME)

    def test_ad_time_is_excluded_from_foreground_timer(self):
        clock = Mock()
        elapsed = [0]
        def advance(seconds):
            elapsed[0] += seconds
        clock.monotonic.side_effect = lambda: elapsed[0]
        clock.sleep.side_effect = advance
        game = g.PKG, g.GAME
        ad = g.PKG, "com.windmill.sdk.widget.InterstitialView_4012003"
        phone = Mock()
        phone.activity.side_effect = [game, ad, game, game]
        phone.awake.return_value = True
        def dismiss(*_):
            advance(20)
            return True
        log = Mock()
        with patch("gcenter.time", clock), patch("gcenter.close_interstitial", side_effect=dismiss):
            g.play_seconds(phone, g.GAME, 4, log)
        self.assertEqual(elapsed[0], 26)
        log.assert_called_once_with("ad_closed", remaining=2)

    @patch("gcenter.time.sleep")
    @patch("gcenter.Phone")
    def test_full_run_with_no_badges_returns_to_first_screen(self, phone_cls, _sleep):
        page = g.Page(home_tree())
        for cards in page.cards.values():
            for card in cards:
                card.coins = 0
        phone = phone_cls.return_value
        phone.width, phone.height = 1080, 2400
        phone.activity.return_value = g.PKG, g.CENTER
        phone.page.return_value = page
        args = SimpleNamespace(serial=None, section="all", limit=0, execute=True)
        events = []
        g.run(args, lambda event, **fields: events.append((event, fields)))
        scans = [fields for event, fields in events if event == "section_checked"]
        self.assertEqual([s["section"] for s in scans], list(g.HEADERS))
        self.assertEqual(scans[-1]["scrolls"], 2)
        self.assertEqual(events[-1], ("run_completed", {"sections": list(g.HEADERS), "rounds": 0}))
        last = phone.input.call_args.args
        self.assertEqual(last[:2], (g.CENTER, "swipe"))
        self.assertLess(last[3], last[5])

    @patch("gcenter.time.sleep")
    @patch("gcenter.return_to_center")
    @patch("gcenter.play_seconds")
    @patch("gcenter.wait_game", return_value=g.GAME)
    @patch("gcenter.first_screen")
    @patch("gcenter.prepare_center")
    @patch("gcenter.Phone")
    def test_unconfirmed_hot_card_survives_pagination(self, phone_cls, *_mocks):
        marked = g.Page(home_tree())
        later = g.Page(home_tree())
        later.cards["hot"][0].name = "另一款游戏"
        later.cards["hot"][0].coins = 0
        phone = phone_cls.return_value
        phone.width, phone.height = 1080, 2400
        # Each attempt reads the card, then reads three still-marked snapshots.
        phone.page.side_effect = [marked]*9 + [later]*3
        args = SimpleNamespace(serial=None, section="hot", limit=0, execute=True)
        events = []
        with self.assertRaisesRegex(RuntimeError, "指尖幻影"):
            g.run(args, lambda event, **fields: events.append((event, fields)))
        self.assertEqual(events[-1][1]["unresolved"], {"指尖幻影": "still_marked"})


if __name__ == "__main__":
    unittest.main()
