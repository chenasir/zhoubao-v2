# V2 新闻源策略

## 目标

信源数量不是唯一目标。V2 优先保证：一手信息可追溯、交易新闻覆盖完整、与中国及中金业务相关、失效源可被发现、抓取能在 Vercel 请求时限内完成。

当前 80 项配置是“检索策略”，不是 80 个直连订阅：其中 3 项为已验证的直接 RSS，其余通过 Google News 定向检索指定媒体、机构域名或 Watch List。Google News 仅承担发现与传输，实际媒体名称取自 RSS 条目的 `source` 元数据。页面会明确显示二者，并支持按需解析媒体原文链接。

## 分层

- Tier 1：政府、监管机构、交易所、主权基金、投资促进机构和重点公司官网。
- Tier 2：Reuters、Bloomberg、Zawya、KED Global、The National 等权威媒体。
- Tier 3：中文出海媒体和宽口径 Google News 发现查询。
- Watch List：按客户清单批量检索。V2 将每 4 家公司组合成一个查询，避免 V1 每家公司单独请求造成超时。

筛选排序会读取 `source_tier`。Tier 1 的来源质量权重最高，但最终仍必须通过交易硬度、金额或规模、国家路由和主题配额规则。

## 地区重点

### 沙特阿拉伯

重点覆盖 SPA、PIF、MISA、CMA、Saudi Exchange、Saudi Aramco、ACWA Power，以及 Zawya、Reuters、Bloomberg、MEED、Arab News。另设中国驻沙特使馆、商务部驻沙特机构、中文出海媒体和客户清单查询。

### 阿联酋

重点覆盖 WAM、Dubai/Abu Dhabi Media Office、ADQ、Mubadala、ADX、DFM、DIFC、ADGM、DP World、AD Ports，以及主要区域商业媒体。查询同时覆盖 Abu Dhabi、Dubai 和 UAE 三种地理表达。

### 哈萨克斯坦

重点覆盖 Qazinform、AIFC、AIX、KASE、Kazakh Invest、政府和总统府、Samruk-Kazyna、KazMunayGas，以及 Astana Times、Interfax、Trend、Reuters 和 Bloomberg。

### 韩国

重点覆盖 FSC、KRX、Invest Korea、MOTIR、KFTC，以及 KED Global、Chosun Biz、JoongAng、Pulse、Business Korea、Seoul Economic Daily、Korea Herald、Yonhap、Reuters 和 Bloomberg。

## 健康检查

运行：

```powershell
python scripts/audit_sources.py
python scripts/audit_sources.py --country KZ --json
```

直连 RSS 返回非 2xx 或无法解析出条目时，脚本以失败退出。Google News 查询返回 0 条不一定代表失效，也可能只是最近 7 天无匹配结果，需要结合多周结果判断。

## 维护原则

1. 每月运行一次健康检查。
2. 失效 RSS 不应长期留在启用列表；优先寻找官方替代，否则改为 `site:` 查询。
3. 新增来源时填写 `tier`、`category` 和 `max_items`。
4. 避免多个仅措辞不同但结果高度重合的宽口径查询。
5. 对连续四周无结果的来源进行人工复核，而不是自动删除。
