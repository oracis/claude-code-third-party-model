# Claude Code 接入第三方模型

**Connect Claude Code to any third-party model — native Anthropic endpoints, or OpenAI-only ones via a zero-dependency local gateway.**

把 Claude Code 接到任意第三方模型上：DeepSeek / Kimi / GLM 这类有 Anthropic 兼容端点的直接改配置；
只有 OpenAI 兼容端点的模型（如 OpenCode Zen 免费层、OpenRouter 上的 stealth 模型）需要一层本地
协议转换网关。本仓库同时提供**协议转换网关**、**一句话切换器（含删除）**，以及把这条路上所有坑
讲清楚的排查手册。

> 本仓库本身是一份 **AI Skill**（`SKILL.md`）。用 WorkBuddy / Claude Code 等 agent 时，
> 把 `SKILL.md` 装进 skills 目录，agent 就能自己照着做诊断和配置。

---

## 两条路线

| 你的模型 | 做法 | 需要网关吗 |
|---|---|---|
| **有 Anthropic 兼容端点**<br>DeepSeek `/anthropic`、Kimi、GLM 等 | 直接把 `ANTHROPIC_BASE_URL` 指过去 | ❌ 不需要 |
| **只有 OpenAI 兼容端点**<br>OpenRouter 上的模型、OpenCode Zen 免费层、各类 stealth 模型 | 本地起 `ccproxy.py` 做 Anthropic ↔ OpenAI 翻译 | ✅ 必须 |

关键判断：**看模型提供方给的是不是 Anthropic Messages 协议**。给 OpenAI 格式的
（`/v1/chat/completions`）就必须转换——Claude Code 不会说 OpenAI 那套。

---

## 快速开始

### 路线一：原生 Anthropic 端点（以 DeepSeek 为例）

```bash
# ~/.claude/settings.json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "sk-你的key",
    "ANTHROPIC_MODEL": "deepseek-flash[1m]"
  },
  "model": "deepseek-flash[1m]"
}
```

改完**重启 `claude`**（已开的会话仍持旧配置）。

### 路线二：只有 OpenAI 端点（需要网关）

```bash
# 1) 起网关（零依赖，Python 3 标准库，无需 pip install）
#    以 OpenCode Zen 免费层为例：它只接受 opencode 客户端/SDK 发出的请求，
#    所以先起 scripts/zen_bridge.py（内部转发到 opencode serve），网关再指向桥接。
python scripts/zen_bridge.py --port 8787 --sdk-port 41234 --auto-start
python scripts/ccproxy.py --port 3457 --upstream http://127.0.0.1:8787/v1/chat/completions --model deepseek-v4.1-flash-free
#    首次启动会生成一个客户端令牌，存进 ~/.claude-code-proxy.json

# 2) 另开一个终端，带令牌确认网关活着（不带令牌会返回 401）
TOKEN=$(python -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude-code-proxy.json')))['client_token'])")
curl -s -H "x-api-key: $TOKEN" http://127.0.0.1:3457/health

# 3) 把 Claude Code 指到网关
#    ~/.claude/settings.json
#    "ANTHROPIC_BASE_URL": "http://127.0.0.1:3457"
#    "ANTHROPIC_AUTH_TOKEN": "<上面那个 client_token>"   <-- 真 key 在网关配置里，令牌由 ccswitch 自动同步
```

配置也可以落盘到 `~/.claude-code-proxy.json`，之后直接 `python scripts/ccproxy.py` 就用它：

```json
{
  "host": "127.0.0.1",
  "port": 3457,
  "upstream": {
    "url": "http://127.0.0.1:8787/v1/chat/completions",
    "model": "deepseek-v4.1-flash-free",
    "api_key": ""
  },
  "drop_reasoning": true,
  "timeout": 900
}
```

> OpenCode Zen 免费层（`deepseek-v4.1-flash-free`）**不能直连**：匿名直连会被
> `FreeTierError: only used from within OpenCode` 拒。`http://127.0.0.1:8787` 是
> 本仓库 `zen_bridge.py` 的本地端口，它经 opencode SDK 转发到 Zen。其它普通
> OpenAI 上游（OpenRouter 等）直接写真实端点即可，不需要桥接。
> 已下线的 `space-bunny-free`（2026-10-16 起 Zen 不再提供）请不要再用了。

---

## 核心组件

### `scripts/ccproxy.py` — Anthropic ↔ OpenAI 协议转换网关

约 600 行，**纯 Python 标准库**，零第三方依赖。它做的事：

```
Claude Code ──Anthropic Messages──▶ ccproxy ──OpenAI /chat/completions──▶ 上游
                (流式 SSE)             │                                    (Zen / OpenRouter / 任意)
                                       └── 翻译回来，事件序列严格合规
```

设计上几个刻意的选择：

