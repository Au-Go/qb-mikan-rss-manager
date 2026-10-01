# -*- coding: utf-8 -*-
"""
蜜柑计划(mikanani.kas.pub) 季度字幕组 RSS 采集器

用法示例:
    python mikan_rss_collector.py --year 2025 --season 秋
    python mikan_rss_collector.py -y 2026 -s Winter --limit 5
    python mikan_rss_collector.py -y 2025 -s 夏 --delay 0.5

站点接口(已实测):
    1. 季度番组目录:  GET /Home/BangumiCoverFlowByDayOfWeek?year={Y}&seasonStr={春|夏|秋|冬}
       返回整季所有番组的 HTML 片段, 番组链接形如 /Home/Bangumi/{id}
    2. 番组详情页:    GET /Home/Bangumi/{id}
       可解析出番组标题 + 各字幕组名称(id) + 对应 RSS 地址
    3. RSS 订阅源:
       全部字幕组聚合: /RSS/Bangumi?bangumiId={id}
       单个字幕组:     /RSS/Bangumi?bangumiId={id}&subgroupid={sgid}

输出(默认写入 output/ 目录):
    {year}{season}_rss.json   结构化完整数据
    {year}{season}_rss.opml   可直接导入 RSS 阅读器(Inoreader / Follow / NetNewsWire 等)
    {year}{season}_rss.csv    扁平表格

仅使用 Python 标准库, 无需安装依赖。
"""

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://mikanani.kas.pub"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SEASON_ALIASES = {
    "春": "春", "spring": "春", "1": "春",
    "夏": "夏", "summer": "夏", "2": "夏",
    "秋": "秋", "fall": "秋", "autumn": "秋", "3": "秋",
    "冬": "冬", "winter": "冬", "4": "冬",
}
SEASON_EN = {"春": "Spring", "夏": "Summer", "秋": "Fall", "冬": "Winter"}


def normalize_season(raw: str) -> str:
    key = raw.strip().lower()
    if key in SEASON_ALIASES:
        return SEASON_ALIASES[key]
    # 兼容 "2025Fall" 之类写法中的季节后缀
    for alias, cn in SEASON_ALIASES.items():
        if key.endswith(alias):
            return cn
    raise SystemExit(f"无法识别的季度: {raw!r} (可用: 春/夏/秋/冬 或 Spring/Summer/Fall/Winter)")


def fetch(url: str, retries: int = 3, timeout: int = 30) -> str:
    """带重试的 GET, 返回 UTF-8 文本。"""
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            print(f"    [重试 {attempt}/{retries}] {url} -> {e}", file=sys.stderr)
            time.sleep(1.5 * attempt)
    raise SystemExit(f"请求失败: {url} ({last_err})")


def get_season_bangumi(year: int, season_cn: str):
    """返回 [(bangumi_id, 番组名), ...], 番组名可能为空字符串。"""
    qs = urllib.parse.urlencode({"year": year, "seasonStr": season_cn})
    page = fetch(f"{BASE}/Home/BangumiCoverFlowByDayOfWeek?{qs}")
    # 番组链接的 title 属性里带完整名称: <a href="/Home/Bangumi/3737" ... title="无职英雄 ...">
    pat = re.compile(r'href="/Home/Bangumi/(\d+)"[^>]*?\stitle="([^"]*)"')
    seen, result = set(), []
    for bid, title in pat.findall(page):
        if bid in seen:
            continue
        seen.add(bid)
        result.append((bid, html.unescape(title).strip()))
    # 兜底: 有的番组可能没有 title 属性
    for bid in re.findall(r'href="/Home/Bangumi/(\d+)"', page):
        if bid not in seen:
            seen.add(bid)
            result.append((bid, ""))
    return result


def get_bangumi_detail(bangumi_id: str):
    """返回 (番组标题, [ {subgroup_id, name}, ... ])。"""
    page = fetch(f"{BASE}/Home/Bangumi/{bangumi_id}")
    m = re.search(r'class="title"\s+style="color:white;">([^<]+)</p>', page)
    title = html.unescape(m.group(1)).strip() if m else ""

    subgroups = []
    # 每个字幕组一个 <div class="subgroup-text" id="{sgid}"> 块
    blocks = re.split(r'<div class="subgroup-text" id="(\d+)">', page)
    # blocks = [前文, sgid1, 块1内容, sgid2, 块2内容, ...]
    for i in range(1, len(blocks) - 1, 2):
        sgid, content = blocks[i], blocks[i + 1]
        # 组名是块内第一个指向 /Home/PublishGroup/ 的链接
        gm = re.search(r'href="/Home/PublishGroup/\d+"[^>]*>([^<]+)</a>', content)
        name = html.unescape(gm.group(1)).strip() if gm else f"字幕组{sgid}"
        subgroups.append({"subgroup_id": int(sgid), "name": name})
    return title, subgroups


