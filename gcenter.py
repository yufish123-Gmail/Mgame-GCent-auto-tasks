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
HEADERS = {"recommend": "好游推荐", "editor": "小编精选", "hot": "热门内容"}
SECTION_SECONDS = {"recommend": 60, "editor": 80, "hot": 120}
ROOT = Path(__file__).resolve().parent


def label(n):
    return (n.get("text") or n.get("content-desc") or "").strip()


def box(n):
    return tuple(map(int, re.findall(r"-?\d+", n.get("bounds", ""))))


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
    def __init__(self, serial):
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

    def tree(self, compressed=False):
        flags = ["--compressed"] if compressed else []
        for attempt in range(3):
            try:
                raw = self.call("exec-out", "uiautomator", "dump", *flags, "/dev/tty")
                start, end = raw.find("<?xml"), raw.rfind("</hierarchy>")
                if 0 <= start < end:
                    return ET.fromstring(raw[start:end+len("</hierarchy>")])
                reason = "界面树不完整"
            except (RuntimeError, ET.ParseError, subprocess.TimeoutExpired) as exc:
                reason = str(exc)
            if attempt < 2:
                time.sleep(.5)
        raise RuntimeError(f"连续三次未能读取完整界面树 ({reason})")

    def page(self):
        for attempt in range(3):
            if self.activity() != (PKG, CENTER):
                raise RuntimeError("请打开美团游戏中心首页")
            tree = self.tree()
            try:
                return Page(tree)
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(.8)

    def awake(self):
        return "mWakefulness=Awake" in self.call("shell", "dumpsys", "power")

    def input(self, activity, *args):
        if self.activity() != (PKG, activity):
            raise RuntimeError("操作前前台页面已改变")
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
        time.sleep(.65)
    raise RuntimeError("未确认小游戏已在前台，未开始计时")


