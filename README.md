# CICC 全球周度新闻摘要 Agent V2

面向沙特阿拉伯、阿联酋、哈萨克斯坦和韩国的内部新闻检索与双语周报工具。

V2 在原版基础上重点改进了信源完整性、失效源维护、抓取速度、Vercel 无状态运行和公网访问保护。旧版仓库保持不变。

## 核心流程

1. 从官方机构、交易所、主权基金、重点公司、权威媒体、中文出海媒体和客户 Watch List 检索最近 7 天新闻。
2. 标题去重并执行 Hard Exclude、国家路由、国家准入门槛、排序与主题组合四层筛选。
3. 同事在网页中复核 FINAL / RESERVE / HOLD 候选并勾选。
4. LLM 先生成英文权威稿，再忠实翻译中文，并执行金额事实与双语一致性检查。
5. 将结果写入中英文 Word 模板并下载 ZIP。

## V2 变化

- 信源从“媒体列表”升级为 Tier 1/2/3 分层注册表。
- 清除或替换 8 个已经失效的直连 RSS。
- 新增针对 SPA、PIF、CMA、Saudi Exchange、ADQ、Mubadala、ADX、DFM、AIFC、AIX、KASE、Kazakh Invest、FSC、KRX、Invest Korea 等一手机构的定向发现策略。
- 四个地区均启用 Watch List 定向查询。
- Watch List 每 4 家公司组合查询，请求数显著低于逐家公司查询。
- 新闻抓取改为受控并发，并支持每个来源独立设置结果上限。
- 排序加入来源层级权重。
- DOCX 生成改为浏览器编排的无状态 API，不依赖 Serverless 进程内任务缓存。
- 可通过 `APP_ACCESS_TOKEN` 保护所有业务 API。
- 手动 URL 和正文抓取拒绝内网、回环和链路本地地址，并逐跳检查重定向。
- 增加信源健康检查脚本和部署说明。

详细设计见 [V2 信源策略](docs/source_strategy_v2.md) 和 [Vercel 部署说明](docs/vercel_deployment.md)。

## 本地启动

Windows 下双击：

```text
start-agent.bat
```

或使用终端：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env，填入 OPENROUTER_API_KEY
.\.venv\Scripts\python.exe run.py
```

打开 `http://127.0.0.1:8765`。

## 环境变量

```dotenv
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=deepseek/deepseek-v4-pro
LLM_TIMEOUT=90
LLM_CONCURRENCY=3
FETCH_CONCURRENCY=8
APP_ACCESS_TOKEN=
HOST=127.0.0.1
PORT=8765
MAX_PER_SOURCE=30
LOOKBACK_DAYS=7
```

本地仅自己使用时可以不设置 `APP_ACCESS_TOKEN`。部署到公网时应设置一个足够长的随机值。

## 信源健康检查

```powershell
.\.venv\Scripts\python.exe scripts\audit_sources.py
.\.venv\Scripts\python.exe scripts\audit_sources.py --country KZ --json
```

配置位于：

- `config/sources.yaml`：信源、层级、类别、单源结果上限
- `config/watchlist.yaml`：客户与重点机构名单
- `config/filtering.yaml`：四层筛选规则
- `config/source_aliases.yaml`：媒体名称归一化和主页

当前注册表包含 80 个检索策略，其中 3 个是已验证的直接 RSS，其余使用 Google News 对指定媒体、机构网站或 Watch List 进行定向发现。Google News 是传输与发现渠道，候选条目的 `source` 才是实际发布媒体。页面支持按需将 Google News 跳转链接解析成媒体原文链接。

## Vercel

仓库已包含 `vercel.json`。在 Vercel 导入仓库后：

1. 配置 `OPENROUTER_API_KEY`、`APP_ACCESS_TOKEN` 等环境变量。
2. 确认 Fluid Compute 已启用。
3. 部署完成后，在页面运行配置中输入同一访问口令。

V2 不再使用 V1 的 `/api/generate-job` 内存任务方式。每条新闻独立格式化，最后单独渲染 Word，因此更适合无状态 Serverless。

注意：Vercel 官方将 Hobby 限定为个人、非商业用途。正式公司内部使用前，请自行确认账户方案与使用条款。

## 项目结构

```text
app/                  FastAPI、抓取、筛选、LLM、Word 渲染
api/index.py          Vercel Python 入口
config/               信源、筛选和 Watch List 配置
docs/                 业务规则、信源策略、部署说明
scripts/              信源健康检查
static/               单页前端
templates/            中英文 Word 模板
tests/                无网络单元测试
```

## 安全提示

- 不要提交 `.env`。
- 不要在公开部署中留空 `APP_ACCESS_TOKEN`。
- 页面填写的访问口令和 OpenRouter Key 保存在当前浏览器 localStorage；不要在公共电脑使用。
- 新闻和 LLM 生成内容必须经过人工复核后再对外使用。