| 选择 | 原因 |
|---|---|
| 用 `http.client` 而非 `requests`/`axios` | **天然无视 `HTTP_PROXY`**，从根上消除"网关被系统代理劫持"这类问题 |
| 事件序列严格按规范生成 | `message_start`×1 → blocks → `message_delta` → `message_stop`×1 |
| **异常也补发 `message_stop`** | 上游中途断流/报错时不裸断——这是 `stream was malformed` 的根治手段 |
| 多个 `tool_call` 的增量不提前关块 | 上游参数是**交错**到达的；提前关会把后续增量写进已关闭的块 |
| 丢弃 `reasoning_content` | Anthropic 的 thinking block 需要 `signature`，伪造容易炸 |

CLI 参数优先于配置文件：

```bash
python scripts/ccproxy.py --port 3457 --verbose                       # 前台带日志
python scripts/ccproxy.py --upstream <url> --model <name> --key <k>   # 临时换上游
curl -s -H "x-api-key: <client_token>" http://127.0.0.1:3457/health   # {"ok":true,"service":"ccproxy","upstream":...}
```

### `scripts/ccswitch.py` — 一句话切换模型

profile 即模板：`~/.claude/profiles/<名字>.json` 存一份完整的 `settings.json`，
切换 = 复制覆盖 `~/.claude/settings.json`，并自动协调网关启停。

```bash
python scripts/ccswitch.py deepseek      # 切到 DeepSeek 原生端点（自动停掉不再需要的网关）
python scripts/ccswitch.py zen-ds-flash  # 切到需要网关的模型（自动把网关/桥接拉起来）
python scripts/ccswitch.py status        # 当前 profile + 网关状态
python scripts/ccswitch.py list          # 列出所有 profile
python scripts/ccswitch.py gateway       # 只确保网关在跑，不切换
```

别名：`ds` → deepseek，`zen` / `opencode-zen` / `zen-ds` / `ds-flash` → zen-ds-flash。

**模型过期/失效了？删掉就行**（免费模型说没就没，不用改代码）：

```bash
python scripts/ccswitch.py delete <profile>                 # 交互确认后删除
python scripts/ccswitch.py delete <profile> --yes           # 跳过确认
python scripts/ccswitch.py delete <profile> --permanent     # 连付费模型也真删
```

- **免费模型（profile 带 `x_free`）是真删**——`os.remove` 直接干掉，不留 backup
  （免费模型要么不好用、要么已失效，没有保留价值，想重建随时加回来）。
- **付费模型**（用自己的 key）才归档：移入 `~/.claude/backups/deleted/`，可拷回恢复。
- `ccswitch list` 带 `[free]` / `[paid]` 标记；切换器 GUI 底部的「删除所选模型」
  下拉显示「显示名 [profile名]」，免费模型确认弹窗会明示「永久删除、不留 backup」。

要不要网关**不靠额外元数据判断**，而是从配置自己推：`ANTHROPIC_BASE_URL` 指向
`127.0.0.1:<网关端口>` 就需要，指向别的（如 `api.deepseek.com/anthropic`）就不需要。

`examples/profiles/` 下有可直接用的模板，拷到 `~/.claude/profiles/` 即可：

```bash
cp examples/profiles/*.json ~/.claude/profiles/
# 然后编辑 deepseek.json 填上你自己的 key
```

### 顺带：把同一个模型配进 WorkBuddy 客户端

除了 Claude Code，WorkBuddy 也可以在「配置自定义模型」里直接选第三方模型，落盘在
`~/.workbuddy/models.json`：

```json
{
  "id": "deepseek-v4.1-flash-free",
  "name": "DeepSeek V4.1 Flash (Zen 免费)",
  "vendor": "OpenCode Zen",
  "url": "http://127.0.0.1:8787/v1/chat/completions",
  "apiKey": " ",
  "supportsToolCall": true,
  "supportsReasoning": true
}
```

> ⚠️ **`apiKey` 不能留空字符串**。为空时客户端会回落到自己的平台 token，当成
> `Authorization: Bearer <平台 token>` 发给上游，匿名端点直接回 `401 Invalid API key.`
> （错误码 3001、`custom_model_auth`），而报错文案会误导你去检查「API Key、模型 ID、接口地址」。
> 修法是填**一个 ASCII 空格**——匿名端点先 trim 再判空，照常放行。
> 注意必须是 ASCII 空格/Tab，`\xa0` 之类的全角空白无效。细节见 `SKILL.md`。

---

## 为什么不用 claude-code-router

`claude-code-router` 是接第三方模型最常用的工具，但 **2.1.1 的流式转换是坏的**——它会把
Anthropic 事件**整个写两遍**，而且**从不发送 `message_stop`**，导致 Claude Code 报：

```
API Error: The response stream was malformed. The response above may be incomplete.
```

这不是"时不时不稳定"，而是**必现**。定位过程记录在 `SKILL.md` 里，方法本身可复用：

1. 抓经网关的 SSE，统计事件序列 → 发现全部重复、无 `message_stop`；
2. 直连上游抓原始流 → 上游完全正常，排除上游；
3. **决定性对照实验**：架一个返回"教科书式标准 OpenAI SSE"的假上游，把网关指过去
   → 照样畸形。而假上游日志显示只收到 **1 次**请求，证明那两遍输出是网关自己写的。

→ 结论：中间层的锅。npm 上只有 2.1.0 / 2.1.1，没有修复版，2MB 压缩产物也不好打补丁，
所以直接换成本仓库的 `ccproxy.py`。

