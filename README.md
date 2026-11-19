# Mgame-GCenter 游戏中心金币任务

在 Windows 上通过 ADB 操作已授权的 Android 手机。手机解锁后打开美团游戏中心的「玩游戏」首页。程序按游戏卡上的金币标识选择任务。

| 区域 | 实测入口 | 等待时长 |
|---|---|---|
| 好游推荐 | 横向卡片，同一张卡同时显示「试玩60秒+10」和「去玩」 | 读取卡片秒数 |
| 小编精选 | 大卡的「去玩」，或带金币标识的小图标下方的游戏名 | 读取区域说明，本机为 80 秒 |
| 热门内容 | 双列卡片中，标题旁有「+」及奖励数的游戏 | 读取区域说明，本机为 120 秒 |

### 本机运行

在本目录打开 PowerShell。默认模式列出当前页面已加载的奖励卡，包括需要滚动才可点击的卡片。

```powershell
.\run.ps1
.\run.ps1 -Section recommend -Execute -Limit 1
.\run.ps1 -Execute
```

`-Section` 支持 `all`、`recommend`、`editor`、`hot`。`-Limit` 限制本次尝试的轮数，默认 0 表示执行所选区域。多台设备可用 `-Serial` 指定序列号。脚本优先使用项目的虚拟环境，也支持同级 FCash 项目已安装的 Python。

独立部署需要 Python 3.10 或更高版本、OpenCV、NumPy 及 Android Platform Tools。将 adb 加入 PATH 或放在 Windows 用户目录的标准 Android SDK 路径。可用以下命令准备独立环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 计时与结算

执行时先定位好游推荐所在的首屏，再扫描所选区域。小编精选会把新游戏放到旧卡片的同一位置，程序只选择该位置上最前面的卡片。热门内容即使显示「任务已完成」，仍逐张读取金币标识。时长依次取自卡片、区域说明、三个区域的默认值（60、80、120 秒）。日志的 `duration_source` 记录实际来源。

小游戏进入前台后，程序按要求时长加 1 至 2 秒计时。遇到已识别的插屏会查找「关闭」或「跳过」，回到原游戏后继续累计前台时间。计时结束后返回游戏中心。

每轮最多读取三次页面。连续两次看到原游戏的标识消失，或看到小编精选在相同位置换上新卡，才记录 `confirmed`。卡片移出当前页面会记录为待复查。同一区域同名游戏每次执行最多尝试两轮。热门内容滚动到连续两次滑动后可见卡片及位置均未变化，才结束扫描；翻页后仍保留此前未确认的游戏。

### 中断后继续与查看结果

重新运行同一条执行命令即可。手机停留在小游戏或已识别的插屏时，程序先确认该游戏来自同一任务栈中的游戏中心，再返回首页读取当前标识。重新打开仍带标识的游戏会重新计时。若手机停在其他应用，请先手动返回游戏中心首页。

每次执行的记录位于 `runs/*.jsonl`。查看最后一个日志的末尾可确认本次执行的范围和结果。

```powershell
$latest = Get-ChildItem .\runs\*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -LiteralPath $latest.FullName -Encoding UTF8 -Tail 8
```

| 日志事件 | 含义 |
|---|---|
| `section_checked` | 区域复查结果。`still_marked` 是当前页面仍有标识的游戏，`unresolved` 包含此前翻页后仍待核对的游戏及原因。 |
| `run_completed` | 所选区域全部扫完、待复查项为空，手机已回到游戏中心首屏。 |
| `limit_reached` | 达到 `-Limit` 指定的尝试轮数。 |
| `stopped` | 执行中止，退出码为 2。记录包含原因，以及中止时已知的区域、游戏和最近事件。 |

可用 `python -m unittest -v` 在电脑上运行离线回归测试。

### 2026-11-19 实机记录

10:39（北京时间）完成三个区域的最后一次复查。好游推荐最初的 4 张奖励卡均已清除金币标识，小编精选的当前卡片均无金币标识，热门内容最初的 10 张奖励卡均已清除标识并扫到列表末尾。三个区域的 `section_checked.still_marked` 均为空，手机随后回到游戏中心首屏。本次按页面金币标识验收。复查日志保存在本地 `runs/20261119-103710-31560.jsonl`。

本次遇到插屏导致界面树读取失败、小编精选保留被替换的卡片节点，以及热门内容显示「任务已完成」时仍有金币卡片。对应优化已推送到 GitHub 的 `main` 分支，提交为 [`3016d92`](https://github.com/yufish123-Gmail/Mgame-GCent-auto-tasks/commit/3016d927e2aeb6bd7b6d738beb72b34cd2aaec23)。该版本 9 项离线回归测试全部通过，实机复测待下次连接手机后进行。
