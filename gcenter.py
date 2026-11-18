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
                if coins and not 1 <= seconds <= 600:
                    raise RuntimeError(f"{heading} 有奖励，但时长未识别")
                target = box(go if go is not None else names[0])
                layout = "compact" if key == "editor" and go is None else key
                self.cards[key].append(Card(label(names[0]), seconds, coins, target, box(node), layout))

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

    def tree(self):
        for _ in range(3):
            raw = self.call("exec-out", "uiautomator", "dump", "/dev/tty")
            start, end = raw.find("<?xml"), raw.rfind("</hierarchy>")
            if 0 <= start < end:
                return ET.fromstring(raw[start:end+12])
            time.sleep(.5)
        raise RuntimeError("连续三次未能读取完整界面树")

    def page(self):
        if self.activity() != (PKG, CENTER):
            raise RuntimeError("请打开美团游戏中心首页")
        return Page(self.tree())

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


def wait_game(phone):
    for _ in range(16):
        pkg, act = phone.activity()
        if pkg == PKG and re.fullmatch(re.escape(GAME) + r"\d*", act):
            return act
        time.sleep(.65)
    raise RuntimeError("未确认小游戏已在前台，未开始计时")


def visual_return(phone):
    import cv2
    import numpy as np
    raw = subprocess.check_output(phone.args + ["exec-out", "screencap", "-p"], timeout=12)
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.shape[0] <= image.shape[1]:
        return None
    height, width = image.shape
    small = cv2.resize(image, (432, 960))
    template = cv2.imdecode(np.frombuffer((ROOT/"templates/game_return.png").read_bytes(), np.uint8), 0)
    scores = cv2.matchTemplate(small[:85, 350:], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    if score >= .92:
        return round((350+x+template.shape[1]/2)*width/432), round((y+template.shape[0]/2)*height/960)
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
        buttons = [n for n in phone.tree().iter() if label(n) == "返回" and
                   n.get("package") == PKG and n.get("clickable") == "true" and
                   n.get("resource-id") == PKG + ":id/hao"]
        if len(buttons) == 1 and phone.activity() == current:
            x, y, right, bottom = box(buttons[0])
            phone.input(act, "tap", (x+right)//2, (y+bottom)//2)
        elif phone.activity() == current:
            point = visual_return(phone)
            if point:
                phone.input(act, "tap", *point)
        time.sleep(1.5)
    raise RuntimeError("游戏退出后未回到游戏中心")


def close_interstitial(phone, current):
    pkg, activity = current
    if pkg != PKG or not re.fullmatch(r"com\.windmill\.sdk\.widget\.InterstitialView_\d+", activity):
        return False
    tree = phone.tree()
    close = [n for n in tree.iter() if label(n) == "关闭" and n.get("package") == PKG]
    if len(close) != 1 or not any(label(n) == "广告" for n in tree.iter()):
        return False
    x, y, right, bottom = box(close[0])
    if right <= x or bottom <= y:
        return False
    phone.input(activity, "tap", (x+right)//2, (y+bottom)//2)
    time.sleep(1)
    return True


def play_seconds(phone, act, duration, log):
    remaining = duration
    ads = 0
    previous = time.monotonic()
    while remaining > 0:
        time.sleep(min(2, remaining))
        current = phone.activity()
        now = time.monotonic()
        if not phone.awake():
            raise RuntimeError("屏幕已休眠，请解锁手机后重新读取任务")
        if current == (PKG, act):
            remaining -= now - previous
        elif ads < 5 and close_interstitial(phone, current):
            ads += 1
            log("ad_closed", remaining=round(remaining, 1))
            if phone.activity() != (PKG, act):
                raise RuntimeError("关闭插屏后未回到原游戏")
        else:
            raise RuntimeError(f"游戏离开前台，本轮计时中止 {current}")
        previous = time.monotonic()


def run(args, log):
    phone = Phone(args.serial)
    sections = list(HEADERS) if args.section == "all" else [args.section]
    page = phone.page()
    if not args.execute:
        for key in sections:
            print(HEADERS[key], [(c.name, c.seconds, c.coins) for c in page.cards[key] if c.coins])
        return
    total = 0
    pending = set()
    for key in sections:
        attempts = {}
        swipes = 0
        last_scroll = None
        still = 0
        scan_pages = 0
        while True:
            page = phone.page()
            eligible = [c for c in page.cards[key] if c.coins]
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
                if eligible:
                    pending.update((key, c.name) for c in eligible)
                log("section_checked", section=key, still_marked=[c.name for c in eligible])
                break
            card = min(candidates, key=lambda c: (attempts.get(c.name, 0), page.point(c, phone.width) is None))
            point = page.point(card, phone.width)
            if not point:
                if swipes >= 18:
                    raise RuntimeError(f"滚动后仍无法定位 {card.name}")
                phone.reveal(page, key, card)
                swipes += 1
                continue
            swipes = 0
            log("open", section=key, game=card.name, coins=card.coins, seconds=card.seconds)
            phone.input(CENTER, "tap", *point)
            act = wait_game(phone)
            duration = card.seconds + random.randint(1, 2)
            log("foreground_timer", game=card.name, seconds=duration, activity=act)
            play_seconds(phone, act, duration, log)
            return_to_center(phone, act)
            for check in range(3):
                marked_names = {c.name for c in phone.page().cards[key] if c.coins}
                if card.name not in marked_names:
                    break
                if check < 2:
                    time.sleep(1)
            confirmed = card.name not in marked_names
            log("confirmed" if confirmed else "unconfirmed", section=key, game=card.name,
                badge_removed=confirmed)
            attempts[card.name] = attempts.get(card.name, 0) + 1
            total += 1
            if args.limit and total >= args.limit:
                pending.update((key, name) for name in attempts if name in marked_names)
                if pending:
                    raise RuntimeError(f"本次测试有待核对的奖励 {pending}")
                return
    if pending:
        raise RuntimeError(f"以下任务尚未确认结算 {sorted(pending)}")


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
    def log(event, **fields):
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
