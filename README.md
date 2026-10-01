# 蜜柑计划 RSS × qBittorrent 总控程序

把「选番 → 导入 RSS 订阅 → 挑未读 → 下载到对应番组文件夹」这条链路自动化。

- **蜜柑计划**（镜像站 `https://mikanani.kas.pub`）提供每季新番的字幕组 RSS。
- **qBittorrent 5.0+** 通过 WebUI 的 RSS 功能订阅这些源并下载种子。

仅使用 **Python 标准库**，无任何第三方依赖。

---

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows |
| Python | 3.8+（本机请用 `py -3` 启动，见下） |
| qBittorrent | 5.0+，已开启 WebUI |
| 网络 | 可访问 `mikanani.kas.pub` 与本机 qB WebUI |

> **重要**：本机 `python` / `python3` 指向 Windows 商店占位程序，直接运行会静默退出（退出码 49）。
> 请统一使用启动器：`py -3 mikan_qb_manager.py`。
> 程序内部调用采集脚本时也会自动按 `py -3 → python → sys.executable` 顺序探测可用的 Python。

## 二、文件说明

| 文件 | 作用 |
| --- | --- |
| `mikan.bat` | **快捷启动（双击即可运行）**，可透传命令行参数 |
| `mikan_qb_manager.py` | 主程序（入口） |
| `qb_client.py` | qBittorrent WebUI API 封装（登录 / RSS / 下载） |
| `mikan_rss_collector.py` | 已有的季度采集脚本（本程序只调用，不修改） |
| `使用说明.txt` | 采集脚本的原使用说明 |
| `config.json` | 首次运行自动生成：qB 连接、动画根目录、番组→文件夹映射 |
| `processed.json` | 已处理条目记录（`feedpath\|articleid` 列表），防重复下载 |
| `output/` | 采集器输出的 JSON/OPML/CSV |
| `_test_feature2.py` | 功能二的离线模拟测试（假 qB 客户端，不需要真 qB） |

## 三、快速开始

**日常使用：双击脚本目录下的 `mikan.bat` 即可**（自动切换 UTF-8 代码页、进入程序目录、结束后暂停方便看结果）。也可带参数启动：

```bat
mikan.bat --check-qb     等价于 py -3 mikan_qb_manager.py --check-qb
mikan.bat --debug
```

命令行手动启动：

```bat
cd /d "脚本所在目录"
py -3 mikan_qb_manager.py
```

> `mikan.bat` 须保持纯 ASCII 内容——cmd 按 GBK 解析批处理，UTF-8 中文注释会破坏解析。

首次运行会依次询问：

1. qB WebUI 地址（默认 `http://127.0.0.1:8080`）
2. 用户名（默认 `admin`）
3. 密码（回车跳过；输入后会再问是否明文保存到 `config.json`）
4. 动画根目录（如 `F:\Anime`）

之后进入主菜单：

```
================ 蜜柑计划 × qBittorrent 管理器 ================
1. 采集并导入 RSS 订阅（功能一）
2. 未读条目筛选下载（功能二）
3. 检查 qBittorrent 连接
0. 退出
```

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--check-qb` | 只检查 qB 连接（登录 + 版本 + 列出 RSS 订阅）后退出 |
| `--debug` | 打印 HTTP 请求摘要（密码打码，不泄露） |
| `--dry-run` | 演练模式：不向 qB 发送任何写请求、不创建文件夹，只打印计划动作；采集失败时用内置示例数据测试交互 |
| `--self-test` | 运行内置单元自检（名称清洗、多选解析、JSON 结构兼容）后退出 |

建议先跑一次：

```bat
py -3 mikan_qb_manager.py --check-qb     # 确认 qB 连得上
py -3 mikan_qb_manager.py --self-test    # 确认程序逻辑正常
```

## 四、功能一：采集并导入 RSS

流程：问年份/季度 → 调用 `mikan_rss_collector.py` 采集（48 小时内已有 JSON 可跳过）→ 勾选番组 → 每部番勾选字幕组 → 建文件夹 + 添加订阅。

勾选语法（功能一、二通用）：

```
1,3,5-8     逗号分隔 + 闭区间
all / a     全选（字幕组菜单里 a = 全部字幕组）
0           字幕组菜单里 = 聚合 RSS
5-3         区间反了会自动交换
回车 / none 取消
```

越界编号忽略并警告、重复编号去重、非法字符提示重输，均不会崩溃。

订阅源 / 文件夹命名规则（`sanitize_feed_name`）：

1. 去掉开头的 `Mikan Project - `（兼容 `–` `—` 等破折号变体）
2. 去首尾空白
3. 截断到前 **10 个 Unicode 字符**（中文算 1 个字）
4. 截断后去掉末尾的空格 `.` `-` `_`
5. Windows 非法字符 `\ / : * ? " < > |` 替换为 `_`
6. 命中 Windows 保留名（`CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`）时加 `_` 前缀
7. 结果为空时用 `bangumi_{番组ID}` 兜底