---

## 三个静默故障（本仓库最值钱的部分）

这三个坑的共同点是：**报错信息完全指向错误的方向**，靠读报错永远查不出来。

### ① 系统代理劫持本地网关

`axios` / `requests` 会默认读取 `http_proxy` / `https_proxy` 环境变量。如果系统设了代理，
网关转发上游的请求会**被代理吞掉**，症状极具迷惑性：

| 现象 | 真相 |
|---|---|
| 打上游得到 **400**，但 `curl --noproxy` 直连是 **200** | 代理层引入的假错误 |
| 打本地 `127.0.0.1:<port>` 得到 **502** | 连回环地址都被代理了 |

**定位手法**：架一个本地回显服务器（记录收到的请求头与请求体），把网关指过去。
如果回显服务器收不到请求，就证明网关走了代理。这手法对所有"本地网关转上游"的场景通用。

**修法**：网关侧用不读代理的 HTTP 客户端（`ccproxy.py` 用 `http.client`，天然免疫）；
启动时清掉代理环境变量并设 `NO_PROXY=*`（`ccswitch.py` 已内置）。Claude Code 侧若在某些
IDE 里单独调 `claude`，也要在那一侧设 `NO_PROXY=127.0.0.1,localhost`。

### ② 配置文件名的静默陷阱

`claude-code-router` 读的是 **`config-router.json`**，不是 `config.json`。文件名写错时它
**不报错**，而是静默加载内置默认配置——表现为"配置写了就是不生效"。识别方法：

```bash
ccr health   # 若出现你不认识的 provider 名（如 codewhisperer-primary），就是没读到你的配置
```

源码里写死：`DEFAULT_CONFIG_PATH = path.join(homedir(), ".claude-code-router", "config-router.json")`。

### ③ 模型名写对了却不生效

Claude Code 有**多套**模型配置（`ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、
`ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、
`CLAUDE_CODE_SUBAGENT_MODEL`…），只改一个的话子代理/后台任务仍走旧模型。
判定优先级：**环境变量 > `settings.json` > `~/.claude.json` > 内置默认**。
还有 Windows 上的注册表覆盖源要一并排查。

---

## 目录结构

```
.
├── SKILL.md                        # AI Skill 定义（诊断手册 + 配置范例，agent 直接读）
├── README.md                       # 本文件
├── LICENSE                         # MIT
├── scripts/
│   ├── ccproxy.py                  # Anthropic ↔ OpenAI 协议转换网关（零依赖）
│   ├── ccswitch.py                 # profile 一键切换 / 删除 + 网关启停协调
│   └── zen_bridge.py               # Zen 免费模型专用桥接（→ opencode serve → Zen）
└── examples/
    └── profiles/
        └── deepseek.json           # 原生 Anthropic 端点模板（key 留空，需自己填）
```

---

## 实测验证

流式响应的事件序列检查（这是判断网关好坏的硬指标）：

| 检查项 | 期望 | 实测 |
|---|---|---|
| `message_start` / `message_delta` / `message_stop` 计数 | 各 1 | ✅ |
| `content_block_stop` 计数 | = `content_block_start` 计数 | ✅ |
| 首 / 末事件 | `message_start` / `message_stop` | ✅ |
| 文本拼接 | 等于完整回答 | ✅ |
| 带 `tools` 时 | 出现 `tool_use` 块，`args` 能 `json.loads` 成功 | ✅ `{"city": "北京"}` |
| 真实 Claude Code | `claude -p "7*8=?"` → `56`、`is_error:false` | ✅ |

自测脚本（直接跑，看事件计数）：

```bash
curl -sN http://127.0.0.1:3457/v1/messages \
  -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"x","max_tokens":300,"stream":true,"messages":[{"role":"user","content":"回答两个字:你好"}]}'
```

---

## 环境与限制

- **平台**：Windows / macOS / Linux 均可。`ccswitch.py` 的网关进程管理做了跨平台分支
  （Windows 走 `netstat` + `taskkill`，POSIX 走 pidfile + `SIGTERM`）。
- **Python**：3.8+，只用标准库，**不需要 `pip install` 任何东西**。
- **强制推理模型**：像 Zen 免费层这类模型会把推理 token 算进 `max_tokens`，
  给太小会出现"空回复"（内容被推理吃光）。给 1000+ 比较稳。
- **匿名 / stealth 模型**：规格与数据政策通常未公开，提供方**可能保留 prompt**。
  别用于敏感或强合规场景。
- **免费通道有时效**：公共免费上游随时可能关停或限流。换上游只需改配置里的 `url` + `model`。

## License

MIT © oracis

---

<sub>关键词：claude code 第三方模型 · ANTHROPIC_BASE_URL 配置 · claude code 换模型不生效 ·
The response stream was malformed · claude-code-router 流式畸形 · Anthropic to OpenAI 协议转换 ·
DeepSeek / Kimi / GLM / OpenRouter 接入 Claude Code · 多 profile 一键切换 ·
本地网关被系统代理劫持 · unrecognized_model 警告</sub>
