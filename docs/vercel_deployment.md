# Vercel 部署说明

## V2 为什么改为无状态生成

V1 把后台任务状态和生成后的 ZIP 保存在 Python 进程内存中。Serverless 实例可能在请求间切换或回收，因此状态查询和下载不一定命中同一实例。

V2 由浏览器编排：

1. 对每条选中新闻调用 `/api/format-item`。
2. 浏览器保留返回的双语结构化内容。
3. 全部完成后调用 `/api/render`，一次性生成并下载 ZIP。

服务器不再依赖跨请求内存状态。

## Vercel 设置

建议在 Project Settings 中配置：

- `OPENROUTER_API_KEY`
- `LLM_MODEL`
- `APP_ACCESS_TOKEN`：至少 24 位随机字符串，防止公开链接被他人调用。
- `FETCH_CONCURRENCY=8`
- `LLM_CONCURRENCY=2` 或 `3`

并确认 Fluid Compute 已启用。`vercel.json` 将 Python Function 的 `maxDuration` 设为 300 秒，并部署到新加坡区域。

## 使用边界

- Vercel Hobby 的官方规则限定为个人、非商业用途。若该工具属于公司的正式内部工作流，应由团队自行确认账户方案是否符合 Vercel 条款。
- Hobby 用量适合低频的 2-3 人试用，但不能保证第三方新闻网站和 Google News 永不限流。
- 不要把 `.env`、访问口令或真实 OpenRouter Key 提交到 Git。
- 部署完成后只将 URL 和访问口令分享给内部使用者。
