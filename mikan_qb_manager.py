# -*- coding: utf-8 -*-
"""
蜜柑计划 RSS × qBittorrent 总控程序

功能：
  功能一：调用 mikan_rss_collector.py 采集季度番组 → 勾选订阅 →
          清洗名称（去掉 Mikan Project 前缀、截前 10 字）→
          创建番组文件夹 → 向 qBittorrent WebUI 逐条添加 RSS 订阅
  功能二：读取 qB RSS 未读条目 → 按番组分组展示 → 勾选 →
          下载到对应番组文件夹 → 标记已读 + 写入本地 processed.json

启动：py -3 mikan_qb_manager.py [--debug] [--check-qb] [--dry-run] [--self-test]

仅使用 Python 标准库（3.8+）。
"""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from qb_client import (
    QBClient,
    QBError,
    collect_articles,
    flatten_rss_tree,
    is_feed_node,
)

# ----------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
PROCESSED_PATH = SCRIPT_DIR / "processed.json"
COLLECTOR_PATH = SCRIPT_DIR / "mikan_rss_collector.py"
DEFAULT_OUTDIR = SCRIPT_DIR / "output"

SEASON_ALIASES = {
    "春": "春", "spring": "春", "1": "春",
    "夏": "夏", "summer": "夏", "2": "夏",
    "秋": "秋", "fall": "秋", "autumn": "秋", "3": "秋",
    "冬": "冬", "winter": "冬", "4": "冬",
}

# 订阅源/文件夹名清洗：开头前缀（含多种破折号变体）
_MIKAN_PREFIX_RE = re.compile(r"^Mikan\s+Project\s*[-–—]\s*", re.IGNORECASE)
_WIN_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_WINDOWS_RESERVED.update("COM%d" % i for i in range(1, 10))
_WINDOWS_RESERVED.update("LPT%d" % i for i in range(1, 10))

DEFAULT_CONFIG = {
    "qb": {"base_url": "http://127.0.0.1:8080", "username": "admin", "password": ""},
    "anime_root": "",
    "bangumi_folder_map": {},
    "processed_articles": [],
    "last_collect": None,
}


# ----------------------------------------------------------------------
# 日志小工具
# ----------------------------------------------------------------------
class UserAbort(Exception):
    """输入流关闭（EOF）导致的用户中断，主循环应退出而非回到菜单。"""


def info(msg):
    print(f"[信息] {msg}")


def warn(msg):
    print(f"[警告] {msg}")


def err(msg):
    print(f"[错误] {msg}")


# ----------------------------------------------------------------------
# 配置与 processed.json
# ----------------------------------------------------------------------
def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in DEFAULT_CONFIG:
                    if key in data and data[key] is not None:
                        cfg[key] = data[key]
                if not isinstance(cfg.get("qb"), dict):
                    cfg["qb"] = copy.deepcopy(DEFAULT_CONFIG["qb"])
                if not isinstance(cfg.get("bangumi_folder_map"), dict):
                    cfg["bangumi_folder_map"] = {}
        except (OSError, json.JSONDecodeError) as e:
            warn(f"读取 config.json 失败（{e}），将使用默认配置。")
    return cfg


def save_config(config):
    data = copy.deepcopy(config)
    # 会话级字段绝不落盘：_password_in_memory/_session_password 只存在于内存。
    # （旧版曾把 _session_password 误写入磁盘，这里写盘时一律清除残留。）
    data.pop("_password_in_memory", None)
    data.pop("_session_password", None)
    # 密码若标记为「仅本会话」，写盘时清空
    if config.get("_password_in_memory"):
        data.setdefault("qb", {})["password"] = ""
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        warn(f"写入 config.json 失败：{e}")


def load_processed(config):
    """已处理条目集合，元素形如 'feedpath|articleid'。"""
    items = set()
    for x in config.get("processed_articles") or []:
        items.add(str(x))
    if PROCESSED_PATH.exists():
        try:
            data = json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                items.update(str(x) for x in data)
        except (OSError, json.JSONDecodeError) as e:
            warn(f"读取 processed.json 失败：{e}")
    return items


