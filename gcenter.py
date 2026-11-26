"""Drive visible Game Center controls on an authorized Android phone."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

PKG = "com.sankuai.meituan"
CENTER = "com.sankuai.titans.adapter.mtapp.KNBWebViewActivity"
GAME = "com.meituan.android.mgc.container.MGCGameActivity"
MAIN = "com.meituan.android.pt.homepage.activity.MainActivity"
AD_WEB = "com.sankuai.waimai.business.knb.WMMultiWebViewActivity"
AD_SCREEN = "com.sigmob.sdk.base.common.PortraitAdActivity"
HEADERS = {"recommend": "好游推荐", "editor": "小编精选", "hot": "热门内容"}
SECTION_SECONDS = {"recommend": 60, "editor": 80, "hot": 120}
ROOT = Path(__file__).resolve().parent


def label(n):
    return (n.get("text") or n.get("content-desc") or "").strip()


def box(n):
    return tuple(map(int, re.findall(r"-?\d+", n.get("bounds", ""))))


def center_of(bounds):
    if len(bounds) == 4:
        x, y, right, bottom = bounds
        if right > x and bottom > y:
            return (x+right)//2, (y+bottom)//2
    return None


class ForegroundChanged(RuntimeError):
    pass


class GameTransitionError(RuntimeError):
    pass


@dataclass
class Card:
    name: str
    seconds: int
    coins: int
    target: tuple
    area: tuple
    layout: str
    duration_source: str = "section"


class Page:
    def __init__(self, tree):
        parent = {c: n for n in tree.iter() for c in n}
        texts = {label(n): n for n in tree.iter() if label(n)}
        if "玩游戏" not in texts or not all(h in texts for h in HEADERS.values()):
            raise RuntimeError("当前窗口没有完整的游戏中心首页标识")
        self.tree = tree
        header = parent[parent[texts["玩游戏"]]]
        self.top = box(header)[3]
        web = next((n for n in tree.iter() if n.get("class") == "android.webkit.WebView"), None)
        if web is None:
            raise RuntimeError("未识别游戏中心 WebView")
        self.bottom = min((box(parent[texts[s]])[1] for s in ("去桌面", "领VIP福利", "赚钱")
                           if s in texts and box(parent[texts[s]])[1] > self.top), default=box(web)[3])
        self.cards = {}
        self.anchors = {}
        self.completed = set()
        self.section_seconds = {}
        for key, heading in HEADERS.items():
            h = texts[heading]
            siblings = list(parent[h])
            tail = siblings[siblings.index(h) + 1:]
            if key == "editor":
                cards = [n for n in tail if len(n)]
                info = "".join(label(n) for n in tail[:2])
            else:
                container = next((n for n in tail if len(n)), None)
                if container is None:
                    raise RuntimeError(f"{heading}卡片容器未识别")
                cards = list(container)
                if key == "hot":
                    grid = next((n for n in container.iter() if n.get("class") == "android.widget.GridView"), None)
                    if grid is None or not len(grid):
                        raise RuntimeError("热门内容网格未识别")
                    cards = [c for wrap in grid[0] for c in wrap if len(c)]
                info = "".join(label(n) for n in tail[:4])
            if any(label(n).startswith("任务已完成") for n in tail[:2]):
                self.completed.add(key)
            sec = re.search(r"玩指定游戏\s*(\d+)\s*秒", info)
            if sec:
                self.section_seconds[key] = int(sec[1])
            self.cards[key] = []
            self.anchors[key] = box(h)
            for node in cards:
                direct = [n for n in node if n.get("class") == "android.widget.TextView" and label(n)]
                go = next((n for n in direct if label(n) in ("去玩", "玩", "玩玩")), None)
                names = [n for n in direct if label(n) not in ("热", "新", "+", "去玩", "玩", "玩玩")
                         and not label(n).isdigit() and not label(n).startswith("+")]
                if not names:
                    continue
                labels = [label(n) for n in node.iter() if label(n)]
                marker = next((m for s in labels if (m := re.fullmatch(
                    r"(?:(?:试)?玩(\d+)秒)?(?:金币)?\+(\d+)", re.sub(r"\s+", "", s)))), None)
                seconds = int(sec[1]) if sec else 0
                coins = 0
                if marker:
                    seconds = int(marker[1]) if marker[1] else seconds
                    coins = int(marker[2])
                elif "+" in labels:
                    idx = labels.index("+") + 1
                    if idx < len(labels) and labels[idx].isdigit():
                        coins = int(labels[idx])
                source = "card" if marker and marker[1] else "section"
                if coins and sec is None and not (marker and marker[1]):
                    seconds = SECTION_SECONDS[key]
                    source = "default"
                target = box(go if go is not None else names[0])
                layout = "compact" if key == "editor" and go is None else key
                self.cards[key].append(Card(label(names[0]), seconds, coins, target, box(node), layout, source))
            # The WebView keeps replaced compact cards behind the current card at the same position.
            if key == "editor":
                front = {c.target: c for c in self.cards[key] if c.layout == "compact"
                         and c.target[2] > c.target[0]}
                self.cards[key] = [c for c in self.cards[key] if c.layout != "compact"
                                   or front.get(c.target, c) is c]

    def point(self, card, width):
        x, y, right, bottom = card.target
        mid = (x + right) // 2, (y + bottom) // 2
        if right-x >= 38 and bottom-y >= 29 and 12 < mid[0] < width-12 and self.top+10 < mid[1] < self.bottom-10:
            return mid
        return None


class Phone:
    def __init__(self, serial, log=None):
        self.log = log
        exe = shutil.which("adb") or Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools/adb.exe"
        self.args = [str(exe)] + (["-s", serial] if serial else ["-d"])
        size = re.findall(r"(\d+)x(\d+)", self.call("shell", "wm", "size"))
        if not size:
            raise RuntimeError("无法确认手机屏幕尺寸")
        self.width, self.height = map(int, size[-1])

    def call(self, *args):
        proc = subprocess.run(self.args + list(map(str, args)), capture_output=True, timeout=22)
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode(errors="replace").strip())
        return proc.stdout.decode("utf-8", errors="replace")

    def activity(self):
        s = self.call("shell", "dumpsys", "activity", "activities")
        m = re.search(r"(?:topResumedActivity=|mResumedActivity:)[^\n]*?\s([\w.]+)/([\w.$]+)", s)
        if not m:
            return "", ""
        pkg, cls = m.groups()
        return pkg, pkg + cls if cls.startswith(".") else cls

    def tree(self, compressed=False, attempts=3):
        flags = ["--compressed"] if compressed else []
        for attempt in range(attempts):
            try:
                raw = self.call("exec-out", "uiautomator", "dump", *flags, "/dev/tty")
                start, end = raw.find("<?xml"), raw.rfind("</hierarchy>")
                if 0 <= start < end:
                    return ET.fromstring(raw[start:end+len("</hierarchy>")])
                reason = "界面树不完整"
            except (RuntimeError, ET.ParseError, subprocess.TimeoutExpired) as exc:
                reason = str(exc)
            if attempt + 1 < attempts:
                time.sleep(.5)
        raise RuntimeError(f"尝试 {attempts} 次后仍未读取完整界面树 ({reason})")

    def screenshot(self):
        return subprocess.check_output(self.args + ["exec-out", "screencap", "-p"], timeout=12)

    def page(self):
        dismissed = False
        for attempt in range(4):
            if self.activity() != (PKG, CENTER):
                raise RuntimeError("请打开美团游戏中心首页")
            tree = self.tree()
            labels = {label(node) for node in tree.iter() if center_of(box(node))}
            if {"桌面入口提示", "立即领"} <= labels:
                buttons = [point for node in tree.iter() if label(node) == "不需要"
                           and (point := center_of(box(node)))]
                if self.log is None or dismissed or len(buttons) != 1:
                    raise RuntimeError("桌面入口提示遮挡游戏中心，请关闭弹窗后继续")
                self.input(CENTER, "tap", *buttons[0])
                self.log("center_prompt_dismissed", title="桌面入口提示")
                dismissed = True
                time.sleep(.8)
                continue
            try:
                return Page(tree)
            except RuntimeError:
                if attempt == 3:
                    raise
                time.sleep(.8)
        raise RuntimeError("关闭桌面入口提示后未能读取游戏中心")

    def awake(self):
        return "mWakefulness=Awake" in self.call("shell", "dumpsys", "power")

    def input(self, activity, *args):
        if self.activity() != (PKG, activity):
            raise ForegroundChanged("操作前前台页面已改变")
        self.call("shell", "input", *args)

    def reveal(self, page, key, card):
        x, y, r, b = card.target
        peers = [c for c in page.cards[key] if c.layout == card.layout]
        visible = [c for c in peers if page.point(c, self.width)]
        if (b == y == 0 or r-x < 38) and visible and (key == "recommend" or card.layout == "compact"):
            anchor = visible[0]
            yy = sum(anchor.area[1::2]) // 2
            left, right = int(self.width*.16), int(self.width*.83)
            start,end = (right,left) if peers.index(card) > peers.index(anchor) else (left,right)
            self.input(CENTER, "swipe", start, yy, end, yy, 480)
        else:
            if b == y == 0:
                y = next((c.area[1] for c in peers if c.area[3] > c.area[1]), page.anchors[key][1])
                b = y
            low = int(self.height*.78)
            high = int(self.height*.35)
            start,end = (low,high) if (y+b)//2 >= (page.bottom+page.top)//2 else (high,low)
            self.input(CENTER, "swipe", self.width//2, start, self.width//2, end, 540)
        time.sleep(.7)


def is_game(activity):
    return re.fullmatch(re.escape(GAME) + r"\d*", activity) is not None


def is_interstitial(activity):
    return (re.fullmatch(r"com\.windmill\.sdk\.widget\.InterstitialView_\d+", activity) is not None
            or activity == "com.sigmob.sdk.base.common.PortraitTransparentAdActivity")


def wait_game(phone):
    for _ in range(16):
        pkg, act = phone.activity()
        if pkg == PKG and is_game(act):
            return act
        if pkg == PKG and (is_interstitial(act) or act in (AD_WEB, AD_SCREEN)):
            if not close_interstitial(phone, (pkg, act), phone.log):
                break
        time.sleep(.65)
    raise GameTransitionError("未确认小游戏已在前台，未开始计时")


def visual_button(phone, filename, region, raw=None):
    import cv2
    import numpy as np
    raw = phone.screenshot() if raw is None else raw
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.shape[0] <= image.shape[1]:
        return None
    height, width = image.shape
    small = cv2.resize(image, (432, 960))
    template = cv2.imdecode(np.frombuffer((ROOT/"templates"/filename).read_bytes(), np.uint8), 0)
    left, top, right, bottom = region
    scores = cv2.matchTemplate(small[top:bottom, left:right], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    if score >= .92:
        return round((left+x+template.shape[1]/2)*width/432), round((top+y+template.shape[0]/2)*height/960)
    return None


def return_to_center(phone, act):
    handled_updates = set()
    update_task = None
    for attempt in range(6):
        current = phone.activity()
        if current == (PKG, CENTER):
            return
        if current == (PKG, MAIN):
            entries = [(node, point) for node in phone.tree().iter() if label(node) == "游戏中心"
                       and (point := center_of(box(node)))]
            clickable = [point for node, point in entries if node.get("clickable") == "true"]
            targets = clickable or [point for _, point in entries]
            if len(targets) != 1:
                raise RuntimeError("已回到美团主页，请打开游戏中心复查金币标识")
            phone.input(MAIN, "tap", *targets[0])
            time.sleep(2)
            continue
        if update_task is not None and current[0] == PKG and current[1] != act and is_game(current[1]):
            chain = [(owner, activity) for owner, activity, task in activity_history(phone) if task == update_task]
            if chain[:2] == [current, (PKG, CENTER)]:
                act = current[1]
        if current != (PKG, act):
            if close_interstitial(phone, current):
                continue
            break
        if attempt == 0:
            raw = phone.screenshot()
            prompt = visual_button(phone, "game_update.png", (153, 391, 280, 466), raw)
            point = None if prompt else visual_button(phone, "game_return.png", (350, 0, 432, 85), raw)
            if point:
                phone.input(act, "tap", *point)
                time.sleep(1.5)
                continue
        try:
            tree = phone.tree(compressed=True)
        except (RuntimeError, subprocess.TimeoutExpired):
            tree = None
        nodes = list(tree.iter()) if tree is not None else []
        labels = {label(n) for n in nodes if n.get("package") == PKG and center_of(box(n))}
        restart_prompt = any(re.fullmatch(r"新版本已经准备好[，,](?:是否重启(?:应用|游戏)[？?]|请重启应用(?:后继续[~～]?)?)", s)
                             for s in labels)
        if "更新提示" in labels and restart_prompt:
            choice = next((name for name in ("取消", "确认") if name in labels and name not in handled_updates), None)
            actions = [point for n in nodes if n.get("package") == PKG and label(n) == choice
                       and (point := center_of(box(n)))]
            if len(actions) == 1:
                if choice == "确认":
                    history = activity_history(phone)
                    candidates = {task for owner, activity, task in history if (owner, activity) == current}
                    if len(candidates) != 1:
                        raise GameTransitionError("无法确认更新提示所属的游戏任务")
                    update_task = candidates.pop()
                    chain = [(owner, activity) for owner, activity, task in history if task == update_task]
                    if chain[:2] != [current, (PKG, CENTER)]:
                        raise GameTransitionError("游戏更新提示与游戏中心不在预期任务栈中")
                phone.input(act, "tap", *actions[0])
                if phone.log:
                    phone.log("game_update_dismissed", activity=act, control=choice)
                handled_updates.add(choice)
                time.sleep(1.5)
                continue
        buttons = [n for n in nodes if label(n) == "返回" and
                   n.get("package") == PKG and n.get("clickable") == "true" and
                   n.get("resource-id") == PKG + ":id/hao"]
        if len(buttons) == 1 and phone.activity() == current:
            x, y, right, bottom = box(buttons[0])
            phone.input(act, "tap", (x+right)//2, (y+bottom)//2)
        elif phone.activity() == current:
            point = visual_button(phone, "game_return.png", (350, 0, 432, 85))
            if point:
                phone.input(act, "tap", *point)
            else:
                phone.input(act, "keyevent", "KEYCODE_BACK")
        time.sleep(1.5)
    if phone.activity() != (PKG, CENTER):
        raise GameTransitionError("游戏退出后未回到游戏中心")


def close_interstitial(phone, current, log=None):
    pkg, activity = current
    if pkg == PKG and activity in (AD_WEB, AD_SCREEN):
        return back_from_ad_web(phone, log, activity)
    if pkg != PKG or not is_interstitial(activity):
        return False
    task = None

    def restore_redirect(package):
        nonlocal task
        if task is None:
            tasks = {task_id for owner, name, task_id in activity_history(phone) if (owner, name) == current}
            if len(tasks) != 1:
                return False
            task = tasks.pop()
        phone.call("shell", "am", "task", "focus", task)
        if log:
            log("ad_redirect_recovered", activity=activity, redirect_package=package)
        return True

    for _ in range(8):
        foreground = phone.activity()
        if foreground != current:
            if foreground[0] == PKG:
                return True
            if foreground[0] and not restore_redirect(foreground[0]):
                return False
            time.sleep(.7)
            continue
        point = None
        try:
            tree = phone.tree(compressed=True, attempts=1)
        except (RuntimeError, subprocess.TimeoutExpired):
            if "InterstitialView_" in activity and phone.activity() == current:
                point = visual_button(phone, "interstitial_close.png", (285, 130, 385, 330))
        else:
            close = [n for n in tree.iter() if n.get("package") == PKG and
                     (label(n) in ("关闭", "跳过") or n.get("resource-id") == "close_btn")]
            if len(close) == 1 and (close[0].get("resource-id", "").endswith("/tobid_interstitial_skip_text")
                                    or any(label(n) == "广告" for n in tree.iter())):
                point = center_of(box(close[0]))
            if point is None and "InterstitialView_" in activity and phone.activity() == current:
                labels = {label(n) for n in tree.iter() if n.get("package") == PKG and center_of(box(n))}
                if {"应用大小", "立即下载", "隐私政策"} <= labels:
                    point = visual_button(phone, "windmill_download_close.png", (360, 140, 432, 375))
        if (point is None and activity == "com.sigmob.sdk.base.common.PortraitTransparentAdActivity"
                and phone.activity() == current):
            point = visual_button(phone, "sigmob_close.png", (385, 48, 431, 108))
        if point is not None and phone.activity() == current:
            try:
                phone.input(activity, "tap", *point)
            except ForegroundChanged:
                continue
            # Some ads briefly keep the same activity before opening the browser.
            for _ in range(6):
                time.sleep(.5)
                pkg, after = phone.activity()
                if pkg == PKG and after != activity:
                    return True
                if pkg and pkg != PKG:
                    if not restore_redirect(pkg):
                        return False
                    time.sleep(.7)
                    break
        else:
            time.sleep(.7)
    after = phone.activity()
    return after != current and after[0] == PKG


def activity_history(phone):
    stack = phone.call("shell", "dumpsys", "activity", "activities")
    return re.findall(r"\* Hist\s+#\d+: ActivityRecord\{[^\n}]*?\s([\w.]+)/([\w.$]+) t(\d+)\b", stack)


def back_from_ad_web(phone, log=None, landing=AD_WEB):
    current = PKG, landing
    entries = activity_history(phone)
    for owner, activity, task in entries:
        if (owner, activity) != current:
            continue
        chain = [(p, a) for p, a, t in entries if t == task]
        if (len(chain) < 4 or chain[0] != current or chain[3] != (PKG, CENTER)
                or chain[1][0] != PKG or not is_interstitial(chain[1][1])
                or chain[2][0] != PKG or not is_game(chain[2][1])):
            continue
        phone.input(landing, "keyevent", "KEYCODE_BACK")
        time.sleep(.8)
        foreground = phone.activity()
        if foreground == current:
            if landing != AD_WEB:
                return False
            try:
                tree = phone.tree(compressed=True, attempts=1)
            except (RuntimeError, subprocess.TimeoutExpired):
                return False
            buttons = {}
            for node in tree.iter():
                control = (node.get("resource-id"), node.get("content-desc"))
                if (node.get("package") == PKG and node.get("clickable") == "true"
                        and control in {(PKG + ":id/button_ll", "返回按钮"), (PKG + ":id/button_lr", "关闭按钮")}
                        and (point := center_of(box(node)))
                        and 0 < point[0] < phone.width * .23 and 0 < point[1] < phone.height * .12):
                    buttons.setdefault(control[1], []).append(point)
            targets = buttons.get("关闭按钮", buttons.get("返回按钮", []))
            if len(targets) != 1:
                return False
            phone.input(AD_WEB, "tap", *targets[0])
            if log:
                log("ad_web_toolbar_tapped", control="关闭按钮" if "关闭按钮" in buttons else "返回按钮")
            time.sleep(.8)
            foreground = phone.activity()
        if foreground in chain[1:4]:
            if log:
                log("ad_web_closed", activity=landing)
            return True
    return False


def game_task_id(phone, act):
    entries = activity_history(phone)
    tasks = {task for _, _, task in entries}
    matches = []
    for task in tasks:
        chain = [(pkg, activity) for pkg, activity, owner in entries if owner == task]
        if chain[:2] == [(PKG, act), (PKG, CENTER)]:
            matches.append(task)
    return matches[0] if len(matches) == 1 else None


def restore_game_task(phone, act):
    task = game_task_id(phone, act)
    if task is None:
        return False
    phone.call("shell", "am", "task", "focus", task)
    time.sleep(.8)
    return phone.activity() == (PKG, act)


def desktop_wait(phone, act, duration, log):
    task = game_task_id(phone, act)
    home = phone.call("shell", "cmd", "package", "resolve-activity", "--brief",
                      "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME")
    match = re.search(r"^([\w.]+)/([\w.$]+)\s*$", home, re.M)
    if task is None or match is None:
        return False
    package, activity = match.groups()
    launcher = package, package + activity if activity.startswith(".") else activity
    if launcher[0] in (PKG, "android") or not phone.awake():
        return False

    game = PKG, act
    phone.input(act, "keyevent", "KEYCODE_HOME")
    for _ in range(8):
        current = phone.activity()
        if current == launcher:
            break
        if current[0] and current != game:
            raise GameTransitionError(f"返回桌面时前台发生变化 {current}")
        time.sleep(.4)
    else:
        if phone.activity() == game:
            return False
        raise GameTransitionError("未确认已回到系统桌面")

    log("desktop_timer", seconds=duration, activity=act)
    started = time.monotonic()
    while (remaining := duration - (time.monotonic() - started)) > 0:
        time.sleep(min(5, remaining))
        current = phone.activity()
        if current == game and game_task_id(phone, act) == task:
            return False
        if current != launcher:
            raise GameTransitionError(f"桌面等待期间前台发生变化 {current}")
    waited = time.monotonic() - started

    if game_task_id(phone, act) != task:
        raise GameTransitionError("无法确认桌面等待前的原游戏任务")
    if phone.activity() != launcher:
        raise GameTransitionError("恢复原游戏前前台已改变")
    if not phone.awake():
        phone.call("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(.5)
        if phone.activity() != launcher:
            raise GameTransitionError("唤醒手机后未回到桌面，请解锁手机后再复查")
    phone.call("shell", "am", "task", "focus", task)
    if wait_game(phone) != act or game_task_id(phone, act) != task:
        raise GameTransitionError("桌面等待后未确认恢复原游戏任务")
    log("desktop_timer_finished", seconds=duration, waited_seconds=round(waited, 2), activity=act)
    return True


def play_seconds(phone, act, duration, log, timing=None):
    remaining = duration
    deadline = time.monotonic() + max(180, duration * 6)
    previous = time.monotonic()
    restored = 0
    while remaining > 0:
        time.sleep(min(2, remaining))
        current = phone.activity()
        now = time.monotonic()
        if not phone.awake():
            raise RuntimeError("屏幕已休眠，请解锁手机后重新读取任务")
        if current == (PKG, act):
            remaining -= now - previous
        else:
            foreground = current
            if (current[0] == PKG and (is_interstitial(current[1]) or current[1] in (AD_WEB, AD_SCREEN))
                    and time.monotonic() < deadline):
                ad_started = time.monotonic()
                try:
                    if close_interstitial(phone, current, log):
                        log("ad_closed", remaining=round(remaining, 1),
                            handling_seconds=round(time.monotonic() - ad_started, 2))
                        foreground = phone.activity()
                finally:
                    if timing is not None:
                        timing["ad_recovery_seconds"] += time.monotonic() - ad_started
            # The launcher or WebView can briefly appear during an activity transition.
            for _ in range(6):
                if foreground[0] == PKG and foreground[1] not in (CENTER, MAIN):
                    break
                time.sleep(.65)
                foreground = phone.activity()
            if foreground[0] == PKG and foreground[1] in (CENTER, MAIN):
                log("game_returned_early", activity=foreground[1], remaining=round(remaining, 1))
                return
            ad_pending = foreground[0] == PKG and (is_interstitial(foreground[1])
                                                   or foreground[1] in (AD_WEB, AD_SCREEN))
            if (foreground[0] and foreground[0] != PKG and restored < 2
                    and time.monotonic() < deadline and restore_game_task(phone, act)):
                restored += 1
                log("game_task_restored", redirect_package=foreground[0], remaining=round(remaining, 1))
                previous = time.monotonic()
                continue
            if foreground != (PKG, act) and not ad_pending:
                raise GameTransitionError(f"游戏离开前台，本轮计时中止 {foreground}")
            if ad_pending and (foreground == current or time.monotonic() >= deadline):
                raise GameTransitionError(f"未能关闭插屏 {foreground}")
        previous = time.monotonic()


def prepare_center(phone, log):
    pkg, current = phone.activity()
    if pkg == PKG and current in (AD_WEB, AD_SCREEN) and back_from_ad_web(phone, log, current):
        pkg, current = phone.activity()
    if (pkg, current) == (PKG, CENTER):
        return phone.page()
    if pkg != PKG or not (is_game(current) or is_interstitial(current)):
        raise RuntimeError(f"请打开游戏中心首页，当前前台为 {pkg}/{current}")
    # Hist entries list each Android task from its foreground activity down.
    entries = activity_history(phone)
    for owner, activity, task in entries:
        if (owner, activity) != (pkg, current):
            continue
        chain = [(p, a) for p, a, t in entries if t == task]
        game_index = 0 if is_game(current) else 1
        if (len(chain) > game_index + 1 and chain[0] == (pkg, current)
                and chain[game_index][0] == PKG and is_game(chain[game_index][1])
                and chain[game_index + 1] == (PKG, CENTER)):
            log("recover_to_center", activity=current)
            return_to_center(phone, chain[game_index][1])
            return phone.page()
    raise RuntimeError("未识别到同一任务栈中的游戏中心入口，请回到游戏中心首页")


def first_screen(phone, page):
    for _ in range(40):
        y, bottom = page.anchors["recommend"][1::2]
        near_top = bottom > y and page.top <= y < page.bottom
        phone.input(CENTER, "swipe", phone.width//2, int(phone.height*.20),
                    phone.width//2, int(phone.height*.82), 230)
        time.sleep(.5)
        page = phone.page()
        if near_top and page.top <= page.anchors["recommend"][1] < page.bottom:
            return page
    raise RuntimeError("无法定位好游推荐，尚未确认扫描起点")


def refresh_center(phone):
    phone.input(CENTER, "keyevent", "KEYCODE_BACK")
    for _ in range(8):
        time.sleep(.5)
        foreground = phone.activity()
        if foreground == (PKG, MAIN):
            return_to_center(phone, GAME)
            return phone.page()
        if foreground != (PKG, CENTER):
            raise ForegroundChanged(f"刷新游戏中心时前台发生变化 {foreground}")
    return None


def badge_result(phone, key, original, anchor=None, initial_page=None):
    last = None
    for attempt in range(3):
        page = initial_page if attempt == 0 and initial_page is not None else phone.page()
        cards = page.cards[key]
        matches = [c for c in cards if c.name == original.name]
        if matches:
            evidence = "still_marked" if any(c.coins for c in matches) else "badge_removed"
        elif (key == "editor" and page.anchors[key] == anchor and anchor[3] > anchor[1]
              and any(c.layout == original.layout and c.target == original.target
                      and page.point(c, phone.width) for c in cards)):
            evidence = "slot_replaced"
        else:
            evidence = "card_not_located"
        if evidence == last and evidence in ("badge_removed", "slot_replaced"):
            return evidence, page
        last = evidence
        if attempt < 2:
            time.sleep(.6)
    evidence = last if last in ("still_marked", "card_not_located") else "unstable_page"
    return evidence, page


def hot_viewport(phone, page):
    return tuple((c.name, c.area) for c in page.cards["hot"] if page.point(c, phone.width))


def scroll_hot(phone):
    phone.input(CENTER, "swipe", phone.width//2, int(phone.height*.77),
                phone.width//2, int(phone.height*.36), 520)
    time.sleep(.5)


def recheck_pending(phone, pending, log):
    started = time.monotonic()
    page = refresh_center(phone)
    if page is None:
        log("pending_recheck_skipped", reason="游戏中心未重新打开")
        return pending, None
    remaining = set()
    for key in HEADERS:
        names = {name for section, name in pending if section == key}
        if not names:
            continue
        log("pending_section_started", section=key, games=sorted(names))
        still_marked = set()
        if key == "hot":
            last_viewport = None
            stable = 0
            for scrolls in range(41):
                still_marked.update(c.name for c in page.cards[key] if c.coins)
                # A vanished card in the paginated grid needs matching snapshots.
                candidates = [c for c in page.cards[key] if c.name in names and not c.coins]
                if candidates:
                    time.sleep(.6)
                    page = phone.page()
                    clear = {c.name for c in page.cards[key] if not c.coins}
                    names.difference_update(c.name for c in candidates if c.name in clear)
                    still_marked.update(c.name for c in page.cards[key] if c.coins)
                if not names or still_marked:
                    break
                viewport = hot_viewport(phone, page)
                stable = stable + 1 if viewport and viewport == last_viewport else 0
                if stable == 2 or scrolls == 40:
                    break
                last_viewport = viewport
                scroll_hot(phone)
                page = phone.page()
        else:
            # Short sections expose their roster in one page, including off-screen cards.
            for _ in range(12):
                cards = page.cards[key]
                if not cards or any(page.point(c, phone.width) for c in cards):
                    break
                phone.reveal(page, key, cards[0])
                page = phone.page()
            first = {c.name for c in page.cards[key]}
            still_marked.update(c.name for c in page.cards[key] if c.coins)
            visible = any(page.point(c, phone.width) for c in page.cards[key])
            time.sleep(.6)
            page = phone.page()
            second = {c.name for c in page.cards[key]}
            still_marked.update(c.name for c in page.cards[key] if c.coins)
            visible = visible and any(page.point(c, phone.width) for c in page.cards[key])
            if visible and first == second and not still_marked:
                names.clear()
            else:
                names.difference_update((first & second) - still_marked)
        remaining.update((key, name) for name in names | still_marked)
        log("pending_section_checked", section=key, still_marked=sorted(still_marked), unresolved=sorted(names))
    log("pending_recheck_finished", elapsed_seconds=round(time.monotonic()-started, 1))
    return remaining, page


def run(args, log, *, phone=None, diagnostic=None):
    if phone is None:
        phone = Phone(args.serial, log=log if args.execute else None)
    sections = list(HEADERS) if args.section == "all" else [args.section]
    if not args.execute:
        page = phone.page()
        for key in sections:
            print(HEADERS[key], [(c.name, c.seconds, c.coins) for c in page.cards[key] if c.coins])
        return
    total = 0
    pending = set()
    timing_mode = getattr(args, "timing", "foreground")
    started = time.monotonic()
    log("run_started", serial=args.serial, sections=sections, limit=args.limit, timing=timing_mode)
    page = prepare_center(phone, log)
    section_seconds = dict(page.section_seconds)
    next_page = first_screen(phone, page)
    for key in sections:
        section_started = time.monotonic()
        foreground_budget = 0
        desktop_budget = 0
        log("section_started", section=key)
        attempts = {}
        unresolved = {}
        swipes = 0
        last_scroll = None
        still = 0
        scan_pages = 0
        pass_scrolls = 0
        retried_offscreen = False
        refresh_attempted = False
        reopened = False
        while True:
            page = next_page if next_page is not None else phone.page()
            next_page = None
            for seen in page.cards[key]:
                if (seen.name in unresolved and not seen.coins
                        and (reopened or page.point(seen, phone.width))):
                    evidence, updated_page = badge_result(phone, key, seen, initial_page=page)
                    page = updated_page
                    if evidence == "badge_removed":
                        del unresolved[seen.name]
                        log("confirmed", section=key, game=seen.name, evidence="later_badge_removed",
                            badge_removed=True)
            section_seconds.update(page.section_seconds)
            eligible = [c for c in page.cards[key] if c.coins]
            for c in eligible:
                if attempts.get(c.name, 0) >= 2:
                    unresolved[c.name] = "still_marked"
            candidates = [c for c in eligible if attempts.get(c.name, 0) < 2]
            if key == "hot" and not candidates:
                # The grid can append cards when the user reaches the bottom.
                visible = hot_viewport(phone, page)
                still = still + 1 if visible and visible == last_scroll else 0
                last_scroll = visible
                if still < 2:
                    if pass_scrolls >= 40:
                        raise RuntimeError("热门内容分页扫描达到上限，尚未确认列表末尾")
                    scroll_hot(phone)
                    scan_pages += 1
                    pass_scrolls += 1
                    continue
            retry_names = [name for name in unresolved if attempts.get(name, 0) < 2]
            if not refresh_attempted and any(unresolved[name] == "still_marked" for name in retry_names) and all(
                    attempts.get(card.name, 0) > 0 for card in candidates):
                next_page = refresh_center(phone)
                refresh_attempted = True
                reopened = next_page is not None
                log("retry_refresh", section=key, games=retry_names, reopened=reopened)
                if reopened:
                    last_scroll = None
                    still = pass_scrolls = swipes = 0
                    retried_offscreen = key == "hot"
                continue
            if not candidates:
                if key == "hot" and retry_names and not retried_offscreen:
                    log("hot_retry_scan", games=retry_names)
                    next_page = first_screen(phone, page)
                    retried_offscreen = True
                    last_scroll = None
                    still = pass_scrolls = 0
                    continue
                pending.update((key, name) for name in unresolved)
                log("section_checked", section=key, still_marked=[c.name for c in eligible],
                    unresolved=dict(unresolved), scrolls=scan_pages, completion_label=key in page.completed,
                    elapsed_seconds=round(time.monotonic() - section_started, 2),
                    planned_foreground_seconds=foreground_budget, planned_desktop_seconds=desktop_budget)
                next_page = page
                break
            card = min(candidates, key=lambda c: (attempts.get(c.name, 0), page.point(c, phone.width) is None))
            point = page.point(card, phone.width)
            if not point:
                if swipes >= 18:
                    raise RuntimeError(f"滚动后仍无法定位 {card.name}")
                phone.reveal(page, key, card)
                swipes += 1
                continue
            seconds, source = card.seconds, card.duration_source
            if source == "default" and key in section_seconds:
                seconds, source = section_seconds[key], "previous_section"
            if not 1 <= seconds <= 600:
                raise RuntimeError(f"{HEADERS[key]} 的 {card.name} 有奖励，但时长未识别")
            swipes = 0
            use_desktop = timing_mode == "desktop" and not attempts.get(card.name, 0)
            log("open", section=key, game=card.name, coins=card.coins, seconds=seconds,
                duration_source=source, attempt=attempts.get(card.name, 0)+1,
                timing="desktop" if use_desktop else "foreground")
            interrupted = False
            timing = {"ad_recovery_seconds": 0.0}
            stage = "launch_seconds"
            stage_started = time.monotonic()
            try:
                phone.input(CENTER, "tap", *point)
                act = wait_game(phone)
                timing[stage] = time.monotonic() - stage_started
                stage, stage_started = "play_elapsed_seconds", time.monotonic()
                if use_desktop and desktop_wait(phone, act, seconds+1, log):
                    desktop_budget += seconds+1
                else:
                    if use_desktop:
                        log("desktop_timer_fallback", reason="未完成同一游戏任务的桌面等待")
                    duration = seconds + random.randint(1, 2)
                    foreground_budget += duration
                    log("foreground_timer", game=card.name, seconds=duration, activity=act)
                    play_seconds(phone, act, duration, log, timing=timing)
                timing[stage] = time.monotonic() - stage_started
                stage, stage_started = "return_seconds", time.monotonic()
                return_to_center(phone, act)
                timing[stage] = time.monotonic() - stage_started
            except (GameTransitionError, ForegroundChanged) as exc:
                timing[stage] = time.monotonic() - stage_started
                recovery_started = time.monotonic()
                interrupted = True
                log("round_interrupted", section=key, game=card.name, reason=str(exc))
                if diagnostic is not None:
                    diagnostic(phone, "round_interrupted")
                if phone.activity() == (PKG, MAIN):
                    return_to_center(phone, GAME)
                next_page = prepare_center(phone, log)
                log("round_recovered", section=key, game=card.name)
                timing["recovery_seconds"] = time.monotonic() - recovery_started
            verify_started = time.monotonic()
            evidence, next_page = badge_result(phone, key, card, page.anchors[key], initial_page=next_page)
            timing["verify_seconds"] = time.monotonic() - verify_started
            log("round_timing", section=key, game=card.name, **{k: round(v, 1) for k, v in timing.items()})
            confirmed = evidence in ("badge_removed", "slot_replaced")
            if confirmed:
                unresolved.pop(card.name, None)
            else:
                unresolved[card.name] = evidence
            log("confirmed" if confirmed else "unconfirmed", section=key, game=card.name,
                evidence=evidence, badge_removed=confirmed)
            attempts[card.name] = attempts.get(card.name, 0) + 1
            if evidence == "still_marked" and attempts[card.name] == 2 and not interrupted:
                log("gameplay_check_needed", section=key, game=card.name,
                    reason="两轮复查仍有金币标识，可正常游玩一局后返回核对")
            total += 1
            if args.limit and total >= args.limit:
                pending.update((key, name) for name in unresolved)
                if pending:
                    raise RuntimeError(f"本次测试有待核对的奖励 {pending}")
                log("limit_reached", rounds=total)
                return
    if pending:
        log("pending_recheck_started", sections=[key for key in sections if any(k == key for k, _ in pending)])
        pending, next_page = recheck_pending(phone, pending, log)
    if pending:
        raise RuntimeError(f"以下任务尚未确认结算 {sorted(pending)}")
    first_screen(phone, next_page)
    log("run_completed", sections=sections, rounds=total, elapsed_seconds=round(time.monotonic() - started, 2))


def save_diagnostic(phone, logfile, state, index, log):
    path = logfile.with_name(f"{logfile.stem}-{index:03d}-{state}.png")
    try:
        png = phone.screenshot()
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("ADB 没有返回有效 PNG")
        path.write_bytes(png)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log("screenshot_unavailable", state=state, reason=str(exc))
    else:
        log("screenshot", state=state, file=path.name)


def main():
    parser = argparse.ArgumentParser(description="美团游戏中心金币任务")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--serial")
    parser.add_argument("--section", choices=["all", *HEADERS], default="all")
    parser.add_argument("--timing", choices=["foreground", "desktop"], default="foreground",
                        help="计时模式，桌面等待需实机核验金币标识")
    parser.add_argument("--limit", type=int, default=0, help="最多执行几轮，0 表示全部")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit 必须大于等于 0")
    logfile = ROOT / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"
    context = {}
    diagnostics = 0
    errors = (RuntimeError, OSError, subprocess.TimeoutExpired, ET.ParseError, ValueError,
              subprocess.CalledProcessError)

    def log(event, **fields):
        if event in ("section_started", "pending_section_started"):
            context.clear()
            context["section"] = fields["section"]
        if event == "open":
            context["game"] = fields["game"]
        evidence_events = ("screenshot", "screenshot_unavailable")
        if event in ("foreground_timer", "desktop_timer", "desktop_timer_finished", "desktop_timer_fallback",
                     "ad_closed", "ad_redirect_recovered", "ad_web_closed", "game_task_restored",
                     "game_returned_early", "game_update_dismissed", "ad_web_toolbar_tapped", *evidence_events):
            fields.update({k: v for k, v in context.items() if k in ("section", "game")})
        if event == "stopped":
            fields.update(context)
        elif event not in evidence_events:
            context["last_event"] = event
        if event in ("confirmed", "unconfirmed", "section_checked"):
            context.pop("game", None)
        line = json.dumps(dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), event=event, **fields), ensure_ascii=False)
        print(line, flush=True)
        logfile.parent.mkdir(exist_ok=True)
        with logfile.open("a", encoding="utf-8") as out:
            out.write(line + "\n")

    def diagnose(phone, state):
        nonlocal diagnostics
        diagnostics += 1
        save_diagnostic(phone, logfile, state, diagnostics, log)

    try:
        if args.execute:
            import msvcrt
            with (ROOT/".session.lock").open("a+b") as lock:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError("另一个游戏中心任务正在运行") from exc
                phone = Phone(args.serial, log=log)
                try:
                    run(args, log, phone=phone, diagnostic=diagnose)
                except errors as exc:
                    log("stopped", reason=str(exc))
                    diagnose(phone, "stopped")
                    return 2
        else:
            run(args, log)
    except errors as exc:
        log("stopped", reason=str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