def visual_button(phone, filename, region):
    import cv2
    import numpy as np
    raw = subprocess.check_output(phone.args + ["exec-out", "screencap", "-p"], timeout=12)
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
    for _ in range(6):
        current = phone.activity()
        if current == (PKG, CENTER):
            return
        if current != (PKG, act):
            if close_interstitial(phone, current):
                continue
            break
        point = visual_button(phone, "game_return.png", (350, 0, 432, 85))
        if point:
            phone.input(act, "tap", *point)
            time.sleep(1.5)
            continue
        try:
            tree = phone.tree()
        except (RuntimeError, subprocess.TimeoutExpired):
            tree = None
        buttons = [n for n in (tree.iter() if tree is not None else ()) if label(n) == "返回" and
                   n.get("package") == PKG and n.get("clickable") == "true" and
                   n.get("resource-id") == PKG + ":id/hao"]
        if len(buttons) == 1 and phone.activity() == current:
            x, y, right, bottom = box(buttons[0])
            phone.input(act, "tap", (x+right)//2, (y+bottom)//2)
        elif phone.activity() == current:
            phone.input(act, "keyevent", "KEYCODE_BACK")
        time.sleep(1.5)
    raise RuntimeError("游戏退出后未回到游戏中心")


def close_interstitial(phone, current):
    pkg, activity = current
    if pkg != PKG or not is_interstitial(activity):
        return False
    stack = phone.call("shell", "dumpsys", "activity", "activities")
    task = re.search(re.escape(PKG + "/" + activity) + r" t(\d+)\b", stack)
    for _ in range(6):
        if phone.activity() != current:
            break
        point = None
        if "InterstitialView_" in activity:
            point = visual_button(phone, "interstitial_close.png", (285, 130, 385, 260))
        if point is None:
            try:
                tree = phone.tree(compressed=True)
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
            close = [n for n in tree.iter() if n.get("package") == PKG and
                     (label(n) in ("关闭", "跳过") or n.get("resource-id") == "close_btn")]
            if len(close) == 1 and (close[0].get("resource-id", "").endswith("/tobid_interstitial_skip_text")
                                    or any(label(n) == "广告" for n in tree.iter())):
                x, y, right, bottom = box(close[0])
                if right > x and bottom > y:
                    point = (x+right)//2, (y+bottom)//2
        if point is not None and phone.activity() == current:
            phone.input(activity, "tap", *point)
        time.sleep(1.5)
        pkg, after = phone.activity()
        if pkg and pkg != PKG and task:
            phone.call("shell", "am", "task", "focus", task[1])
            time.sleep(1)
    after = phone.activity()
    return after != current and after[0] == PKG


def play_seconds(phone, act, duration, log):
    remaining = duration
    deadline = time.monotonic() + max(180, duration * 6)
    previous = time.monotonic()
    while remaining > 0:
        time.sleep(min(2, remaining))
        current = phone.activity()
        now = time.monotonic()
        if not phone.awake():
            raise RuntimeError("屏幕已休眠，请解锁手机后重新读取任务")
        if current == (PKG, act):
            remaining -= now - previous
        elif time.monotonic() < deadline and close_interstitial(phone, current):
            log("ad_closed", remaining=round(remaining, 1))
            if phone.activity() != (PKG, act):
                raise RuntimeError("关闭插屏后未回到原游戏")
        else:
            raise RuntimeError(f"游戏离开前台，本轮计时中止 {current}")
        previous = time.monotonic()


def prepare_center(phone, log):
    pkg, current = phone.activity()
    if (pkg, current) == (PKG, CENTER):
        return phone.page()
    if pkg != PKG or not (is_game(current) or is_interstitial(current)):
        raise RuntimeError(f"请打开游戏中心首页，当前前台为 {pkg}/{current}")
    stack = phone.call("shell", "dumpsys", "activity", "activities")
    # Hist entries list each Android task from its foreground activity down.
    entries = re.findall(r"\* Hist\s+#\d+: ActivityRecord\{[^\n}]*?\s([\w.]+)/([\w.$]+) t(\d+)\b", stack)
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
        phone.input(CENTER, "swipe", phone.width//2, int(phone.height*.23),
                    phone.width//2, int(phone.height*.8), 230)
        time.sleep(.8)
        page = phone.page()
        if near_top and page.top <= page.anchors["recommend"][1] < page.bottom:
            return page
    raise RuntimeError("无法定位好游推荐，尚未确认扫描起点")


def badge_result(phone, key, original, anchor=None):
    last = None
    for attempt in range(3):
        page = phone.page()
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
            return evidence
        last = evidence
        if attempt < 2:
            time.sleep(1)
    return last if last in ("still_marked", "card_not_located") else "unstable_page"


def run(args, log):
    phone = Phone(args.serial)
    sections = list(HEADERS) if args.section == "all" else [args.section]
    if not args.execute:
        page = phone.page()
        for key in sections:
            print(HEADERS[key], [(c.name, c.seconds, c.coins) for c in page.cards[key] if c.coins])
        return
    total = 0
    pending = set()
    log("run_started", serial=args.serial, sections=sections, limit=args.limit)
    first_screen(phone, prepare_center(phone, log))
    for key in sections:
        log("section_started", section=key)
        attempts = {}
        unresolved = {}
        swipes = 0
        last_scroll = None
        still = 0
        scan_pages = 0
        while True:
            page = phone.page()
            for seen in page.cards[key]:
                if seen.name in unresolved and not seen.coins and page.point(seen, phone.width):
                    if badge_result(phone, key, seen) == "badge_removed":
                        del unresolved[seen.name]
                        log("confirmed", section=key, game=seen.name, evidence="later_badge_removed",
                            badge_removed=True)
            eligible = [c for c in page.cards[key] if c.coins]
            for c in eligible:
                if attempts.get(c.name, 0) >= 2:
                    unresolved[c.name] = "still_marked"
            candidates = [c for c in eligible if attempts.get(c.name, 0) < 2]
            if not candidates:
                if key == "hot":
                    # The grid can append cards when the user reaches the bottom.
                    visible = tuple((c.name, c.area) for c in page.cards[key] if page.point(c, phone.width))
                    still = still + 1 if visible and visible == last_scroll else 0
                    last_scroll = visible
                    if still < 2:
                        if scan_pages >= 40:
                            raise RuntimeError("热门内容分页扫描达到上限，尚未确认列表末尾")
                        phone.input(CENTER, "swipe", phone.width//2, int(phone.height*.77),
                                    phone.width//2, int(phone.height*.36), 520)
                        scan_pages += 1
                        time.sleep(1)
                        continue
                pending.update((key, name) for name in unresolved)
                log("section_checked", section=key, still_marked=[c.name for c in eligible],
                    unresolved=dict(unresolved), scrolls=scan_pages, completion_label=key in page.completed)
                break
            card = min(candidates, key=lambda c: (attempts.get(c.name, 0), page.point(c, phone.width) is None))
            point = page.point(card, phone.width)
            if not point:
                if swipes >= 18:
                    raise RuntimeError(f"滚动后仍无法定位 {card.name}")
                phone.reveal(page, key, card)
                swipes += 1
                continue
            if not 1 <= card.seconds <= 600:
                raise RuntimeError(f"{HEADERS[key]} 的 {card.name} 有奖励，但时长未识别")
            swipes = 0
            log("open", section=key, game=card.name, coins=card.coins, seconds=card.seconds,
                duration_source=card.duration_source, attempt=attempts.get(card.name, 0)+1)
            phone.input(CENTER, "tap", *point)
            act = wait_game(phone)
            duration = card.seconds + random.randint(1, 2)
            log("foreground_timer", game=card.name, seconds=duration, activity=act)
            play_seconds(phone, act, duration, log)
            return_to_center(phone, act)
            evidence = badge_result(phone, key, card, page.anchors[key])
            confirmed = evidence in ("badge_removed", "slot_replaced")
            if confirmed:
                unresolved.pop(card.name, None)
            else:
                unresolved[card.name] = evidence
            log("confirmed" if confirmed else "unconfirmed", section=key, game=card.name,
                evidence=evidence, badge_removed=confirmed)
            attempts[card.name] = attempts.get(card.name, 0) + 1
            total += 1
            if args.limit and total >= args.limit:
                pending.update((key, name) for name in unresolved)
                if pending:
                    raise RuntimeError(f"本次测试有待核对的奖励 {pending}")
                log("limit_reached", rounds=total)
                return
    if pending:
        raise RuntimeError(f"以下任务尚未确认结算 {sorted(pending)}")
    first_screen(phone, phone.page())
    log("run_completed", sections=sections, rounds=total)


def main():
    parser = argparse.ArgumentParser(description="美团游戏中心金币任务")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--serial")
    parser.add_argument("--section", choices=["all", *HEADERS], default="all")
    parser.add_argument("--limit", type=int, default=0, help="最多执行几轮，0 表示全部")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit 必须大于等于 0")
    logfile = ROOT / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"
    context = {}
    def log(event, **fields):
        if event == "section_started":
            context.clear()
            context["section"] = fields["section"]
        if event == "open":
            context["game"] = fields["game"]
        if event in ("foreground_timer", "ad_closed"):
            fields.update({k: v for k, v in context.items() if k in ("section", "game")})
        if event == "stopped":
            fields.update(context)
        else:
            context["last_event"] = event
        if event in ("confirmed", "unconfirmed", "section_checked"):
            context.pop("game", None)
        line = json.dumps(dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), event=event, **fields), ensure_ascii=False)
        print(line, flush=True)
        logfile.parent.mkdir(exist_ok=True)
        with logfile.open("a", encoding="utf-8") as out:
            out.write(line + "\n")
    try:
        if args.execute:
            import msvcrt
            with (ROOT/".session.lock").open("a+b") as lock:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError("另一个游戏中心任务正在运行") from exc
                run(args, log)
        else:
            run(args, log)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ET.ParseError, ValueError) as exc:
        log("stopped", reason=str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
