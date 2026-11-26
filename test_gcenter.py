"""Regression cases reconstructed from device runs."""
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

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
        timing = {"ad_recovery_seconds": 0}
        with patch("gcenter.time", clock), patch("gcenter.close_interstitial", side_effect=dismiss):
            g.play_seconds(phone, g.GAME, 4, log, timing=timing)
        self.assertEqual(elapsed[0], 26)
        self.assertEqual(timing["ad_recovery_seconds"], 20)
        log.assert_called_once_with("ad_closed", remaining=2, handling_seconds=20)

    @patch("gcenter.time.sleep")
    def test_ad_closing_in_place_needs_no_task_history(self, _sleep):
        phone = Mock()
        ad = "com.windmill.sdk.widget.InterstitialView_4012001"
        current = (g.PKG, ad)
        phone.activity.side_effect = [current, current, (g.PKG, g.GAME)]
        tree = ET.Element("hierarchy")
        close = node(tree, "关闭", bounds="[828,644][894,689]")
        close.attrib.update({"package": g.PKG, "resource-id": "tobid:id/tobid_interstitial_skip_text"})
        phone.tree.return_value = tree
        self.assertTrue(g.close_interstitial(phone, current))
        phone.input.assert_called_once_with(ad, "tap", 861, 666)
        phone.call.assert_not_called()

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
    @patch("gcenter.visual_button", return_value=(989, 560))
    def test_windmill_download_sheet_uses_its_close_icon(self, visual, _sleep):
        phone = Mock()
        current = g.PKG, "com.windmill.sdk.widget.InterstitialView_4012003"
        phone.call.return_value = ""
        tree = ET.Element("hierarchy")
        for text in ("应用大小", "立即下载", "隐私政策"):
            node(tree, text).set("package", g.PKG)
        phone.tree.return_value = tree
        phone.activity.side_effect = [current, current, current, (g.PKG, g.GAME)]
        self.assertTrue(g.close_interstitial(phone, current))
        visual.assert_called_once_with(phone, "windmill_download_close.png", (360, 140, 432, 375))
        phone.input.assert_called_once_with(current[1], "tap", 989, 560)

    @patch("gcenter.time.sleep")
    def test_early_return_to_meituan_home_reopens_game_center(self, _sleep):
        phone = Mock()
        phone.activity.side_effect = [(g.PKG, g.MAIN)] * 8 + [(g.PKG, g.CENTER)]
        tree = ET.Element("hierarchy")
        node(tree, "游戏中心", bounds="[241,266][331,297]")
        node(tree, "游戏中心", bounds="[600,1900][730,2150]").set("clickable", "true")
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
    @patch("gcenter.activity_history")
    def test_sigmob_full_screen_ad_can_return_to_original_interstitial(self, history, _sleep):
        phone = Mock()
        ad = "com.windmill.sdk.widget.InterstitialView_4012001"
        current = (g.PKG, g.AD_SCREEN)
        history.return_value = [(g.PKG, activity, "1067") for activity in (g.AD_SCREEN, ad, g.GAME, g.CENTER)]
        phone.activity.return_value = (g.PKG, ad)
        self.assertTrue(g.close_interstitial(phone, current))
        phone.input.assert_called_once_with(g.AD_SCREEN, "keyevent", "KEYCODE_BACK")
        phone.tree.assert_not_called()

    @patch("gcenter.time.sleep")
    @patch("gcenter.activity_history")
    def test_ad_web_uses_native_close_if_system_back_keeps_webview(self, history, _sleep):
        phone = Mock(width=1080, height=2400)
        ad = "com.windmill.sdk.widget.InterstitialView_4012003"
        current = (g.PKG, g.AD_WEB)
        history.return_value = [(g.PKG, activity, "1") for activity in (g.AD_WEB, ad, g.GAME, g.CENTER)]
        phone.activity.side_effect = [current, (g.PKG, ad)]
        tree = ET.Element("hierarchy")
        close = node(tree, "", bounds="[105,104][232,236]")
        close.attrib.update({"package": g.PKG, "clickable": "true", "resource-id": g.PKG + ":id/button_lr",
                             "content-desc": "关闭按钮"})
        phone.tree.return_value = tree
        log = Mock()
        self.assertTrue(g.back_from_ad_web(phone, log))
        self.assertEqual([c.args for c in phone.input.call_args_list],
                         [(g.AD_WEB, "keyevent", "KEYCODE_BACK"), (g.AD_WEB, "tap", 168, 170)])
        log.assert_any_call("ad_web_toolbar_tapped", control="关闭按钮")

    @patch("gcenter.time.sleep")
    @patch("gcenter.activity_history")
    @patch("gcenter.visual_button")
    def test_confirm_only_update_can_return_from_restarted_game(self, visual, history, _sleep):
        phone = Mock()
        game, updated = (g.PKG, g.GAME), (g.PKG, g.GAME + "1")
        phone.activity.side_effect = [game, updated, updated, (g.PKG, g.CENTER)]
        history.side_effect = [[(*game, "1"), (g.PKG, g.CENTER, "1")],
                               [(*updated, "1"), (g.PKG, g.CENTER, "1")]]
        visual.side_effect = [(1, 1), (990, 102)]
        tree = ET.Element("hierarchy")
        for name, bounds in (("更新提示", "[400,1000][600,1100]"),
                             ("新版本已经准备好，是否重启应用？", "[160,1150][910,1200]"),
                             ("确认", "[140,1269][940,1406]")):
            node(tree, name, bounds=bounds).set("package", g.PKG)
        phone.tree.return_value = tree
        g.return_to_center(phone, g.GAME)
        self.assertEqual([c.args for c in phone.input.call_args_list],
                         [(g.GAME, "tap", 540, 1337), (updated[1], "tap", 990, 102)])

    @patch("gcenter.time.sleep")
    @patch("gcenter.activity_history")
    @patch("gcenter.visual_button")
    def test_reappearing_update_can_restart_once_after_cancel(self, visual, history, _sleep):
        phone = Mock()
        game, updated = (g.PKG, g.GAME), (g.PKG, g.GAME + "1")
        phone.activity.side_effect = [game, game, updated, updated, (g.PKG, g.CENTER)]
        history.side_effect = [[(*game, "1"), (g.PKG, g.CENTER, "1")],
                               [(*updated, "1"), (g.PKG, g.CENTER, "1")]]
        visual.side_effect = [(1, 1), (990, 102)]
        tree = ET.Element("hierarchy")
        for text, bounds in (
            ("更新提示", "[400,1000][600,1100]"),
            ("新版本已经准备好，是否重启应用？", "[160,1150][910,1200]"),
            ("取消", "[140,1269][539,1406]"),
            ("确认", "[540,1269][940,1406]"),
        ):
            node(tree, text, bounds=bounds).set("package", g.PKG)
        phone.tree.return_value = tree
        g.return_to_center(phone, g.GAME)
        self.assertEqual([c.args for c in phone.input.call_args_list],
                         [(g.GAME, "tap", 339, 1337), (g.GAME, "tap", 740, 1337),
                          (updated[1], "tap", 990, 102)])

    @patch("gcenter.time.sleep")
    def test_restore_game_requires_center_directly_below_game_in_same_task(self, _sleep):
        phone = Mock()
        phone.activity.return_value = g.PKG, g.GAME
        game = f"* Hist #2: ActivityRecord{{abc u0 {g.PKG}/{g.GAME} t711}}"
        center = f"* Hist #1: ActivityRecord{{def u0 {g.PKG}/{g.CENTER} t710}}"
        phone.call.return_value = f"{game}\n{center}"
        self.assertFalse(g.restore_game_task(phone, g.GAME))
        self.assertEqual(phone.call.call_count, 1)
        phone.call.return_value = f"{game}\n{center.replace('t710', 't711')}"
        self.assertTrue(g.restore_game_task(phone, g.GAME))
        phone.call.assert_called_with("shell", "am", "task", "focus", "711")

    def test_desktop_wait_checks_launcher_and_restores_same_task(self):
        phone = Mock()
        launcher = ("com.miui.home", "com.miui.home.launcher.Launcher")
        phone.call.return_value = "priority=0\ncom.miui.home/.launcher.Launcher\n"
        phone.activity.return_value = launcher
        clock = Mock()
        elapsed = [0]

        def advance(seconds):
            elapsed[0] += seconds

        clock.monotonic.side_effect = lambda: elapsed[0]
        clock.sleep.side_effect = advance
        log = Mock()
        with (
            patch("gcenter.time", clock),
            patch("gcenter.game_task_id", return_value="711"),
            patch("gcenter.wait_game", return_value=g.GAME),
        ):
            self.assertTrue(g.desktop_wait(phone, g.GAME, 121, log))
        self.assertEqual(elapsed[0], 121)
        phone.input.assert_called_once_with(g.GAME, "keyevent", "KEYCODE_HOME")
        phone.call.assert_called_with("shell", "am", "task", "focus", "711")
        log.assert_any_call("desktop_timer_finished", seconds=121, waited_seconds=121, activity=g.GAME)

    def test_desktop_wait_does_not_focus_a_replaced_task(self):
        phone = Mock()
        phone.call.return_value = "com.miui.home/.launcher.Launcher"
        phone.activity.return_value = "com.miui.home", "com.miui.home.launcher.Launcher"
        with (
            patch("gcenter.game_task_id", side_effect=["711", "712"]),
            patch("gcenter.time.monotonic", side_effect=[0, 0, 1, 1]),
            patch("gcenter.time.sleep"),
        ):
            with self.assertRaisesRegex(g.GameTransitionError, "原游戏任务"):
                g.desktop_wait(phone, g.GAME, 1, Mock())
        self.assertEqual(phone.call.call_count, 1)

    def test_desktop_mode_retries_a_still_marked_card_in_foreground(self):
        marked = g.Page(home_tree())
        clear = g.Page(home_tree())
        clear.cards["recommend"][0].coins = 0
        phone = Mock(width=1080)
        args = SimpleNamespace(serial=None, section="recommend", limit=0, execute=True, timing="desktop")
        events = []
        with (
            patch("gcenter.prepare_center", return_value=marked),
            patch("gcenter.first_screen", side_effect=[marked, clear]),
            patch("gcenter.wait_game", return_value=g.GAME),
            patch("gcenter.desktop_wait", return_value=True) as desktop,
            patch("gcenter.play_seconds") as play,
            patch("gcenter.return_to_center"),
            patch("gcenter.badge_result", side_effect=[("still_marked", marked), ("badge_removed", clear)]),
            patch("gcenter.refresh_center", return_value=marked),
            patch("gcenter.random.randint", return_value=2),
        ):
            g.run(args, lambda event, **fields: events.append((event, fields)), phone=phone)
        desktop.assert_called_once()
        self.assertEqual(desktop.call_args.args[2], 66)
        play.assert_called_once()
        self.assertEqual(play.call_args.args[2], 67)
        checked = next(fields for event, fields in events if event == "section_checked")
        self.assertEqual(checked["planned_desktop_seconds"], 66)
        self.assertEqual(checked["planned_foreground_seconds"], 67)
        self.assertEqual(checked["unresolved"], {})

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
        self.assertEqual(events[-1], ("run_completed", {"sections": list(g.HEADERS), "rounds": 0,
                                                        "elapsed_seconds": ANY}))
        self.assertTrue(all(scan["elapsed_seconds"] >= 0 for scan in scans))
        self.assertGreaterEqual(events[-1][1]["elapsed_seconds"], 0)
        last = phone.input.call_args.args
        self.assertEqual(last[:2], (g.CENTER, "swipe"))
        self.assertLess(last[3], last[5])

    @patch("gcenter.time.sleep")
    @patch("gcenter.return_to_center")
    @patch("gcenter.play_seconds")
    @patch("gcenter.wait_game", return_value=g.GAME)
    @patch("gcenter.first_screen")
    @patch("gcenter.prepare_center")
    @patch("gcenter.refresh_center", return_value=None)
    @patch("gcenter.Phone")
    def test_unconfirmed_hot_card_survives_pagination(self, phone_cls, *_mocks):
        marked = g.Page(home_tree())
        later = g.Page(home_tree())
        later.cards["hot"][0].name = "另一款游戏"
        later.cards["hot"][0].coins = 0
        phone = phone_cls.return_value
        phone.width, phone.height = 1080, 2400
        phone.page.side_effect = [marked]*8 + [later]*3
        args = SimpleNamespace(serial=None, section="hot", limit=0, execute=True)
        events = []
        with self.assertRaisesRegex(RuntimeError, "指尖幻影"):
            g.run(args, lambda event, **fields: events.append((event, fields)))
        check = next(fields for event, fields in events if event == "section_checked")
        self.assertEqual(check["unresolved"], {"指尖幻影": "still_marked"})
        self.assertEqual(events[-1][0], "pending_recheck_skipped")

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
        badge_result.side_effect = [("card_not_located", later), ("badge_removed", clear)]
        phone.page.side_effect = [later]*2 + [clear]*2
        args = SimpleNamespace(serial=None, section="hot", limit=0, execute=True)
        events = []
        g.run(args, lambda event, **fields: events.append((event, fields)))
        self.assertEqual([f["attempt"] for e, f in events if e == "open"], [1, 2])
        self.assertIn(("hot_retry_scan", {"games": ["指尖幻影"]}), events)
        self.assertEqual(first_screen.call_count, 3)
        self.assertEqual(events[-1], ("run_completed", {"sections": ["hot"], "rounds": 2,
                                                        "elapsed_seconds": ANY}))

    @patch("gcenter.time.sleep")
    @patch("gcenter.return_to_center")
    @patch("gcenter.play_seconds")
    @patch("gcenter.wait_game")
    @patch("gcenter.first_screen")
    @patch("gcenter.prepare_center")
    @patch("gcenter.refresh_center")
    @patch("gcenter.Phone")
    def test_interrupted_round_rechecks_and_retries_in_same_run(
            self, phone_cls, refresh, prepare, first_screen, wait_game, play, _back, _sleep):
        marked = g.Page(home_tree())
        clear = g.Page(home_tree())
        clear.cards["recommend"][0].coins = 0
        phone = phone_cls.return_value
        phone.width = 1080
        phone.activity.return_value = g.PKG, g.CENTER
        prepare.return_value = marked
        refresh.return_value = marked
        first_screen.side_effect = [marked, clear]
        wait_game.side_effect = [g.GameTransitionError("启动超时"), g.GAME]
        phone.page.side_effect = [marked, marked, clear, clear]
        args = SimpleNamespace(serial=None, section="recommend", limit=0, execute=True)
        events = []
        diagnostic = Mock()
        g.run(args, lambda event, **fields: events.append((event, fields)), diagnostic=diagnostic)
        self.assertEqual(prepare.call_count, 2)
        diagnostic.assert_called_once_with(phone, "round_interrupted")
        self.assertEqual(phone.page.call_count, 4)
        self.assertEqual(play.call_count, 1)
        self.assertEqual([fields["attempt"] for event, fields in events if event == "open"], [1, 2])
        self.assertIn("round_recovered", [event for event, _ in events])
        self.assertEqual(events[-1], ("run_completed", {"sections": ["recommend"], "rounds": 2,
                                                        "elapsed_seconds": ANY}))

    def test_diagnostic_saves_png_and_reports_capture_failure(self):
        phone = Mock()
        png = b"\x89PNG\r\n\x1a\n"
        phone.screenshot.side_effect = [png, subprocess.TimeoutExpired("adb", 12)]
        log = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            logfile = Path(tmp) / "run.jsonl"
            g.save_diagnostic(phone, logfile, "round_interrupted", 1, log)
            g.save_diagnostic(phone, logfile, "stopped", 2, log)
            self.assertEqual((Path(tmp) / "run-001-round_interrupted.png").read_bytes(), png)
            self.assertFalse((Path(tmp) / "run-002-stopped.png").exists())
        log.assert_any_call("screenshot", state="round_interrupted", file="run-001-round_interrupted.png")
        log.assert_any_call("screenshot_unavailable", state="stopped", reason=ANY)

    def test_refresh_clears_stale_badge_without_repeating_known_45_second_timer(self):
        tree = home_tree()
        title = next(n for n in tree.iter() if g.label(n) == "小编精选")
        editor = next(n for n in tree.iter() if title in list(n))
        editor.insert(1, ET.Element("node", text="玩指定游戏 45 秒可得"))
        initial = g.Page(tree)
        # The section hint is gone during play, but it was read on the first page.
        marked = g.Page(home_tree())
        marked.cards["editor"] = [g.Card("旧游戏", 80, 10, (40, 400, 240, 480),
                                          (40, 400, 240, 480), "compact", "default")]
        clear = g.Page(home_tree())
        clear.cards["editor"] = [g.Card("旧游戏", 0, 0, (40, 400, 240, 480),
                                         (40, 400, 240, 480), "compact")]
        phone = Mock(width=1080)
        phone.page.side_effect = [marked]*3 + [clear]
        args = SimpleNamespace(serial=None, section="editor", limit=0, execute=True)
        events = []
        with (
            patch("gcenter.Phone", return_value=phone),
            patch("gcenter.prepare_center", return_value=initial),
            patch("gcenter.first_screen", side_effect=[marked, clear]),
            patch("gcenter.wait_game", return_value=g.GAME),
            patch("gcenter.play_seconds") as play,
            patch("gcenter.return_to_center"),
            patch("gcenter.refresh_center", return_value=clear) as refresh,
            patch("gcenter.random.randint", return_value=1),
            patch("gcenter.time.sleep"),
        ):
            g.run(args, lambda event, **fields: events.append((event, fields)))
        opens = [fields for event, fields in events if event == "open"]
        self.assertEqual([(f["seconds"], f["duration_source"]) for f in opens], [(45, "previous_section")])
        play.assert_called_once()
        self.assertEqual(play.call_args.args[2], 46)
        refresh.assert_called_once_with(phone)
        summary = next(fields for event, fields in events if event == "section_checked")
        self.assertEqual(summary["planned_foreground_seconds"], 46)
        self.assertEqual(summary["unresolved"], {})

    def test_missing_editor_card_is_resolved_by_focused_recheck_in_same_run(self):
        marked = g.Page(home_tree())
        reopened = g.Page(home_tree())
        bounds = (40, 400, 240, 480)
        marked.cards["editor"] = [g.Card("鲜果消消", 80, 10, bounds, bounds, "compact")]
        reopened.cards["editor"] = [g.Card("新游戏", 80, 0, bounds, bounds, "compact")]
        for page in (marked, reopened):
            page.cards["recommend"][0].coins = 0
            page.cards["hot"][0].coins = 0
        phone = Mock(width=1080, height=2400)
        phone.page.side_effect = [marked]*3 + [reopened]*3
        args = SimpleNamespace(serial=None, section="all", limit=0, execute=True)
        events = []
        with (
            patch("gcenter.Phone", return_value=phone),
            patch("gcenter.prepare_center", return_value=marked),
            patch("gcenter.first_screen", side_effect=[marked, reopened]),
            patch("gcenter.wait_game", return_value=g.GAME),
            patch("gcenter.play_seconds") as play,
            patch("gcenter.return_to_center"),
            patch("gcenter.refresh_center", return_value=reopened) as refresh,
            patch("gcenter.time.sleep"),
        ):
            g.run(args, lambda event, **fields: events.append((event, fields)))
        checks = [fields for event, fields in events if event == "pending_section_checked"]
        self.assertEqual(checks, [{"section": "editor", "still_marked": [], "unresolved": []}])
        play.assert_called_once()
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(events[-1][0], "run_completed")

    @patch("gcenter.time.sleep")
    @patch("gcenter.refresh_center")
    def test_focused_hot_recheck_keeps_missing_card_unresolved(self, refresh, _sleep):
        page = g.Page(home_tree())
        page.cards["hot"][0].coins = 0
        phone = Mock(width=1080, height=2400)
        phone.page.return_value = page
        refresh.return_value = page
        pending = {("hot", "翻页后不见的旧卡")}
        remaining, _ = g.recheck_pending(phone, pending, Mock())
        self.assertEqual(remaining, pending)
        self.assertEqual(phone.input.call_count, 2)


if __name__ == "__main__":
    unittest.main()
