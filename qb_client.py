# -*- coding: utf-8 -*-
"""
qBittorrent WebUI API 客户端（仅标准库，Python 3.8+）

封装 5.0+ 所需接口：
    auth/login, app/version, rss/items, rss/addFolder, rss/addFeed,
    rss/removeItem, rss/markAsRead, torrents/add, torrents/createCategory

用法：
    client = QBClient(debug=False)
    client.login("http://127.0.0.1:8080", "admin", "密码")
    print(client.get_app_version())
"""

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request


class QBError(Exception):
    """qBittorrent 交互统一异常，message 为面向用户的中文提示。"""


TROUBLESHOOT = (
    "排查建议：\n"
    "  1. qBittorrent 是否已开启 WebUI（工具 → 选项 → WebUI）\n"
    "  2. 地址与端口是否正确（默认 http://127.0.0.1:8080）\n"
    "  3. Windows 防火墙是否放行 qBittorrent\n"
    "  4. 先用浏览器打开该地址，确认 WebUI 能正常访问"
)


class QBClient:
    """qBittorrent WebAPI 客户端，内部持有 SID Cookie 会话。"""

    def __init__(self, debug: bool = False, timeout: int = 30):
        self.base_url = ""
        self.debug = debug
        self.timeout = timeout
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj)
        )
        self._logged_in = False

    # ------------------------------------------------------------------ 内部
    def _url(self, path: str) -> str:
        return self.base_url + path

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"[调试] {msg}")

    def _request(self, path: str, method: str = "GET",
                 form: dict = None, timeout: int = None) -> str:
        """发送请求，返回响应文本。失败抛 QBError。"""
        url = self._url(path)
        data = None
        headers = {}
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        shown = dict(form) if form else {}
        if "password" in shown:
            shown["password"] = "***"
        self._log(f"{method} {url} form={shown or '-'}")
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
            if e.code == 403:
                raise QBError(
                    f"HTTP 403 禁止访问：{url}\n"
                    "可能原因：用户名/密码错误；或 WebUI 开启了 CSRF/主机白名单限制。\n"
                    + TROUBLESHOOT
                )
            if e.code == 409:
                raise QBError(f"HTTP 409 冲突：目标已存在（{path}）{('；' + body) if body else ''}")
            raise QBError(f"HTTP {e.code}：{url}{('；响应: ' + body) if body else ''}")
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise QBError(f"请求超时：{url}\n网络慢或 WebUI 无响应，请稍后重试。")
            raise QBError(f"无法连接 qBittorrent WebUI（{url}）：{reason}\n" + TROUBLESHOOT)
        except TimeoutError:
            raise QBError(f"请求超时：{url}\n网络慢或 WebUI 无响应，请稍后重试。")

    def _post_form(self, path: str, form: dict) -> str:
        return self._request(path, method="POST", form=form)

    # ------------------------------------------------------------------ 公开
    def login(self, base_url: str, username: str, password: str) -> bool:
        """建立会话。返回 True 表示已认证（含免登录跳过的情况）。"""
        self.base_url = (base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise QBError("qB WebUI 地址为空，请先在配置中填写。")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "http://" + self.base_url
        try:
            resp = self._post_form(
                "/api/v2/auth/login",
                {"username": username or "", "password": password or ""},
            )
        except QBError as e:
            # 403 常见于「对 localhost 禁用 CSRF 检查」以外的免登录/防护配置
            if "HTTP 403" in str(e):
                print("[警告] 登录接口返回 403，尝试按「本机免登录」继续访问 WebUI ...")
                self._logged_in = True
                return True
            raise
        text = resp.strip()
        if text.startswith("Ok"):
            self._logged_in = True
            return True
        if "Fails" in text:
            raise QBError(
                "登录失败（qB 返回 Fails.）：请检查用户名/密码，\n"
                "并确认 WebUI 已开启且未禁用认证。"
            )
        # 个别版本免登录时返回空体
        self._logged_in = True
        return True

    def get_app_version(self) -> str:
        return self._request("/api/v2/app/version").strip()

    def get_rss_items(self, with_data: bool = True) -> dict:
        """获取 RSS 树。返回 dict：key 为反斜杠层级路径（如 '文件夹\\订阅名'）。"""
        q = "?withData=true" if with_data else ""
        text = self._request(f"/api/v2/rss/items{q}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise QBError(f"解析 RSS 列表失败：{e}")
        if not isinstance(data, dict):
            raise QBError("RSS 列表返回了意外的数据结构。")
        return data

    def add_folder(self, path: str) -> bool:
        """添加 RSS 文件夹。已存在（409）时返回 False，不算错误。"""
        try:
            self._post_form("/api/v2/rss/addFolder", {"path": path})
            return True
        except QBError as e:
            if "409" in str(e) or "冲突" in str(e):
                self._log(f"addFolder 已存在，忽略: {path}")
                return False
            raise

    def add_feed(self, url: str, path: str) -> None:
        """添加 RSS 订阅。path 即 qB RSS 树中的显示名/层级。"""
        self._post_form("/api/v2/rss/addFeed", {"url": url, "path": path})

    def remove_item(self, item_path: str) -> None:
        """删除 RSS 订阅或文件夹（本程序默认不调用，仅供手动处置）。

        参数名跨版本不一致：5.1 源码要求 path，旧文档写 itemPath，
        两个都带上，qB 只读取其认识的那个。
        """
        self._post_form(
            "/api/v2/rss/removeItem",
            {"path": item_path, "itemPath": item_path},
        )

    def mark_as_read(self, item_path: str, article_id=None) -> None:
        """标记已读。itemPath 为 feed 的完整反斜杠路径。

        传 article_id 时只标记该条（qB 5.0+ 支持）；
        不传则把整个订阅源下的条目全部标记为已读。
        """
        form = {"itemPath": item_path}
        if article_id not in (None, ""):
            form["articleId"] = str(article_id)
        self._post_form("/api/v2/rss/markAsRead", form)

    def create_category(self, name: str) -> bool:
        """创建下载分类。已存在时返回 False，不算错误。"""
        try:
            self._post_form("/api/v2/torrents/createCategory", {"category": name})
            return True
        except QBError as e:
            if "409" in str(e) or "冲突" in str(e) or "exist" in str(e).lower():
                return False
            raise

    def add_torrent(self, urls: str, savepath: str,
                    category: str = None, tags: str = None) -> None:
        """添加种子/磁力任务。失败抛 QBError。"""
        form = {"urls": urls, "savepath": savepath}
        if category:
            form["category"] = category
        if tags:
            form["tags"] = tags
        resp = self._post_form("/api/v2/torrents/add", form)
        if "Fails" in resp:
            raise QBError(f"qB 拒绝了下载请求（返回 Fails.）：{urls[:80]}")

    def check(self) -> dict:
        """连接自检：返回 {version, feeds, items}。"""
        version = self.get_app_version()
        tree = self.get_rss_items(with_data=True)
        feeds = flatten_rss_tree(tree)
        return {"version": version, "tree": tree, "feeds": feeds}


# ----------------------------------------------------------------------
# RSS 树解析辅助
# ----------------------------------------------------------------------
def is_feed_node(val) -> bool:
    """qB 的 RSS 树中，feed 节点含 articles/url/uid 等键，folder 节点是纯嵌套 dict。"""
    return isinstance(val, dict) and (
        "articles" in val or "url" in val or "uid" in val
    )


def flatten_rss_tree(tree: dict):
    """把 RSS 树摊平成 [(itemPath, feed_node), ...]。

    itemPath 是反斜杠层级路径（如 'Folder\\FeedName'），
    与 markAsRead 所需的 itemPath 一致；若节点自带 path 字段则优先使用。
    """
    result = []

    def walk(node: dict, prefix: str) -> None:
        for key, val in node.items():
            full = f"{prefix}\\{key}" if prefix else str(key)
            if is_feed_node(val):
                item_path = val.get("path") or full
                result.append((item_path, val))
            elif isinstance(val, dict):
                walk(val, full)

    walk(tree or {}, "")
    return result


def collect_articles(feeds):
    """从 flatten_rss_tree 的结果中收集文章。

    返回 [(itemPath, article_dict), ...]；article 可能缺字段，由调用方兜底。
    """
    out = []
    for item_path, node in feeds:
        articles = node.get("articles") or []
        for art in articles:
            if isinstance(art, dict):
                out.append((item_path, art))
    return out