在 qB RSS 树中的 path 结构：

| 用户选择 | addFeed 的 path | 磁盘文件夹 |
| --- | --- | --- |
| 仅聚合 RSS（0） | `清洗结果` | `根目录\清洗结果` |
| 任一字幕组 | `清洗结果\字幕组名`（先建同名 RSS 文件夹） | `根目录\清洗结果`（多字幕组共用） |
| 聚合 + 字幕组都选 | 聚合放 `清洗结果\聚合`，字幕组各占一行 | 同上 |

- 同一番组的多个字幕组**共用同一个番组文件夹**。
- 同一季两个番组清洗后重名时，后来的自动加 `_2`、`_3` 并打印提示。
- 添加前会查 qB 现有 RSS 树：path 已存在的**跳过**，绝不自动删除旧订阅。
- 单个番组失败只记录并继续，不中断整体流程。
- 添加完成后会回读 RSS 树核对显示名；还会（以 0.4 秒间隔）抽查 RSS 文档 `<title>`，若仍带前缀则打印警告并说明原因。

注意：qB **不支持 OPML 导入**，本程序对每个订阅逐条调用 `addFeed`，不要把采集器生成的 OPML 导进 qB。

## 五、功能二：未读筛选下载

流程：登录 qB → 拉取 RSS 树 → 筛未读 → 按番组分组展示 → 勾选 → 下载 → 标记已读。

- 未读判定：`isRead` 不为 true（qB 5.1 未读条目会**省略 `isRead` 键**，程序按未读处理），且 article id 不在本地 `processed.json` 中（**本地记录优先**）。
- 分组依据：feed path 的第一段（即功能一里设的清洗名）→ 查 `bangumi_folder_map` 找文件夹。
- 未绑定文件夹时暂停询问，可回车采用默认建议 `动画根目录\清洗名`，结果写回映射。
- 下载调用 `torrents/add`：`savepath`=番组文件夹，`category`=清洗后的番组名，`tags`=`mikan`。
- 下载链接优先级：`magnet:` 开头的 `link` → `torrentURL`（蜜柑的 `link` 是番剧页面 HTML，真正的 .torrent 在 `torrentURL`）→ 其他 `link`。
- 成功后调用 `markAsRead`，`itemPath` **直接取自 RSS 树遍历结果**（如 `文件夹\订阅名`），并附带 `articleId` **只标记下载的这一条**，不会把同订阅源里其他未读条目误标已读。
- 标记已读失败不重试，只把条目写进 `processed.json`，下次不会再出现在未读列表。
- 首次进入功能二时若映射为空，会提示先跑功能一，或现在指定根目录自动生成子文件夹路径。

## 六、配置文件

`config.json`（脚本同目录，首次运行生成）：

```json
{
  "qb": {"base_url": "http://127.0.0.1:8080", "username": "admin", "password": ""},
  "anime_root": "F:\\Anime",
  "bangumi_folder_map": {"清洗后的番组名": "F:\\Anime\\清洗后的番组名"},
  "processed_articles": [],
  "last_collect": {"year": 2026, "season": "冬", "json": "output/2026冬_rss.json"}
}
```

- 密码明文保存（个人使用）；若在首次配置时选择不保存，则仅存于本次会话内存，下次运行会重新询问，**不会以任何形式写入磁盘**。
- `processed_articles` 是历史字段；已处理条目现以 `processed.json` 为准，旧字段仍会被合并读取，兼容老配置。