def rss_all(bangumi_id: str) -> str:
    return f"{BASE}/RSS/Bangumi?bangumiId={bangumi_id}"


def rss_one(bangumi_id: str, subgroup_id: int) -> str:
    return f"{BASE}/RSS/Bangumi?bangumiId={bangumi_id}&subgroupid={subgroup_id}"


def write_opml(path: Path, items, season_label: str):
    """items: [{title, rss}] 的扁平列表。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>Mikan {season_label} 字幕组 RSS</title>",
        "  </head>",
        "  <body>",
    ]
    for it in items:
        t = (it["title"] or "未命名").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
        u = it["rss"].replace("&", "&amp;")
        lines.append(f'    <outline type="rss" text="{t}" title="{t}" xmlUrl="{u}" />')
    lines += ["  </body>", "</opml>"]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="蜜柑计划 季度字幕组 RSS 采集器")
    ap.add_argument("-y", "--year", type=int, required=True, help="年份, 如 2025")
    ap.add_argument("-s", "--season", required=True,
                    help="季度: 春/夏/秋/冬 或 Spring/Summer/Fall/Winter")
    ap.add_argument("--limit", type=int, default=0,
                    help="只采集前 N 个番组 (0 = 全部), 用于测试")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="每次请求间隔秒数, 默认 0.4, 请勿设为 0")
    ap.add_argument("-o", "--outdir", default="output", help="输出目录, 默认 output/")
    args = ap.parse_args()

    season_cn = normalize_season(args.season)
    season_en = SEASON_EN[season_cn]
    label = f"{args.year}{season_cn}季"
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.year}{season_cn}"

    print(f"==> 获取 {label} ({season_en}) 番组目录 ...")
    bangumi_list = get_season_bangumi(args.year, season_cn)
    total = len(bangumi_list)
    if total == 0:
        raise SystemExit(f"该季度没有获取到任何番组, 请检查 year/season 参数。")
    if args.limit > 0:
        bangumi_list = bangumi_list[:args.limit]
    print(f"    共 {total} 个番组, 本次采集 {len(bangumi_list)} 个\n")

    data = {
        "source": BASE,
        "year": args.year,
        "season": season_cn,
        "season_en": season_en,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bangumi": [],
    }
    flat = []  # (番组名, 字幕组, RSS)

    for idx, (bid, hint) in enumerate(bangumi_list, 1):
        print(f"[{idx}/{len(bangumi_list)}] /Home/Bangumi/{bid} ...", end=" ", flush=True)
        time.sleep(args.delay)
        try:
            title, subgroups = get_bangumi_detail(bid)
        except SystemExit as e:
            print(f"跳过 ({e})")
            continue
        name = title or hint or bid
        entry = {
            "bangumi_id": int(bid),
            "title": name,
            "rss_all": rss_all(bid),
            "subgroups": [
                {
                    "subgroup_id": s["subgroup_id"],
                    "name": s["name"],
                    "rss": rss_one(bid, s["subgroup_id"]),
                }
                for s in subgroups
            ],
        }
        data["bangumi"].append(entry)
        flat.append({"title": f"{name} (全部)", "rss": entry["rss_all"]})
        for s in entry["subgroups"]:
            flat.append({"title": f"{name} - {s['name']}", "rss": s["rss"]})
        print(f"{name}  [{len(subgroups)} 个字幕组]")

    json_path = outdir / f"{stem}_rss.json"
    opml_path = outdir / f"{stem}_rss.opml"
    csv_path = outdir / f"{stem}_rss.csv"

    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_opml(opml_path, flat, label)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["番组", "字幕组", "RSS地址"])
        for row in flat:
            if row["title"].endswith(" (全部)"):
                w.writerow([row["title"][:-len(" (全部)")], "（全部聚合）", row["rss"]])
            else:
                b, _, sg = row["title"].partition(" - ")
                w.writerow([b, sg, row["rss"]])

    n_bg = len(data["bangumi"])
    n_sg = sum(len(b["subgroups"]) for b in data["bangumi"])
    print(f"\n==> 完成: {n_bg} 个番组, {n_sg} 个字幕组, 共 {len(flat)} 条 RSS")
    print(f"    JSON: {json_path}")
    print(f"    OPML: {opml_path}  (可导入 RSS 阅读器)")
    print(f"    CSV:  {csv_path}")


if __name__ == "__main__":
    main()
