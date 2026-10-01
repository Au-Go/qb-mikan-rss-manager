# -*- coding: utf-8 -*-
"""功能二模拟测试：假 qB 客户端 + 假 RSS 树，验证筛选/分组/下载/标记已读链路。"""
import builtins
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mikan_qb_manager as m  # noqa: E402

TMP = tempfile.mkdtemp(prefix="mikan_qb_test_")
print("临时目录:", TMP)

# 备份真实配置，测试后恢复
cfg_path = m.CONFIG_PATH
proc_path = m.PROCESSED_PATH
cfg_bak = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None


class FakeClient(m.QBClient):
    def __init__(self):
        super().__init__()
        self.base_url = "http://fake"
        self.added = []
        self.read = []

    def get_app_version(self):
        return "5.0.4-fake"

    def get_rss_items(self, with_data=True):
        return {
            "无职英雄 ～技能什么": {
                "articles": [
                    {"id": 101, "title": "无职英雄 - 第05话 [H-Enc]",
                     "link": "magnet:?xt=urn:btih:aaa", "date": "2026-10-01",
                     "isRead": False},
                    {"id": 102, "title": "无职英雄 - 第04话 [H-Enc]",
                     "link": "magnet:?xt=urn:btih:bbb", "date": "2026-09-24",
                     "isRead": True},  # 已读，应被过滤
                    {"id": 103, "title": "无职英雄 - 第03话 [H-Enc]",
                     "link": "magnet:?xt=urn:btih:ccc", "date": "2026-09-17",
                     "isRead": False},  # 在 processed 中，应被过滤
                ],
                "uid": "1", "url": "https://x/1",
            },
            "Folder2\\未绑定番组": {
                "articles": [
                    {"id": 201, "title": "未绑定番组 - 第01话",
                     "link": "magnet:?xt=urn:btih:ddd", "date": "2026-10-02",
                     "isRead": False},
                ],
                "uid": "2", "url": "https://x/2",
            },
        }

    def create_category(self, name):
        return True

    def add_torrent(self, urls, savepath, category=None, tags=None):
        self.added.append({"urls": urls, "savepath": savepath,
                           "category": category, "tags": tags})

    def mark_as_read(self, item_path, article_id=None):
        self.read.append((item_path, article_id))


config = m.load_config()
config["anime_root"] = TMP
config["bangumi_folder_map"] = {
    "无职英雄 ～技能什么": os.path.join(TMP, "无职英雄 ～技能什么"),
}
m.save_processed(
    {"Folder2\\未绑定番组|999", "无职英雄 ～技能什么|103"}, config
)  # 预置本地已处理（103 应被过滤，999 用于验证旧记录保留）

stats = {"torrents_ok": 0, "torrents_fail": 0, "marked_read": 0,
         "mark_read_fail": 0, "errors": 0}

# 交互输入：勾选 1,2（1=已绑定番组条目，2=未绑定番组条目）；
# 未绑定条目询问文件夹时回车 → 使用默认建议 TMP\Folder2
inputs = iter(["1,2", ""])
real_input = builtins.input


def fake_input(prompt=""):
    print(prompt, end="", flush=True)
    val = next(inputs)
    print(val)
    return val


builtins.input = fake_input
fake = FakeClient()
try:
    m.feature_unread_download(fake, config, stats, dry_run=False)
finally:
    builtins.input = real_input

# 校验
client_ok = True
print("\n--- 断言 ---")


def check(name, cond, detail=""):
    global client_ok
    if cond:
        print("  [通过] %s" % name)
    else:
        client_ok = False
        print("  [失败] %s %s" % (name, detail))


check("下载成功 2 条", stats["torrents_ok"] == 2, "实际 %s" % stats["torrents_ok"])
check("下载失败 0 条", stats["torrents_fail"] == 0, "实际 %s" % stats["torrents_fail"])
check("标记已读计数 2", stats["marked_read"] == 2, "实际 %s" % stats["marked_read"])
check("add_torrent 调用 2 次", len(fake.added) == 2, fake.added)
if fake.added:
    check("savepath 为绑定文件夹",
          fake.added[0]["savepath"] == os.path.join(TMP, "无职英雄 ～技能什么"),
          fake.added[0])
    check("category=清洗名/tags=mikan",
          fake.added[0]["category"] == "无职英雄 ～技能什么"
          and fake.added[0]["tags"] == "mikan", fake.added[0])
check("markAsRead 使用树中真实 itemPath + articleId",
      fake.read == [("无职英雄 ～技能什么", 101), ("Folder2\\未绑定番组", 201)], fake.read)
check("绑定目录已创建", os.path.isdir(os.path.join(TMP, "Folder2")))
check("番组映射写回", config["bangumi_folder_map"].get("Folder2") ==
      os.path.join(TMP, "Folder2"), config["bangumi_folder_map"])
processed = set(json.loads(proc_path.read_text(encoding="utf-8")))
check("processed 含已绑定条目", "无职英雄 ～技能什么|101" in processed, processed)
check("processed 含新绑定条目", "Folder2\\未绑定番组|201" in processed, processed)
check("processed 保留旧记录", "Folder2\\未绑定番组|999" in processed, processed)
check("processed 不含已读条目", "无职英雄 ～技能什么|102" not in processed, processed)

# 恢复
if cfg_bak is not None:
    cfg_path.write_text(cfg_bak, encoding="utf-8")
else:
    if cfg_path.exists():
        cfg_path.unlink()
if proc_path.exists():
    proc_path.unlink()
shutil.rmtree(TMP, ignore_errors=True)

print("\n结果:", "全部通过" if client_ok else "存在失败")
sys.exit(0 if client_ok else 1)
