"""Regression cases reconstructed from device runs."""
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
    def test_desktop_prompt_dismisses_only_in_execute_mode(self, _sleep):
        phone = object.__new__(g.Phone)
        phone.activity = Mock(return_value=(g.PKG, g.CENTER))
        phone.input = Mock()
        blocked = home_tree()
        node(blocked, "桌面入口提示")
        node(blocked, "立即领", bounds="[560,2100][950,2200]")
        node(blocked, "不需要", bounds="[100,2100][500,2200]")
        phone.tree = Mock(return_value=blocked)
        phone.log = None
        with self.assertRaisesRegex(RuntimeError, "桌面入口提示"):
            phone.page()
        phone.input.assert_not_called()
        phone.log = Mock()
        phone.tree.side_effect = [blocked, home_tree()]
        self.assertIsInstance(phone.page(), g.Page)
        phone.input.assert_called_once_with(g.CENTER, "tap", 300, 2150)
        phone.log.assert_called_once_with("center_prompt_dismissed", title="桌面入口提示")
        hidden = home_tree()
        for text in ("桌面入口提示", "立即领", "不需要"):
            node(hidden, text, bounds="[100,2356][500,2356]")
        phone.tree.side_effect = None
        phone.tree.return_value = hidden
        self.assertIsInstance(phone.page(), g.Page)
        self.assertEqual(phone.input.call_count, 1)

    @patch("gcenter.time.sleep")
    def test_disappearance_from_hot_view_is_inconclusive(self, _sleep):
        page = g.Page(home_tree())
        original = page.cards["hot"].pop()
        phone = Mock(width=1080)
        phone.page.return_value = page
        self.assertEqual(g.badge_result(phone, "hot", original), ("card_not_located", page))
        page.cards["hot"].append(original)
        original.coins = 0
        self.assertEqual(g.badge_result(phone, "hot", original), ("badge_removed", page))

    @patch("gcenter.time.sleep")
    def test_one_transient_clear_does_not_confirm(self, _sleep):
        marked = g.Page(home_tree())
        clear = g.Page(home_tree())
        clear.cards["hot"][0].coins = 0
        phone = Mock(width=1080)
        phone.page.side_effect = [marked, marked, clear]
        self.assertEqual(g.badge_result(phone, "hot", marked.cards["hot"][0]), ("unstable_page", clear))

    @patch("gcenter.time.sleep")
    def test_editor_replacement_requires_a_visible_slot(self, _sleep):
        page = g.Page(home_tree())
        card = page.cards["editor"][0]
        old = g.Card("旧游戏", 80, 10, card.target, card.area, card.layout)
        phone = Mock(width=1080)
        phone.page.return_value = page
        self.assertEqual(g.badge_result(phone, "editor", old), ("card_not_located", page))
        old.target = card.target = (40, 400, 240, 480)
        self.assertEqual(g.badge_result(phone, "editor", old, page.anchors["editor"]), ("slot_replaced", page))

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

    @patch("gcenter.visual_button")
    @patch("gcenter.time.sleep")
    def test_ad_redirect_while_reading_tree_restores_original_task(self, _sleep, visual):
        phone = Mock()
        ad = "com.windmill.sdk.widget.InterstitialView_4012001"
        current = g.PKG, ad
        external = "com.android.browser", "com.android.browser.BrowserActivity"
        phone.call.return_value = f"* Hist #3: ActivityRecord{{abc u0 {g.PKG}/{ad} t548}}"
        tree = ET.Element("hierarchy")
        button = node(tree, "关闭", bounds="[828,644][894,689]")
        button.attrib.update({"package": g.PKG, "resource-id": "tobid:id/tobid_interstitial_skip_text"})
        phone.tree.return_value = tree
        phone.activity.side_effect = [current, external, external, current, current, (g.PKG, g.GAME)]
        log = Mock()
        self.assertTrue(g.close_interstitial(phone, current, log))
        visual.assert_not_called()
        phone.call.assert_any_call("shell", "am", "task", "focus", "548")
        phone.input.assert_called_once_with(ad, "tap", 861, 666)
        log.assert_called_once_with("ad_redirect_recovered", activity=ad, redirect_package=external[0])

    @patch("gcenter.time.sleep")
    @patch("gcenter.close_interstitial", return_value=True)
    def test_game_launch_closes_interstitial_before_starting_timer(self, close, _sleep):
        phone = Mock()
        ad = g.PKG, "com.sigmob.sdk.base.common.PortraitTransparentAdActivity"
        phone.activity.side_effect = [ad, (g.PKG, g.GAME)]
        self.assertEqual(g.wait_game(phone), g.GAME)
        close.assert_called_once_with(phone, ad, phone.log)

    @patch("gcenter.time.sleep")
    def test_early_return_to_meituan_home_reopens_game_center(self, _sleep):
        phone = Mock()
        phone.activity.side_effect = [(g.PKG, g.MAIN)] * 8 + [(g.PKG, g.CENTER)]
        tree = ET.Element("hierarchy")
        node(tree, "游戏中心", bounds="[600,1900][730,2150]")
        phone.tree.return_value = tree
        log = Mock()
        g.play_seconds(phone, g.GAME, 122, log)
        log.assert_called_once_with("game_returned_early", activity=g.MAIN, remaining=122)
        g.return_to_center(phone, g.GAME)
        phone.input.assert_called_once_with(g.MAIN, "tap", 665, 2025)

    @patch("gcenter.time.sleep")
    def test_ad_web_return_requires_game_and_center_below_ad(self, _sleep):
        phone = Mock()
        ad = "com.windmill.sdk.widget.InterstitialView_4012003"
        current = g.PKG, g.AD_WEB
        stack = [g.AD_WEB, ad, g.GAME]
        def history(activities):
            return "\n".join(f"* Hist #{4-i}: ActivityRecord{{abc u0 {g.PKG}/{act} t597}}"
                             for i, act in enumerate(activities))
        phone.call.return_value = history(stack + [g.MAIN])
        self.assertFalse(g.back_from_ad_web(phone))
        phone.input.assert_not_called()
        phone.call.return_value = history(stack + [g.CENTER, g.MAIN])
        phone.activity.return_value = g.PKG, ad
        self.assertTrue(g.close_interstitial(phone, current))
        phone.input.assert_called_once_with(g.AD_WEB, "keyevent", "KEYCODE_BACK")

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
        # The second attempt reuses the last snapshot of the first result.
        phone.page.side_effect = [marked]*7 + [later]*3
        args = SimpleNamespace(serial=None, section="hot", limit=0, execute=True)
        events = []
        with self.assertRaisesRegex(RuntimeError, "指尖幻影"):
            g.run(args, lambda event, **fields: events.append((event, fields)))
        self.assertEqual(events[-1][1]["unresolved"], {"指尖幻影": "still_marked"})

    @patch("gcenter.time.sleep")
    @patch("gcenter.badge_result")
    @patch("gcenter.return_to_center")
    @patch("gcenter.play_seconds")
    @patch("gcenter.wait_game", return_value=g.GAME)
    @patch("gcenter.first_screen")
    @patch("gcenter.prepare_center")
    @patch("gcenter.Phone")
    def test_retry_sweep_finds_card_hidden_by_pagination(self, phone_cls, _prepare, first_screen,
                                                        _wait, _play, _back, badge_result, _sleep):
        marked = g.Page(home_tree())
        later = g.Page(home_tree())
        later.cards["hot"][0].name = "另一款游戏"
        later.cards["hot"][0].coins = 0
        clear = g.Page(home_tree())
        clear.cards["hot"][0].coins = 0
        phone = phone_cls.return_value
        phone.width, phone.height = 1080, 2400
        first_screen.side_effect = [marked, marked, clear]
        badge_result.side_effect = [("still_marked", later), ("badge_removed", clear)]
        phone.page.side_effect = [later]*2 + [clear]*2
        args = SimpleNamespace(serial=None, section="hot", limit=0, execute=True)
        events = []
        g.run(args, lambda event, **fields: events.append((event, fields)))
        self.assertEqual([f["attempt"] for e, f in events if e == "open"], [1, 2])
        self.assertIn(("hot_retry_scan", {"games": ["指尖幻影"]}), events)
        self.assertEqual(first_screen.call_count, 3)
        self.assertEqual(events[-1], ("run_completed", {"sections": ["hot"], "rounds": 2}))

    @patch("gcenter.time.sleep")
    @patch("gcenter.return_to_center")
    @patch("gcenter.play_seconds")
    @patch("gcenter.wait_game")
    @patch("gcenter.first_screen")
    @patch("gcenter.prepare_center")
    @patch("gcenter.Phone")
    def test_interrupted_round_rechecks_and_retries_in_same_run(
            self, phone_cls, prepare, first_screen, wait_game, play, _back, _sleep):
        marked = g.Page(home_tree())
        clear = g.Page(home_tree())
        clear.cards["recommend"][0].coins = 0
        phone = phone_cls.return_value
        phone.width = 1080
        phone.activity.return_value = g.PKG, g.CENTER
        prepare.return_value = marked
        first_screen.side_effect = [marked, clear]
        wait_game.side_effect = [g.GameTransitionError("启动超时"), g.GAME]
        phone.page.side_effect = [marked, marked, clear, clear]
        args = SimpleNamespace(serial=None, section="recommend", limit=0, execute=True)
        events = []
        g.run(args, lambda event, **fields: events.append((event, fields)))
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(phone.page.call_count, 4)
        self.assertEqual(play.call_count, 1)
        self.assertEqual([fields["attempt"] for event, fields in events if event == "open"], [1, 2])
        self.assertIn("round_recovered", [event for event, _ in events])
        self.assertEqual(events[-1], ("run_completed", {"sections": ["recommend"], "rounds": 2}))


if __name__ == "__main__":
    unittest.main()
