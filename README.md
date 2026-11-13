# 专利法律状态监控系统

面向知识产权代理机构的本地专利法律状态监控原型: 定时采集客户几千件专利的
法律状态与年费记录, 自动比对差异、计算年费期限与宽限期、到期提醒, 并提供
网页控制台。采集目标是一个**自带的本地 mock 专利查询站**, 模拟真实站点的
登录、页面 JS 签名和 IP 限流封禁。

## 技术栈

- Python 3 + Playwright(真实 Chromium 执行页面 JS、复用登录态)
- SQLite(WAL, 单文件, 零外部依赖)
- Flask(mock 站 + Web 控制台)
- openpyxl(Excel 导入/导出)
- pytest(单元测试 + 对 mock 站的浏览器端到端测试)

## 目录结构

```
common/checksum.py        申请号归一化 + CNIPA 校验位(新/旧式, mock 与监控共用)
mock_site/                本地 mock 专利查询站
  app.py                  登录态、JS 签名校验、IP 限流/封禁/自动解封、管理端变异接口
  seed.py                 确定性 mock 专利库(40 件)
  templates/ static/      登录页、检索页、页面签名 sign.js
patent_monitor/
  config.py               全局配置(可被 settings 表覆盖)
  db.py                   SQLite schema、连接、设置读写
  importer.py             CSV/XLSX 导入、申请号校验拦截、任务联动、筛选查询
  collector.py            Playwright 采集器 + 测试用 StubCollector
  monitor.py              快照落库、字段级差异比对、年费期限/宽限期/提醒分级
  scheduler.py            分批限速、封禁等待恢复、断点续采、轮询日志
  webapp.py               Web 控制台(5 个页面 + 导入/导出/手动触发)
  templates/              案件清单/采集任务/状态变化/期限提醒/设置/案件详情
scripts/                  启动与演示数据脚本
tests/                    单元测试 + 真实浏览器端到端测试
data/                     SQLite 库、浏览器会话、演示 CSV(运行时生成)
```

## 快速开始

```bash
# 0) 依赖(本机已装可跳过); 浏览器内核
pip install flask playwright openpyxl pytest requests beautifulsoup4
python -m playwright install chromium

# 1) 生成演示案件清单 data/sample_cases.csv(含脏格式与校验不过的行)
python scripts/seed_demo.py

# 2) 终端 A —— 启动 mock 查询站 http://127.0.0.1:5051
bash scripts/run_mock.sh

# 3) 终端 B —— 启动 Web 控制台 http://127.0.0.1:5050
bash scripts/run_web.sh
```

浏览器打开 http://127.0.0.1:5050 :

1. **案件清单** → 导入 `data/sample_cases.csv`(也可自己传 CSV/XLSX)。
   校验位不通过的申请号直接标红、不进入采集、不发任何请求。
2. **采集任务** → "立即触发一轮采集"。可看到任务状态、轮询日志、断点游标、
   被封 IP 的等待与自动恢复。
3. **状态变化** → 查看法律状态、专利权人、年费缴纳等字段级新旧值差异。
4. **期限提醒** → 按 即将到期 / 滞纳期(宽限期) / 已超期 分级的待缴年费。
5. **设置** → 调整站点账号、采集节奏(批大小/请求间隔/批间暂停/重试)、
   提醒提前天数、宽限期天数、自动轮询间隔。

## 关键设计

### 申请号归一化与校验位
- 接受 `ZL` 前缀、点分/空格/全角分隔符等五花八门写法, 统一为纯申请号。
- 支持新式 13 位(1993 起)与旧式 9 位, 校验位按 CNIPA 加权算法(mod 11, X)。
- 校验不过 **不发请求**, 案件以 `valid=0` 落库并标注原因(含推算建议),
  在采集任务页单列展示; 校验通过才建采集任务。

### 采集与反爬应对
- Playwright 真实浏览器登录, cookie/storage 落盘复用; 会话过期自动重登。
- 签名 `sign(ts, appNo)` 必须在 `/search` 页面上下文由页面 JS 计算,
  Python 端不硬算; 时间戳有有效期。
- 调度器按 `请求间隔` 限速、按 `批大小` 分批、批间暂停。
- mock 站同 IP 请求过快返回 403 + `retryAfter`。调度器捕获后把任务置
  `banned_wait` 并记录解封时间, 先继续其余任务, 到点自动回扫补采。
- 普通错误按指数/固定退避重试, 超过 `max_retries` 置 `failed` 人工介入;
  查无此号(404)直接 `failed`, 不无限重试。

### 断点续采
- 每轮在 `poll_logs` 开一条记录, `cursor_pos` 持久化"下一个待处理下标",
  每处理完一件就落盘。进程在任意时刻被杀, 下次 `run_poll` 发现未完成轮次
  即从游标继续; 正常收尾才关日志, 并在下一轮开始时把 done 任务重新置 pending。

### 差异与年费
- 每次采集存全量快照, 与上一次快照做字段级 diff(法律状态、状态日期、
  专利权人、授权日、名称、类型、每年年费的新增/消失/缴纳状态/金额)。
- 年费应缴日优先取接口返回值, 缺失时按申请日对应日推算(含 2/29 兜底);
  宽限期 = 应缴日 + `grace_days`(默认 180 天)。
- 提醒分级: `due_soon`(提醒窗口内未到期) / `grace`(过应缴日仍在宽限期) /
  `overdue`(超宽限期未缴)。

### 筛选与导出
- 案件清单支持按 客户 / 法律状态 / 提醒级别 / 关键词 筛选与分页,
  一键导出当前筛选结果为 Excel。

## mock 站管理端(测试/演示用)

需带请求头 `X-Admin-Token: admin-token`:

| 接口 | 作用 |
| --- | --- |
| `POST /admin/mutate` | 改法律状态/专利权人, 或把某年费改为未缴 |
| `POST /admin/add-fee-year` | 新增一个年费年度(模拟时间推进) |
| `GET  /admin/list` | 列出全部申请号 |
| `GET  /admin/bans` | 查看各 IP 封禁次数与解封时间 |
| `POST /admin/reset` | 重置 mock 库与限流状态 |

限流参数可用环境变量调小: `MOCK_RATE_WINDOW`、`MOCK_RATE_MAX`、
`MOCK_BAN_SECONDS`、`MOCK_PORT`。

## 测试

```bash
# 全部(含真实浏览器端到端, 约 30s)
python -m pytest tests/ -q

# 仅单元测试(虚拟时钟, 秒级完成, 不启浏览器)
python -m pytest tests/test_checksum.py tests/test_business.py tests/test_scheduler.py -q
```

覆盖: 校验位(正常/脏格式/错校验位/缺校验位/旧式/X 位)、导入去重与拦截、
差异比对、年费期限与提醒分级、调度器(全量成功/查无此号/封禁等待自动恢复/
失败重试/中断后断点续采/多轮重开)、以及对 mock 站的真实 Playwright 流程
(登录、JS 签名、限流封禁与自动解封、管理端变异后差异落库)。

## 对接真实系统时的替换点

- `mock_site` 换成真实专利检索入口, `collector.py` 里登录选择器与签名
  调用按真实站点调整; 签名仍建议在页面上下文 evaluate。
- `seed.py` 的字段结构对齐真实接口; `monitor.py` 的 diff 字段与年费规则
  按实务调整(如费用减缓、年费滞纳金阶梯)。
- 多代理/多 IP 出口可在 `PlaywrightCollector` 注入不同 proxy 以分散限流。
