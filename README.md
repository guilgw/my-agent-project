# 轨道工作台 × DeepSeek Harness

把本机的「轨道 · AI 生产运营中台」接到 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（命令 `dsh`）。智能体通过一个 Python MCP 服务调用工作台里已经写好的函数；聊天在左边，工作台页面在 Harness 右侧浏览器里。

本仓库只放集成代码。不要向 [guilgw/rail-content-studio](https://github.com/guilgw/rail-content-studio) 或 [xiaofanma666/rail-content-studio](https://github.com/xiaofanma666/rail-content-studio) 推送、开分支或提 PR，也不要改 Harness 官方源码。工作台和 Harness 都只在本机克隆，用来阅读和运行。

面向的是做出海火车票短视频的团队。流程停在人工选用和人工终审上：工具可以生成三版脚本、制作和评测，但不能自己挑一版，也不能发布。发布由运营到平台上手动完成。

## 目录

| 路径 | 作用 |
| --- | --- |
| `mcp_server/` | stdio MCP 服务。导入工作台模块，不重写业务规则 |
| `.dsh/skills/rail-overseas-studio/SKILL.md` | 给 Harness 的岗位技能。工作区是本仓库时会被发现 |
| `harness/rail-studio.cordis.yml` | `dsh --profile web --patch` 用的补丁：挂上 MCP，并打开右侧浏览器 |
| `harness/plugins/rail-studio-sidebar/` | 小客户端插件。会话里打开工作台页面，并留一个「打开工作台」按钮 |
| `scripts/start.sh`、`scripts/start-macos.command` | macOS 一键启动工作台和 `dsh` |
| `config.example.env` | 路径和端口示例。复制成 `config.env` 后再改，不要提交 |
| `tests/test_mcp.py` | 协议、只读工具和人工门槛的单元测试 |
| `scripts/probe-mcp.mjs` | 用官方 MCP 客户端列出工具。需要另行安装客户端，不在本仓库依赖里 |

## 前置条件

- Node.js 24.9 或更新版本。Harness `0.2.2-rc.3` 不接受更旧的 Node。
- 能运行 `dsh`。任选一种：
  - `npm install -g @deepseek-ai/dsh@0.2.2-rc.3`
  - 或在 Harness 源码目录执行 `pnpm install && pnpm run build`，再用 `pnpm dsh`
- Python 3.9 或更新版本。MCP 服务本身只用标准库。
- 工作台完整流水线还需要它自己的依赖：在工作台目录建虚拟环境并 `python -m pip install -r requirements.txt`。配音和剪映草稿主要在 macOS 上验证过（`afconvert`，没有 MiniMax 时用 `say`）。
- DeepSeek API Key 不是启动所必需的。写脚本、AI 改写和成片评测才需要。配音可选 MiniMax；没有就走系统语音。YouTube 采集需要 `YOUTUBE_API_KEY`。密钥放在工作台的 `.env`，格式见工作台的 `.env.example`。不要把 `.env` 或 `config.env` 提交到 Git。

本机默认路径：

| 用途 | 默认 |
| --- | --- |
| 工作台 | `~/Documents/loop/rail-content-studio` |
| Harness 源码 | `~/Documents/loop/deepseek-harness` |
| 本仓库 | `~/Documents/loop/my-agent-project` |
| 工作台端口 | `18770`（`8770` 在 macOS 上经常被系统占用） |
| Harness Web | `3080` |

都可以通过环境变量或 `config.env` 改掉。

## 安装

```bash
git clone https://github.com/xiaofanma666/rail-content-studio.git ~/Documents/loop/rail-content-studio
git clone https://github.com/deepseek-ai/deepseek-harness.git ~/Documents/loop/deepseek-harness
git clone https://github.com/guilgw/my-agent-project.git ~/Documents/loop/my-agent-project

cd ~/Documents/loop/rail-content-studio
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# 在 .env 里填写自己的密钥。可以先留空，只读工具仍然可用。

cd ~/Documents/loop/my-agent-project
cp config.example.env config.env   # 路径不同时再改
npm install -g @deepseek-ai/dsh@0.2.2-rc.3
```

从源码跑 Harness 时，装好依赖并 `pnpm run build` 之后，把 `DSH_BIN` 指到 `~/Documents/loop/deepseek-harness/node_modules/.bin/dsh`。

Harness 启动 MCP 子进程时会抹掉变量名里带 `KEY`、`TOKEN`、`SECRET`、`PASSWORD` 的环境变量。补丁用 `!!js process.env.XXX` 把这些变量显式交回去，文件里不写密钥。写在工作台 `.env` 里的密钥由工作台自己的 `llm.load_env()` 读取，不依赖这一步。

## 启动

在 macOS 上双击 `scripts/start-macos.command`，或在终端执行：

```bash
./scripts/start.sh
```

脚本会：

1. 读 `config.env`（没有就用上面的默认路径）。
2. 优先用工作台的 `.venv/bin/python`，否则用 `python3`。
3. 在 macOS 上从系统钥匙串导出 `.run/ca-bundle.pem`，并设为 `SSL_CERT_FILE`（已经设置过则不覆盖）。
4. 若设置了 `TIKHUB_API_KEY`：生成本机证书、把证书追加进这个 CA 包、生成本地令牌，再把 TikTok 数据地址和令牌交给即将启动的工作台进程。
5. 以 `PORT=18770` 启动 `studio.py`，并在有密钥时启动 `python -m tikhub_feed`。
6. 执行 `dsh --profile web --patch <本仓库>/harness/rail-studio.cordis.yml --host 127.0.0.1 --port 3080`。

`--patch` 是启动器的参数，必须和 `--profile` 写在一起。写成 `dsh web --port 3080 --patch ...` 时，Web 应用会把它当成未知选项。设 `DSH_NO_OPEN=1` 可以不让 dsh 打开系统浏览器。按 Ctrl+C 会一并停掉工作台。

打开 Harness 之后新建一个会话，工作区选本仓库，这样技能 `rail-overseas-studio` 才会出现。让模型先加载这个技能，再开始做片。

## 左聊天，右工作台

Harness Web 档案默认关闭右侧浏览器。补丁把自带插件 `ui-sidebar-browser` 的 `disabled` 设为 `false`。旁边的客户端插件在每个浏览器标签页第一次进入会话时调用：

```js
ctx.sidebarRight.openTab('browser', { params: { url: 'http://127.0.0.1:18770' } })
```

输入框旁还有「打开工作台」按钮，可以再开一次。

自动打开没有出现时，在右侧点「浏览器」，地址栏输入 `http://127.0.0.1:18770`。改了 `RAIL_STUDIO_PORT` 之后，还要改 `harness/plugins/rail-studio-sidebar/client.js` 里的 `DEFAULT_STUDIO_URL`，否则按钮仍打开 18770。

右侧浏览器是 iframe，默认带沙箱。工作台页面可以显示；MP4 下载可能被拦下。导出工具会返回 `output/exports/` 里的文件路径。

也可以把插件目录装进配置档（插件管理器 → 添加本地目录）。一键启动不需要这一步，`--patch` 已经用相对路径加载它。两套补丁不要同时启用，否则会插入两条 MCP。

## 工具

模型看到的名字是 `mcp__rail_studio__<工具名>`。服务标识 `serverName` 是 `rail_studio`。

| 工具 | 做什么 | 没有密钥时 |
| --- | --- | --- |
| `studio_status` | 路径、页面地址、密钥是否配好。不返回密钥 | 可用 |
| `read_video_hotspots` | 读本地热点缓存和仓库里的今日灵感。不发起付费采集 | 可用 |
| `collect_video_trends` | YouTube 走工作台；TikTok 走 TikHub，每次一页 | 没密钥的平台记失败，旧数据保留 |
| `read_topic_feed` | 只读本机 AIHOT。不启动采集 | 服务没开时返回说明 |
| `generate_scripts` | 调用 `pipeline.generate_script_session` 写三版并入库。`chosen` 保持空 | 拒绝，并说明缺密钥 |
| `list_scripts` / `read_script` | 脚本库和同选题历史。读取不会选用 | 可用 |
| `rewrite_script` | 调用 `script_edit.rewrite`，只返回预览 | 拒绝 |
| `save_script_revision` | 另存一版。保存后选用状态清空 | 可用 |
| `choose_script_version` | 记下用户指定的版本 | 可用，但必须有确认语 |
| `search_materials` | 按名称、地点、标签搜索本地素材，和页面搜索一致 | 可用 |
| `produce_rough_cut` | 用已选用的版本跑 `pipeline.py`。不再次写脚本 | 配音可退回系统语音；未选用则拒绝 |
| `write_jianying_draft` | 调用 `pipeline.redraft` 写回剪映工程 | 没有本机粗剪时拒绝 |
| `export_roughcut` | 调用 `roughcut_export.render` | 没有本机粗剪时拒绝 |
| `review_finished_video` | 调用 `video_review.review_video` | 拒绝。评测也不等于终审 |
| `read_review` | 默认读 `data/review/review.json`。`refresh` 才重算 | 默认可读。只有 `use_ai` 才要密钥 |
| `read_production_result` | 最近一次粗剪摘要。演示数据带 `preview_only` | 可用 |
| `record_final_review` | 把终审意见写到 `output/final-review.json` | 可用，但必须有确认语 |

确认语必须和用户原话一致，工具不会接受同义改写：

| 动作 | 确认语 |
| --- | --- |
| 选用脚本 | 确认选用此版 |
| 开始制作 | 确认开始制作 |
| 写入剪映 | 确认写入剪映 |
| 导出粗剪 | 确认导出粗剪 |
| 记录终审 | 确认终审 |

没有发布工具。`record_final_review` 固定返回 `publishes: false` 和 `page_publish_unlocked: false`。工作台页面上的发布登记锁在浏览器 `localStorage` 里，这个文件解锁不了它。终审人仍要在页面上登记；通过之后由运营去平台发布。

制作可能超过一分钟。补丁把 `toolCallTimeoutMs` 设成 600000。MCP 自己的说明里写了同一套门槛，技能里写了选题、三段式脚本和事实核查。

## TikHub（TikTok 热点）

TikTok 只用 [TikHub](https://docs.tikhub.io) 的 App V3：`GET /api/v1/tiktok/app/v3/fetch_video_search_result`。2026-11-14 用真实密钥实测，`keyword=train travel`、`count=5`、`offset=0`、`sort_type=1`、`publish_time=7`、`region=GB` 返回 code 200 并计费，视频在 `data.search_item_list[].aweme_info`。同一次实测里 Web 接口 `GET /api/v1/tiktok/web/fetch_search_video?keyword=train travel&count=3&offset=0` 返回 400 且未计费，因此不调用，也不作为备用。

密钥只读环境变量 `TIKHUB_API_KEY`，Bearer 认证，不写进仓库。可以放在已忽略的 `config.env` 里。

```bash
# config.env
TIKHUB_API_KEY=你的令牌
```

有这把密钥时，`scripts/start.sh` 会做四件事，然后才启动工作台：

1. 用 openssl 在 `.run/` 生成本机证书（已有且仍签给 `127.0.0.1` 和 `localhost` 就复用）。私钥是 `.run/tikhub-local.key`，权限 600。
2. 把证书追加到 `.run/ca-bundle.pem`。工作台和适配层都用这份包做 `SSL_CERT_FILE`，所以页面请求 `https://127.0.0.1` 时能通过校验，访问 TikHub 的正式证书也不受影响。启动前如果已经自己设置了 `SSL_CERT_FILE`，脚本不会替换它，需要把 `.run/tikhub-local.crt` 追加进那份证书包。
3. 生成随机本地令牌 `.run/tikhub-local.token`（权限 600），并导出为工作台的 `VIDEO_TRENDS_TIKTOK_TOKEN`。适配层要求 `Authorization: Bearer` 与它对上，否则返回 401，避免同机其他进程拿着你的密钥刷额度。
4. 若你还没有自己设置 `VIDEO_TRENDS_TIKTOK_FEED_URL`，就把它设为 `https://127.0.0.1:8781/videos`（端口可用 `TIKHUB_ADAPTER_PORT` 改）。已经设置过的地址不会被覆盖。

这三个 `.run/` 文件都已被 `.gitignore` 忽略，不要提交，也不要拷到别的机器当正式证书用。

### 在选题页采集

启动后打开 [http://127.0.0.1:18770/#topics](http://127.0.0.1:18770/#topics)。在「今日灵感」里：

1. 平台选 **TikTok**（不要选「全部平台」，除非你也想同时花 YouTube 的额度）。
2. 填写搜索关键词，例如 `train travel`。地区按目标市场选，默认英国。
3. 采集范围选 **近 24 小时** 或 **近 7 天**。页面上还有「近 30 天」，同样只会发 1 次请求。
4. 点 **采集视频**。

工作台会向适配层追加 `q`、`region`、`days` 和 `limit=25`。适配层仍只调用上面的 App V3，参数是 `sort_type=1`、`region`、`publish_time` 等于所选天数，`count=20`，`offset=0`。不翻页，所以点一次只产生 **1 次 TikHub 请求**。TikHub 按请求计费；刷新列表不会再请求。工作台自己还有 30 秒冷却，连点会被拦住。

TikHub 的 `publish_time` 不可靠：实测近 24 小时的一页里，41 条有 23 条落在窗口外。适配层按每条视频的 `create_time` 丢掉窗口外的结果（也丢掉没有发布时间、或比现在超前 5 分钟以上的），再按 `play_count` 从高到低留下前 N 条。N 是环境变量 `TIKHUB_TOP_N`，默认 10，并且不会超过这次的 `limit`。缺的播放、点赞、评论、分享保持 `null`，不写成 0。页面按播放量降序展示。

MCP 的 `collect_video_trends` 仍然可以直接选 TikTok。它不经过这个 HTTPS 地址，而是在进程里调用同一个 `search`，写入工作台的 `output/video-trends.json`。Instagram 仍没有供应商适配。没有 `TIKHUB_API_KEY` 时，选题页上的 TikTok 状态会仍是「缺少数据服务地址」，因为脚本不会去设置数据地址。

## 测试

```bash
cd ~/Documents/loop/my-agent-project
export RAIL_STUDIO_ROOT=~/Documents/loop/rail-content-studio
python3 -m pip install 'pyjianyingdraft==0.3.1'
python3 -m unittest discover -s tests -v
```

测试把脚本库写到临时目录，不改工作台克隆里的 `output/`。生成和改写会替换 `llm.script_json`，不访问模型。制作步骤只检查将要执行的 `pipeline.py` 命令，不真正配音。

可选：安装 `@modelcontextprotocol/client@2.2.0` 后，

```bash
RAIL_STUDIO_ROOT=~/Documents/loop/rail-content-studio \
RAIL_PROJECT_ROOT=~/Documents/loop/my-agent-project \
node scripts/probe-mcp.mjs
PROBE_MODE=auto node scripts/probe-mcp.mjs
```

`auto` 会走 Harness 这版客户端的 2026-07-28 探测。

## 这次实际验证了什么

在这台 Linux 虚拟机上：

- 单元测试 8 项通过。工作台是 `xiaofanma666/rail-content-studio` 的 `main` 浅克隆。
- `@modelcontextprotocol/client@2.2.0` 在 legacy 和 `auto` 两种协商下都列出了上面的 18 个工具。
- `npm` 上的 `@deepseek-ai/dsh@0.2.2-rc.3` 配 Node 24.22.3：`dsh --profile web --patch harness/rail-studio.cordis.yml --dump-config` 能合入补丁，并把右侧浏览器的 `disabled` 设为 `false`。
- 同一个命令去掉 `--dump-config`、加上 `--no-open` 后，Web 服务起来了，子进程是 `python3 -m mcp_server`，启动日志里没有 MCP 报错。
- 页面启动数据里有 `@local/rail-studio-sidebar`，取到的 `client.js` 含有 `openTab`、`browser` 和 `http://127.0.0.1:18770`。
- 单独把工作台跑在 18770 时，首页返回 200 和中文 HTML。

没有在图形界面里点开右侧 iframe，所以没有截图证明「左聊天、右工作台」的最终像素。也没有在 macOS 上跑通配音、`afconvert` 或剪映打开。成片评测没有用真实视频和付费模型打过。

## 已知限制

- 选用和终审靠确认语拦住自动调用。模型如果编造这句原话，工具无法分辨是不是用户说的。技能要求它不要代填。页面上的终审和发布登记仍然要人在工作台里做。
- 工作台和 MCP 共用工作台目录下的 `output/`。不要两边同时改同一份脚本。`RAIL_OUTPUT_DIR` 只给测试隔离脚本库，正式制作的配音和 `result.json` 仍由工作台写到它自己的 `output/`。
- 流水线在 Linux 上不能完成 macOS 语音转换。虚拟机里只验证了命令拼装和拒绝演示数据。
- 复盘读的是仓库里的模拟 CSV，不是平台实时数据，也不会自动改脚本。
- 今日灵感是仓库里已经发布的 JSON。读取和刷新页面都不会重新付费采集。
- TikHub 按请求计费。选题页每点一次「采集视频」只发 1 个 App V3 请求，不翻页，也不自动定时。刷新列表不请求。
- 环境变量或 `config.env` 里如果已经写了 `VIDEO_TRENDS_TIKTOK_FEED_URL`，启动脚本不会改成它自己的本地地址。想用本机适配层时，删掉这个变量后再启动。
- 没配 `TIKHUB_API_KEY` 时，页面上的 TikTok 状态仍是「缺少数据服务地址」。
- 右侧浏览器的沙箱可能拦截下载，也可能拦不住页面自己的后续跳转。这是 Harness 浏览器插件自己的限制。
- 改工作台端口后，要同时改客户端插件里的默认地址。
- Harness 还在开发者预览。补丁按 `0.2.2-rc.3` 的 `@deepseek-ai/dsh-mcp-client` 字段写的，以后的版本可能要改。