## 七、常见问题

**Q: 启动后 qB 连不上？**
先用浏览器打开 WebUI 地址确认能访问；检查 qB「工具 → 选项 → WebUI」是否勾选「启用 Web 用户界面」、端口是否一致、防火墙是否放行。程序在连接失败时也会打印同样的排查建议。若 qB 开了本机免登录（Bypass auth），登录接口返回 403 时程序会自动按免登录继续。

**Q: 登录返回 Fails.？**
用户名/密码错误，或 WebUI 认证配置有变。用 `--check-qb` 单独验证。

**Q: qB 的 RSS 列表里名称仍带 `Mikan Project - `？**
程序已把 addFeed 的 `path` 设为清洗后的名称。若 qB 界面某些视图仍按 RSS 文档 `<title>` 显示，程序无法控制该行为，会打印警告提示；此时可在 qB 中手动改名或升级 qB。

**Q: 中文终端乱码？**
程序启动时已把 stdout/stderr 切到 UTF-8。若仍有乱码，运行前执行 `chcp 65001`，或 `set PYTHONIOENCODING=utf-8`。

**Q: 采集脚本报错 / 一直失败？**
单独手动跑采集器看完整输出：
`py -3 mikan_rss_collector.py -y 2025 -s 秋 --limit 3`
常见原因：参数写错、季度太新还没有番组、镜像站偶发 500（内置 3 次重试，仍失败就稍后再试）。

**Q: 想换一个字幕组 / 删掉某个订阅？**
本程序不提供删除（绝不自动删旧订阅）。请直接在 qB 的 RSS 面板里操作。

**Q: 会不会重复下载？**
不会。成功的条目同时记入 qB 已读状态和本地 `processed.json`，本地记录优先，即使 qB 已读状态丢失也不会重复出现。

**Q: 如何测试而不碰真实 qB？**
`py -3 mikan_qb_manager.py --dry-run`：全流程走交互但不发任何写请求；采集失败时自动用内置示例数据。
`py -3 _test_feature2.py`：离线模拟功能二的下载/标记已读链路。

## 八、设计说明与默认值

实现时采用以下默认值与设计取舍：

1. **同一番组多字幕组共用一个番组文件夹**（文件夹名 = 清洗结果）；qB 侧用 `清洗结果\字幕组名` 区分订阅。
2. **聚合 + 字幕组同时勾选**时，聚合订阅放在 `清洗结果\聚合` 下（避免与同名 RSS 文件夹冲突）；仅勾选聚合时 path 就是 `清洗结果`。
3. 下载任务 **`category` = 清洗后的番组名，`tags` = `mikan`**。
4. **不做**「删除已下载条目后自动清空未读」之类的额外状态管理；只按规格标记已读 + 写 `processed.json`。
5. 采集 JSON 兼容两种结构：顶层对象含 `bangumi` 键（采集器实际输出）与顶层数组；字段名也做了多种兼容（`rss_all`/`rssAll` 等）。
6. 48 小时内已存在同季度 JSON 时询问是否复用，避免重复抓取；复用不校验内容完整性。
7. qB 5.0+ 为唯一目标版本，不做 4.x 兼容。已在 qB **v5.1.2** 实机验证：登录、`addFeed`（显示名干净、无前缀）、`addFolder`、`torrents/add`、`markAsRead`（带 articleId）全部通过；`removeItem` 的参数名在 5.1 源码中是 `path`（旧文档写 `itemPath`），程序两个参数都携带以兼容。
8. 已处理条目以 `feedpath|articleid` 为键存 `processed.json`（`config.json` 的 `processed_articles` 作为历史字段兼容读取）。

## 九、礼貌抓取

- 采集器保持默认 `--delay 0.4` 秒请求间隔，**严禁**设 0 或并发。
- 本程序对 RSS 文档的显示名校验同样保持 ≥0.4 秒间隔。
- 这是个人维护的镜像站，过度抓取可能导致被封 IP 或站点宕机。

下载资源请遵守当地法律法规及字幕组的发布协议。本工具仅管理 RSS 订阅与下载任务的流转。

## 十、许可证

本项目采用 [MIT License](LICENSE) 开源。