def save_processed(processed, config):
    try:
        PROCESSED_PATH.write_text(
            json.dumps(sorted(processed), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        warn(f"写入 processed.json 失败：{e}")
    config["processed_articles"] = sorted(processed)


def _input(prompt):
    """input() 包装：EOF 转为 UserAbort，便于主循环优雅退出。"""
    try:
        return input(prompt)
    except EOFError:
        print()
        raise UserAbort("输入流已关闭")
    except KeyboardInterrupt:
        print()
        raise


def ensure_qb_settings(config, force=False):
    """交互确认 qB 连接设置。force=True（首次运行）时即使有默认值也询问。"""
    dirty = False
    qb = config.setdefault("qb", {})
    if not isinstance(qb, dict):
        config["qb"] = copy.deepcopy(DEFAULT_CONFIG["qb"])
        qb = config["qb"]
        dirty = True
    base = str(qb.get("base_url") or "").strip()
    if force or not base:
        ans = _input(
            "qB WebUI 地址 [%s]：" % (base or "http://127.0.0.1:8080")
        ).strip()
        qb["base_url"] = ans or base or "http://127.0.0.1:8080"
        dirty = True
    user = str(qb.get("username") or "")
    if force or not user:
        ans = _input("qB WebUI 用户名 [%s]：" % (user or "admin")).strip()
        qb["username"] = ans or user or "admin"
        dirty = True
    if force or "password" not in qb or not str(qb.get("password") or ""):
        pwd = _input("qB WebUI 密码（回车跳过）：")
        if pwd:
            ans = _input("是否将密码保存到 config.json？(Y/n)：").strip().lower()
            if ans in ("", "y", "yes"):
                qb["password"] = pwd
                config.pop("_password_in_memory", None)
            else:
                # 仅本次会话使用：内存中保留，save_config 落盘时会清空
                qb["password"] = pwd
                config["_password_in_memory"] = True
        else:
            qb["password"] = ""
        dirty = True
    return dirty


def ensure_anime_root(config, force=False):
    root = str(config.get("anime_root") or "").strip()
    if root and not force:
        return root
    ans = _input("请输入动画根目录（如 F:\\Anime）：%s" % ("[当前 %s] " % root if root else "")).strip()
    root = ans or root
    if not root:
        return ""
    config["anime_root"] = root
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        warn(f"创建动画根目录失败：{e}")
    save_config(config)
    info(f"已保存动画根目录：{root}")
    return root


# ----------------------------------------------------------------------
# 订阅源 / 文件夹名清洗（5.2 / 十三节）
# ----------------------------------------------------------------------
def sanitize_feed_name(raw_title, bangumi_id=None):
    """清洗订阅源 path 与番组文件夹名。

    顺序：去 Mikan Project 前缀 → 去首尾空白 → 截前 10 个 Unicode 字符 →
    去尾部 空格/./-/_ → Windows 非法字符替换为 _ → 保留名加前缀 →
    去尾部 空格/. → 空值兜底 bangumi_{id}
    """
    s = "" if raw_title is None else str(raw_title)
    s = _MIKAN_PREFIX_RE.sub("", s)
    s = s.strip()
    s = s[:10]
    s = s.rstrip(" .-_")
    s = _WIN_ILLEGAL_RE.sub("_", s)
    if s.upper() in _WINDOWS_RESERVED:
        s = "_" + s
    s = s.rstrip(" .")
    if not s:
        if bangumi_id not in (None, ""):
            s = "bangumi_%s" % bangumi_id
        else:
            s = "bangumi_unknown"
    return s


def _dedup_name(base, used):
    """同季内清洗名去重：后来的加 _2 / _3。"""
    if base not in used:
        used.add(base)
        return base
    i = 2
    while "%s_%d" % (base, i) in used:
        i += 1
    name = "%s_%d" % (base, i)
    used.add(name)
    warn("清洗后名称重复：%r 已被占用，本次改用 %r" % (base, name))
    return name


# ----------------------------------------------------------------------
# 多选输入（编号 + 逗号 + 区间）
# ----------------------------------------------------------------------
def parse_multi_select(text, max_n, allow_all=True):
    """解析多选输入。

    返回 (indices, err_msg)：
      indices 为 1 起的已排序去重列表；空输入/"none" → ([], None)；
      非法 → (None, 错误信息)。
    """
    t = (text or "").strip()
    if t == "":
        return [], None
    low = t.lower()
    if low == "none":
        return [], None
    if low == "all":
        if allow_all:
            return list(range(1, max_n + 1)), None
        return None, "此处不支持 all"
    result = set()
    for part in t.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            return None, "无法识别的选项：%r" % part
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        if b < a:
            warn("区间 %r 顺序颠倒，已按 %d-%d 处理" % (part, b, a))
            a, b = b, a
        if a < 1 or b > max_n:
            warn("选项 %r 超出范围 1-%d，越界部分已忽略" % (part, max_n))
            a2, b2 = max(a, 1), min(b, max_n)
            if a2 > b2:
                continue
            a, b = a2, b2
        result.update(range(a, b + 1))
    return sorted(result), None


def ask_multi_select(prompt, max_n, allow_all=True):
    """循环询问直到解析成功。返回 1 起的索引列表（空=取消）。"""
    if max_n <= 0:
        return []
    while True:
        try:
            raw = input(prompt)
        except EOFError:
            print()
            return []
        except KeyboardInterrupt:
            print()
            return []
        idx, e = parse_multi_select(raw, max_n, allow_all=allow_all)
        if e is not None:
            warn("%s，请重新输入" % e)
            continue
        return idx


def try_normalize_season(raw):
    key = (raw or "").strip().lower()
    if key in SEASON_ALIASES:
        return SEASON_ALIASES[key]
    for alias, cn in SEASON_ALIASES.items():
        if key.endswith(alias):
            return cn
    return None


def ask_year_season(config):
    last = config.get("last_collect") or {}
    default_year = last.get("year") or time.localtime().tm_year
    raw = _input(
        "请输入年份 [默认 %d]（可一行输入「2025 秋」）：" % default_year
    ).strip()
    year, season = default_year, None
    if raw:
        tokens = raw.split()
        if len(tokens) == 1 and tokens[0].isdigit():
            year = int(tokens[0])
        else:
            for tok in tokens:
                m = re.fullmatch(r"(\d{4})(.+)", tok)
                if m:
                    year = int(m.group(1))
                    season = try_normalize_season(m.group(2))
                    if season is None:
                        warn("无法识别的季度：%r" % tok)
                    continue
                if tok.isdigit() and len(tok) == 4:
                    year = int(tok)
                    continue
                cn = try_normalize_season(tok)
                if cn:
                    season = cn
                else:
                    warn("无法识别：%r，已忽略" % tok)
    while season is None:
        raw_s = _input("请选择季度（春/夏/秋/冬 或 1-4）：").strip()
        season = try_normalize_season(raw_s)
        if season is None:
            warn("无法识别的季度，请输入 春/夏/秋/冬 或 1-4。")
    return year, season


def ask_anime_root(config):
    """确认/输入动画根目录，保存到配置。"""
    root = str(config.get("anime_root") or "").strip()
    if root:
        ans = _input("动画根目录 [%s]，回车确认，或输入新路径：" % root).strip()
        if not ans:
            return root
        root = ans
    else:
        root = _input("请输入动画根目录（如 F:\\Anime）：").strip()
        if not root:
            return ""
    config["anime_root"] = root
    save_config(config)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        warn("创建动画根目录失败：%s" % e)
    info("动画根目录：%s" % root)
    return root


# ----------------------------------------------------------------------
# 调用采集脚本
# ----------------------------------------------------------------------
def _find_python():
    """依次尝试 py -3 / python / sys.executable，返回 (cmd, version_str)。"""
    candidates = [["py", "-3"], ["python"], [sys.executable]]
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd + ["--version"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode == 0 and "Python 3" in out:
            return cmd, out
    return None, None


def run_collector(year, season, outdir):
    """调用 mikan_rss_collector.py，成功返回 JSON 路径，失败返回 None。"""
    if not COLLECTOR_PATH.exists():
        err("未找到采集脚本：%s" % COLLECTOR_PATH)
        return None
    python_cmd, version = _find_python()
    if not python_cmd:
        err("未找到可用的 Python 3 启动器（依次尝试过 py -3 / python / sys.executable）。")
        warn("本机 python 指向 Windows 商店占位程序，请确认已安装 Python 3.8+ 并可用 py -3 调用。")
        return None
    info("使用 Python：%s（%s）" % (" ".join(python_cmd), version))
    info("正在调用采集器，请稍候（首次约 30-60 秒）...")
    cmd = [
        *python_cmd, str(COLLECTOR_PATH),
        "-y", str(year), "-s", season, "-o", str(outdir),
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=600, cwd=str(SCRIPT_DIR),
        )
    except subprocess.TimeoutExpired:
        err("采集器执行超过 600 秒，已终止。")
        warn("排查建议：网络慢时可先手动运行 py -3 mikan_rss_collector.py -y %d -s %s --limit 3 测试。"
             % (year, season))
        return None
    except FileNotFoundError:
        err("启动采集器失败：找不到 %s" % " ".join(python_cmd))
        return None
    if r.returncode != 0:
        err("采集器退出码 %d。输出尾部：" % r.returncode)
        for line in (r.stdout or "").strip().splitlines()[-8:]:
            print("    " + line)
        for line in (r.stderr or "").strip().splitlines()[-8:]:
            print("    " + line)
        warn("排查建议：检查 -y/-s 参数；确认网络可访问 mikanani.kas.pub；季度太新可能尚无番组。")
        return None
    tail = (r.stdout or "").strip().splitlines()
    if tail:
        info("采集器输出摘要：" + tail[-1].strip())
    json_path = Path(outdir) / ("%d%s_rss.json" % (year, season))
    if not json_path.exists():
        err("采集完成但未找到输出文件：%s" % json_path)
        return None
    return json_path


def load_bangumi_json(path):
    """解析采集器 JSON，兼容「顶层对象含 bangumi 键」与「顶层数组」两种结构。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        items = data.get("bangumi") or data.get("items") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("JSON 结构无法识别（既不是对象也不是数组）")
    result = []
    for it in items:
        if not isinstance(it, dict):
            continue
        bid = it.get("bangumi_id", it.get("bangumiId", it.get("id")))
        title = str(it.get("title") or it.get("name") or "")
        rss_all = str(it.get("rss_all") or it.get("rssAll") or it.get("rss") or "")
        if not rss_all and bid not in (None, ""):
            rss_all = "https://mikanani.kas.pub/RSS/Bangumi?bangumiId=%s" % bid
        subgroups = it.get("subgroups") or it.get("subgroup") or []
        norm_sg = []
        if isinstance(subgroups, list):
            for sg in subgroups:
                if not isinstance(sg, dict):
                    continue
                sgid = sg.get("subgroup_id", sg.get("subgroupId", sg.get("id")))
                norm_sg.append({
                    "subgroup_id": sgid,
                    "name": str(sg.get("name") or sg.get("title") or
                                 ("字幕组%s" % sgid if sgid is not None else "字幕组")),
                    "rss": str(sg.get("rss") or sg.get("rss_url") or
                               (("https://mikanani.kas.pub/RSS/Bangumi?bangumiId=%s&subgroupid=%s"
                                 % (bid, sgid)) if bid not in (None, "") and sgid is not None else "")),
                })
        result.append({
            "bangumi_id": bid,
            "title": title,
            "rss_all": rss_all,
            "subgroups": norm_sg,
        })
    return result


def fake_bangumi_data():
    """--dry-run 且无 JSON 时的示例数据（含前缀/保留名/超长标题用例）。"""
    base = "https://example.invalid/RSS/Bangumi"
    return [
        {
            "bangumi_id": 9001,
            "title": "示例番组甲～超长标题测试十个字截断行为～",
            "rss_all": "%s?bangumiId=9001" % base,
            "subgroups": [
                {"subgroup_id": 1, "name": "字幕组X",
                 "rss": "%s?bangumiId=9001&subgroupid=1" % base},
                {"subgroup_id": 2, "name": "字幕组Y",
                 "rss": "%s?bangumiId=9001&subgroupid=2" % base},
                {"subgroup_id": 3, "name": "字幕组Z",
                 "rss": "%s?bangumiId=9001&subgroupid=3" % base},
            ],
        },
        {
            "bangumi_id": 9002,
            "title": "Mikan Project - 示例番组乙的标题测试",
            "rss_all": "%s?bangumiId=9002" % base,
            "subgroups": [
                {"subgroup_id": 4, "name": "H-Enc",
                 "rss": "%s?bangumiId=9002&subgroupid=4" % base},
            ],
        },
        {
            "bangumi_id": 9003,
            "title": "CON",
            "rss_all": "%s?bangumiId=9003" % base,
            "subgroups": [
                {"subgroup_id": 5, "name": "ANi",
                 "rss": "%s?bangumiId=9003&subgroupid=5" % base},
            ],
        },
    ]


# ----------------------------------------------------------------------
# RSS 树辅助
# ----------------------------------------------------------------------
def scan_rss_tree(tree):
    """返回 (feed_paths, folder_paths)，均为完整反斜杠路径集合。"""
    feeds, folders = set(), set()

    def walk(node, prefix=""):
        for key, val in (node or {}).items():
            full = "%s\\%s" % (prefix, key) if prefix else str(key)
            if is_feed_node(val):
                feeds.add(str(val.get("path") or full))
            elif isinstance(val, dict):
                folders.add(full)
                walk(val, full)

    walk(tree or {})
    return feeds, folders


def format_date(d):
    if d is None:
        return ""
    if isinstance(d, bool):
        return str(d)
    if isinstance(d, (int, float)):
        try:
            return time.strftime("%Y-%m-%d", time.localtime(d))
        except (OverflowError, OSError, ValueError):
            return str(d)
    s = str(d).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %z",
                "%d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.strftime("%Y-%m-%d", time.strptime(s, fmt))
        except ValueError:
            continue
    return s[:16]


def _sg_path_segment(name, idx):
    """字幕组名 → RSS path 中的一段（去掉层级分隔符）。"""
    s = re.sub(r'[\\/]', "_", (name or "").strip())
    return s or ("字幕组%d" % idx if idx else "字幕组")


# ----------------------------------------------------------------------
# dry-run 客户端
# ----------------------------------------------------------------------
class DryRunQBClient(QBClient):
    """--dry-run：不发送任何 qB 写请求，只打印计划动作。"""

    def __init__(self):
        super().__init__(debug=False)
        self.base_url = "(dry-run 未连接)"

    def login(self, base_url, username, password):
        info("dry-run 模式：跳过 qB 登录（不发起网络请求）")
        return True

    def get_app_version(self):
        return "dry-run"

    def get_rss_items(self, with_data=True):
        info("dry-run 模式：跳过读取 RSS 列表，按「现有订阅为空」处理")
        return {}

    def add_folder(self, path):
        info("dry-run：将在 qB 创建 RSS 文件夹 → %s" % path)
        return True

    def add_feed(self, url, path):
        info("dry-run：将添加订阅 path=%s  url=%s" % (path, url))

    def remove_item(self, item_path):
        info("dry-run：将删除订阅 → %s" % item_path)

    def mark_as_read(self, item_path, article_id=None):
        info("dry-run：将标记已读 → %s%s"
             % (item_path, ("（条目 %s）" % article_id) if article_id else ""))

    def create_category(self, name):
        info("dry-run：将确保分类存在 → %s" % name)
        return True

    def add_torrent(self, urls, savepath, category=None, tags=None):
        info("dry-run：将添加下载 savepath=%s category=%s tags=%s url=%s"
             % (savepath, category, tags, urls[:80]))


# ----------------------------------------------------------------------
# 订阅显示名校验（可选）
# ----------------------------------------------------------------------
def verify_display_names(client, added_paths, dry_run):
    if dry_run or not added_paths:
        return
    try:
        tree = client.get_rss_items(with_data=True)
    except QBError as e:
        warn("验证订阅显示名失败：%s" % e)
        return
    feeds, _folders = scan_rss_tree(tree)
    for p in added_paths:
        if p not in feeds:
            warn("未能在 qB 的 RSS 列表中找到刚添加的订阅：%s" % p)
        elif "Mikan Project" in p:
            warn("订阅名仍带有 Mikan Project 前缀：%s" % p)


def peek_rss_doc_titles(added_items, dry_run):
    """读取 RSS 文档 <title>，若带 Mikan Project 前缀则提示（含 0.4s 礼貌间隔）。"""
    if dry_run or not added_items:
        return
    import urllib.request

    warned = False
    for _path, url in added_items:
        time.sleep(0.4)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "mikan-qb-manager/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                head = resp.read(4096).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - 校验失败不影响主流程
            warn("读取 RSS 文档以验证显示名失败：%s" % e)
            continue
        m = re.search(r"<title[^>]*>([^<]+)</title>", head, re.IGNORECASE)
        if not m:
            continue
        doc_title = m.group(1).strip()
        if "Mikan Project" in doc_title and not warned:
            warned = True
            warn("RSS 文档标题仍为「%s」。" % doc_title)
            print("       qB RSS 树的显示名由 addFeed 的 path 决定（本程序已设为清洗后的名称）；")
            print("       若 qB 界面仍显示带前缀的名称，属 qB 以文档标题显示的情形，请在 qB 中手动改名或升级 qB。")


# ----------------------------------------------------------------------
# 功能一：采集并导入 RSS
# ----------------------------------------------------------------------
def feature_collect_import(client, config, stats, dry_run):
    print("\n=== 功能一：采集并导入 RSS ===")
    year, season = ask_year_season(config)
    outdir = DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / ("%d%s_rss.json" % (year, season))

    bangumi_list = None
    if json_path.exists():
        age_h = (time.time() - json_path.stat().st_mtime) / 3600.0
        if age_h <= 48:
            ans = _input(
                "已存在较新的采集结果 %s（%.1f 小时前生成）。是否直接使用？(Y/n)："
                % (json_path, age_h)
            ).strip().lower()
            if ans in ("", "y", "yes"):
                try:
                    bangumi_list = load_bangumi_json(json_path)
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    err("解析采集 JSON 失败：%s" % e)
                    if not dry_run:
                        return
    if bangumi_list is None:
        if dry_run:
            info("dry-run 模式：不调用采集器（避免网络请求），使用内置示例数据测试交互。")
            bangumi_list = fake_bangumi_data()
        else:
            jp = run_collector(year, season, outdir)
            if jp is not None:
                try:
                    bangumi_list = load_bangumi_json(jp)
                    json_path = jp
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    err("解析采集 JSON 失败：%s" % e)
                    return
            else:
                return
    if not bangumi_list:
        warn("没有解析到任何番组。")
        return

    n_sg = sum(len(b["subgroups"]) for b in bangumi_list)
    info("采集完成：共 %d 个番组，%d 个字幕组条目。" % (len(bangumi_list), n_sg))

    # 记录 last_collect
    if not dry_run:
        config["last_collect"] = {
            "year": year, "season": season, "json": str(json_path),
        }
        save_config(config)

    # ---- 勾选番组 ----
    print("\n请勾选要订阅的番组（支持 1,3,5-8 / all / 回车取消）：")
    for i, b in enumerate(bangumi_list, 1):
        print("  [%d] %s（%d 个字幕组）" % (i, b["title"], len(b["subgroups"])))
    picked = ask_multi_select("> ", len(bangumi_list))
    if not picked:
        info("已取消。")
        return

    # ---- 动画根目录（确认） ----
    anime_root = ask_anime_root(config)
    if not anime_root:
        info("未指定动画根目录，已取消。")
        return

    used_names = set()
    added, skipped, failed = [], [], []
    added_items = []  # (path, url) 供显示名校验

    for bi in picked:
        b = bangumi_list[bi - 1]
        title = b["title"]
        bid = b.get("bangumi_id")
        cleaned = _dedup_name(sanitize_feed_name(title, bid), used_names)
        print("\n【%s】" % title)
        info("清洗名称：%s（原始） → %s（清洗后）" % (title, cleaned))

        sgs = b["subgroups"]
        print("【%s】请勾选字幕组（0=聚合全部，a=全部字幕组，或 1,2,3）：" % title)
        print("  [0] (全部字幕组聚合)")
        for i, sg in enumerate(sgs, 1):
            print("  [%d] %s" % (i, sg["name"]))
        try:
            raw = _input("> ").strip()
        except UserAbort:
            info("已取消。")
            break
        except KeyboardInterrupt:
            info("已取消。")
            break
        if raw == "" or raw.lower() == "none":
            info("跳过该番组。")
            continue

        sel_set = set()
        want_agg = False
        bad = False
        for t in raw.split(","):
            t = t.strip()
            if not t:
                continue
            tl = t.lower()
            if tl == "a":
                sel_set.update(range(1, len(sgs) + 1))
            elif t == "0":
                want_agg = True
            else:
                parsed, e = parse_multi_select(t, len(sgs), allow_all=True)
                if parsed is None:
                    warn("%s，跳过该番组" % e)
                    bad = True
                    break
                sel_set.update(parsed)
        if bad:
            continue
        if not want_agg and not sel_set:
            info("未选择任何订阅源，跳过该番组。")
            continue

        # ---- 建文件夹 ----
        folder = os.path.join(anime_root, cleaned)
        folder_existed = os.path.isdir(folder)
        if not dry_run:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                err("创建文件夹失败：%s" % e)
                failed.append((title, "创建文件夹失败: %s" % e))
                stats["errors"] += 1
                continue
            if folder_existed:
                info("文件夹已存在，复用：%s" % folder)
            else:
                info("将创建文件夹：%s" % folder)
                stats["folders_created"] += 1
            config.setdefault("bangumi_folder_map", {})[cleaned] = folder
            save_config(config)
        else:
            info("dry-run：将创建文件夹：%s" % folder)

        # ---- 计算订阅 path ----
        # 仅聚合 → 根级 feed；有字幕组 → 先建文件夹，聚合放 '清洗结果\\聚合'
        sg_picked = [(i, sg) for i, sg in enumerate(sgs, 1) if i in sel_set]
        feed_plan = []
        if want_agg and not sg_picked:
            feed_plan.append((cleaned, b["rss_all"], "聚合"))
        else:
            if want_agg:
                feed_plan.append(("%s\\聚合" % cleaned, b["rss_all"], "聚合"))
            for i, sg in sg_picked:
                seg = _sg_path_segment(sg["name"], i)
                feed_plan.append(("%s\\%s" % (cleaned, seg), sg["rss"], sg["name"]))

        # ---- 读取 qB 现有订阅 ----
        existing_feeds, existing_folders = set(), set()
        if not dry_run:
            try:
                tree = client.get_rss_items(with_data=True)
                existing_feeds, existing_folders = scan_rss_tree(tree)
            except QBError as e:
                warn("读取现有 RSS 列表失败，将直接尝试添加：%s" % e)

        needs_folder = any("\\" in p for p, _, _ in feed_plan)
        if needs_folder and not dry_run:
            if cleaned in existing_feeds:
                warn("qB 中已存在同名订阅「%s」，无法再建同名 RSS 文件夹；请在 qB 中处理后重试。" % cleaned)
                failed.append((title, "qB 中已存在同名订阅，无法建 RSS 文件夹"))
                continue
            if cleaned not in existing_folders:
                try:
                    if client.add_folder(cleaned):
                        info("已在 qB 中创建 RSS 文件夹：%s" % cleaned)
                        existing_folders.add(cleaned)
                except QBError as e:
                    warn("创建 RSS 文件夹失败：%s" % e)

        # ---- 逐条添加订阅 ----
        for path, url, label in feed_plan:
            if not url:
                warn("%s 缺少 RSS 地址，跳过。" % label)
                failed.append((title, "%s 无 RSS 地址" % label))
                continue
            if not dry_run and path in existing_feeds:
                info("该订阅已存在，跳过：%s" % path)
                skipped.append(path)
                stats["feeds_skipped"] += 1
                continue
            try:
                client.add_feed(url, path)
                info("已向 qBittorrent 添加订阅源：%s" % path)
                added.append(path)
                added_items.append((path, url))
                stats["feeds_added"] += 1
            except QBError as e:
                if "409" in str(e) or "冲突" in str(e):
                    info("该订阅已存在，跳过：%s" % path)
                    skipped.append(path)
                    stats["feeds_skipped"] += 1
                else:
                    err("添加订阅失败（%s）：%s" % (path, e))
                    failed.append((path, str(e).splitlines()[0]))
                    stats["errors"] += 1

    # ---- 汇总 ----
    print("\n=== 功能一完成 ===")
    info("新增订阅 %d 个，跳过 %d 个，失败 %d 个。" % (len(added), len(skipped), len(failed)))
    if not dry_run:
        verify_display_names(client, added, dry_run)
        peek_rss_doc_titles(added_items, dry_run)
    if failed:
        err("失败清单：")
        for name, reason in failed:
            print("    - %s：%s" % (name, reason))


# ----------------------------------------------------------------------
# 功能二：未读条目筛选下载
# ----------------------------------------------------------------------
def feature_unread_download(client, config, stats, dry_run):
    print("\n=== 功能二：未读条目筛选下载 ===")
    folder_map = config.get("bangumi_folder_map") or {}
    if not folder_map:
        warn("尚未绑定任何番组文件夹，建议先运行功能一；")
        warn("或现在指定一个动画根目录，程序按清洗后的名称自动生成子文件夹路径。")
        print("  1. 返回主菜单")
        print("  2. 指定动画根目录并继续")
        try:
            ch = _input("请选择 [1]：").strip()
        except UserAbort:
            return
        except KeyboardInterrupt:
            return
        if ch != "2":
            return
        root = ensure_anime_root(config, force=True)
        if not root:
            return
        folder_map = config.get("bangumi_folder_map") or {}

    try:
        version = client.get_app_version()
        info("已登录 qBittorrent（%s，版本 %s）" % (client.base_url, version))
    except QBError as e:
        err("无法获取 qB 版本：%s" % e)
        return

    try:
        tree = client.get_rss_items(with_data=True)
    except QBError as e:
        err("读取 RSS 列表失败：%s" % e)
        return
    feeds = flatten_rss_tree(tree)
    processed = load_processed(config)

    entries = []
    for item_path, art in collect_articles(feeds):
        art_id = art.get("id")
        if art_id is None:
            continue
        key = "%s|%s" % (item_path, art_id)
        if key in processed:
            continue  # 本地 processed 优先于 isRead
        if art.get("isRead", False):
            continue
        link = str(art.get("link") or "").strip()
        # 蜜柑的 link 是番剧页面（HTML），可下载地址在 torrentURL；
        # 磁力类 feed 的 link 本身就是 magnet:，优先直接用。
        torrent_url = str(
            art.get("torrentURL") or art.get("torrentUrl") or ""
        ).strip()
        if link.startswith("magnet:"):
            use_url = link
        elif torrent_url:
            use_url = torrent_url
        elif link:
            use_url = link
        else:
            warn("条目缺少可用的下载链接，跳过：%s" % (art.get("title") or "(无标题)"))
            continue
        top = str(item_path).split("\\", 1)[0]
        entries.append({
            "item_path": item_path,
            "art_id": art_id,
            "title": str(art.get("title") or "(无标题)").strip(),
            "link": use_url,
            "date": art.get("date"),
            "group_key": top,
        })

    if not entries:
        info("没有发现未读条目（或均已处理过）。")
        return

    order, groups = [], {}
    for e in entries:
        g = e["group_key"]
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(e)

    info("共发现 %d 条未读条目，分布于 %d 部动画。" % (len(entries), len(order)))
    print("\n请勾选要下载的条目（1,3,5-8 / all / 回车取消）：")
    flat_list = []
    for g in order:
        dest = folder_map.get(g) or "(未绑定文件夹)"
        print("【%s】→ %s" % (g, dest))
        for e in groups[g]:
            flat_list.append(e)
            print("  [%d] %s  (%s)" % (len(flat_list), e["title"], format_date(e["date"])))
    if not flat_list:
        return
    picked = ask_multi_select("> ", len(flat_list))
    if not picked:
        info("已取消。")
        return

    anime_root = str(config.get("anime_root") or "").strip()
    ok, fail = [], []
    per_anim = Counter()

    for idx in picked:
        e = flat_list[idx - 1]
        g = e["group_key"]
        folder = (config.get("bangumi_folder_map") or {}).get(g)
        if not folder:
            suggestion = os.path.join(anime_root, g) if anime_root else ""
            prompt = "【%s】尚未绑定下载文件夹，请输入其文件夹路径" % g
            if suggestion:
                prompt += " [默认 %s]" % suggestion
            try:
                folder = _input(prompt + "：").strip()
            except UserAbort:
                fail.append((e["title"], "用户中断"))
                break
            except KeyboardInterrupt:
                fail.append((e["title"], "用户中断"))
                break
            if not folder:
                folder = suggestion
            if not folder:
                err("未指定文件夹，跳过：%s" % e["title"])
                fail.append((e["title"], "未指定文件夹"))
                continue
            if not dry_run:
                try:
                    os.makedirs(folder, exist_ok=True)
                except OSError as ex:
                    err("创建文件夹失败：%s" % ex)
                    fail.append((e["title"], "创建文件夹失败: %s" % ex))
                    continue
            config.setdefault("bangumi_folder_map", {})[g] = folder
            folder_map = config["bangumi_folder_map"]
            save_config(config)
            info("已绑定：%s → %s" % (g, folder))

        try:
            client.create_category(g)  # 尽力而为，已存在会被忽略
            client.add_torrent(
                e["link"], savepath=folder, category=g, tags="mikan",
            )
        except QBError as ex:
            err("添加下载失败：%s → %s" % (e["title"], ex))
            fail.append((e["title"], str(ex).splitlines()[0]))
            stats["torrents_fail"] += 1
            continue
        info("已加入下载：%s → %s" % (e["title"], folder))
        ok.append(e)
        per_anim[g] += 1
        stats["torrents_ok"] += 1

        # 标记已读：itemPath 直接取自 RSS 树遍历结果，不自行拼接；
        # 带 articleId 只标记本条，避免把同源其他未读条目误标已读
        try:
            client.mark_as_read(e["item_path"], article_id=e["art_id"])
            stats["marked_read"] += 1
        except QBError as ex:
            warn("标记已读失败（仅记录到本地 processed.json）：%s" % ex)
            stats["mark_read_fail"] += 1
        processed.add("%s|%s" % (e["item_path"], e["art_id"]))
        save_processed(processed, config)
        save_config(config)

    print("\n=== 功能二完成 ===")
    info("已加入下载任务：%d 条；失败 %d 条。" % (len(ok), len(fail)))
    if ok:
        for g, n in per_anim.items():
            print("    %s → %s（%d 条）" % (g, config["bangumi_folder_map"].get(g, "?"), n))
    if fail:
        err("失败清单：")
        for t, reason in fail:
            print("    - %s：%s" % (t, reason))


# ----------------------------------------------------------------------
# --check-qb
# ----------------------------------------------------------------------
def check_qb_command(config, dry_run=False):
    print("=== qBittorrent 连接检查 ===")
    qb = config.get("qb") or {}
    base = str(qb.get("base_url") or "").strip()
    if not base:
        base = _input("qB WebUI 地址 [http://127.0.0.1:8080]：").strip() or "http://127.0.0.1:8080"
    user = str(qb.get("username") or "admin")
    pwd = str(qb.get("password") or "")
    if not pwd:
        # 配置里没有保存密码（如选择了「仅本会话」），单独 --check-qb 时补问一次
        pwd = _input("qB WebUI 密码（回车跳过）：")
        qb["password"] = pwd
    client = DryRunQBClient() if dry_run else QBClient()
    client.login(base, user, pwd)
    result = client.check()
    info("版本：%s" % result["version"])
    feeds = result["feeds"]
    info("共 %d 个 RSS 订阅：" % len(feeds))
    for p, _node in feeds[:50]:
        print("    %s" % p)
    if len(feeds) > 50:
        print("    ... 其余 %d 个省略" % (len(feeds) - 50))


# ----------------------------------------------------------------------
# --self-test
# ----------------------------------------------------------------------
def self_test():
    print("=== 内置自检 ===")
    passed = failed = 0

    def check(name, actual, expected):
        nonlocal passed, failed
        if actual == expected:
            passed += 1
            print("  [通过] %s" % name)
        else:
            failed += 1
            print("  [失败] %s：期望 %r，实际 %r" % (name, expected, actual))

    # --- sanitize_feed_name ---
    check("去前缀+截10字",
          sanitize_feed_name("Mikan Project - 无职英雄 ～技能什么的毫无用处～"),
          "无职英雄 ～技能什么")
    check("去前缀(无尾空格)",
          sanitize_feed_name("Mikan Project-转生恶女的黑历史"),
          "转生恶女的黑历史")
    check("破折号变体 –",
          sanitize_feed_name("Mikan Project – 标题测试字"),
          "标题测试字")
    check("破折号变体 —",
          sanitize_feed_name("Mikan Project—标题测试字"),
          "标题测试字")
    check("无前缀原样",
          sanitize_feed_name("Let's Play 充满任务的人生"),
          "Let's Play")
    check("不足10字全保留",
          sanitize_feed_name("短名"),
          "短名")
    check("保留名加前缀",
          sanitize_feed_name("CON"),
          "_CON")
    check("保留名小写",
          sanitize_feed_name("nul"),
          "_nul")
    check("非法字符替换",
          sanitize_feed_name('a:b*c?d"e<f>g|h'),
          "a_b_c_d_e_")  # 先截前10字 'a:b*c?d"e<' 再替换
    check("非法字符替换(短串)",
          sanitize_feed_name('a:b*c?d'),
          "a_b_c_d")
    check("空值兜底",
          sanitize_feed_name("", 1234),
          "bangumi_1234")
    check("截断后去尾部符号",
          sanitize_feed_name("Mikan Project - abcdefghij.-_ "),
          "abcdefghij")
    check("仅非法字符不为空",
          sanitize_feed_name("???"),
          "___")

    # --- parse_multi_select ---
    check("普通多选", parse_multi_select("1,3", 5)[0], [1, 3])
    check("区间", parse_multi_select("1,3,5-8", 10)[0], [1, 3, 5, 6, 7, 8])
    check("all", parse_multi_select("all", 3)[0], [1, 2, 3])
    check("空输入", parse_multi_select("", 3)[0], [])
    check("none", parse_multi_select("none", 3)[0], [])
    check("区间反转", parse_multi_select("5-3", 5)[0], [3, 4, 5])
    check("越界忽略", parse_multi_select("9", 3)[0], [])
    check("部分越界", parse_multi_select("2-9", 4)[0], [2, 3, 4])
    check("去重", parse_multi_select("1,1,2", 5)[0], [1, 2])
    check("非法字符报错", parse_multi_select("abc", 3)[0], None)
    check("多重区间报错", parse_multi_select("2-4-6", 5)[0], None)

    # --- load_bangumi_json 结构兼容 ---
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"bangumi": [{"bangumi_id": 1, "title": "T",
                            "subgroups": [{"subgroup_id": 2, "name": "S", "rss": "u"}]}]},
              tmp, ensure_ascii=False)
    tmp.close()
    data = load_bangumi_json(tmp.name)
    check("对象结构解析", (len(data), data[0]["title"], len(data[0]["subgroups"])),
          (1, "T", 1))
    os.unlink(tmp.name)

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump([{"bangumi_id": 3, "title": "U", "rss_all": "r", "subgroups": []}],
              tmp, ensure_ascii=False)
    tmp.close()
    data = load_bangumi_json(tmp.name)
    check("数组结构解析", (len(data), data[0]["rss_all"]), (1, "r"))
    os.unlink(tmp.name)

    print("\n自检结果：通过 %d，失败 %d" % (passed, failed))
    return failed == 0


# ----------------------------------------------------------------------
# 主程序
# ----------------------------------------------------------------------
def get_client(existing, config, args):
    if existing is not None:
        return existing
    if args.dry_run:
        return DryRunQBClient()
    qb = config.get("qb") or {}
    client = QBClient(debug=args.debug)
    client.login(
        str(qb.get("base_url") or ""),
        str(qb.get("username") or ""),
        str(qb.get("password") or ""),
    )
    return client


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(
        description="蜜柑计划 RSS × qBittorrent 总控程序（纯标准库）"
    )
    ap.add_argument("--debug", action="store_true",
                    help="打印 HTTP 请求摘要（不打印密码）")
    ap.add_argument("--check-qb", action="store_true",
                    help="仅检查 qBittorrent 连接后退出")
    ap.add_argument("--dry-run", action="store_true",
                    help="演练模式：不向 qB 发送任何写请求，不创建文件夹")
    ap.add_argument("--self-test", action="store_true",
                    help="运行内置单元自检后退出")
    args = ap.parse_args()

    if args.self_test:
        ok = self_test()
        sys.exit(0 if ok else 1)

    first_run = not CONFIG_PATH.exists()
    config = load_config()

    if args.check_qb:
        try:
            check_qb_command(config, dry_run=args.dry_run)
        except QBError as e:
            err(str(e))
            sys.exit(1)
        except UserAbort:
            print()
            return
        return

    try:
        if first_run:
            info("首次运行：请确认以下配置（直接回车使用方括号中的默认值）。")
            ensure_qb_settings(config, force=True)
            ensure_anime_root(config, force=True)
        else:
            ensure_qb_settings(config, force=False)
            ensure_anime_root(config, force=False)
        save_config(config)
    except UserAbort:
        print("\n[信息] 输入已关闭，退出。")
        return

    stats = {
        "feeds_added": 0, "feeds_skipped": 0, "folders_created": 0,
        "torrents_ok": 0, "torrents_fail": 0,
        "marked_read": 0, "mark_read_fail": 0, "errors": 0,
    }
    client = None

    while True:
        print()
        print("================ 蜜柑计划 × qBittorrent 管理器 ================")
        print("1. 采集并导入 RSS 订阅（功能一）")
        print("2. 未读条目筛选下载（功能二）")
        print("3. 检查 qBittorrent 连接")
        print("0. 退出")
        try:
            ch = input("请选择：").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        try:
            if ch == "1":
                client = get_client(client, config, args)
                feature_collect_import(client, config, stats, args.dry_run)
            elif ch == "2":
                client = get_client(client, config, args)
                feature_unread_download(client, config, stats, args.dry_run)
            elif ch == "3":
                check_qb_command(config, dry_run=args.dry_run)
            elif ch in ("0", "q", "quit", "exit"):
                break
            else:
                warn("无效选项，请输入 0-3。")
        except UserAbort:
            print("\n[信息] 输入已关闭，退出。")
            break
        except KeyboardInterrupt:
            print("\n[信息] 已中断，返回主菜单。")
        except QBError as e:
            err(str(e))
            stats["errors"] += 1

    print("\n=== 本轮统计 ===")
    print("    新增订阅源：%d  跳过已存在：%d  创建文件夹：%d"
          % (stats["feeds_added"], stats["feeds_skipped"], stats["folders_created"]))
    print("    下载成功：%d  下载失败：%d"
          % (stats["torrents_ok"], stats["torrents_fail"]))
    print("    标记已读成功：%d  标记失败：%d"
          % (stats["marked_read"], stats["mark_read_fail"]))
    print("    错误次数：%d" % stats["errors"])
    print("再见。")


if __name__ == "__main__":
    main()
