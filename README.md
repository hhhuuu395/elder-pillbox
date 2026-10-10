# 老人智能药盒 · 最小 Demo

## 项目简介

一个面向老人服药的极简 Demo，分**子女端**和**老人端**两个视图：子女拍下药盒（Mock OCR）→
后端调 DeepSeek 把处方文字抽成结构化用药计划 → 子女在页面上核对、补全、确认生效 →
老人端按早/中/晚显示"今日药历"，大字体、大按钮，点一下即可打卡 →
超时未服的药弹出三选一（带出 / 补服 / 确实漏服），选"确实漏服"会**通知子女端**。
核心是**不猜药**：AI 拿不准的字段一律留空并要求人工补全，AI 说的不算数，人确认了才算数。

---

## 技术栈与架构

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | FastAPI 0.143.0 + uvicorn 0.54.0 | 单文件 `app.py`，同时负责托管页面和代理 AI |
| AI | DeepSeek `deepseek-chat`（OpenAI 兼容 `/chat/completions`） | 唯一出网调用，用 httpx 0.28.1 |
| 请求体校验 | pydantic 2.14.0 | 五个请求模型 |
| 前端 | 单文件 HTML + CSS + 原生 JS | 无框架、无构建、无外链、无 CDN |
| 存储 | **纯内存**（模块级 `STATE` 字典） | 无数据库，无文件持久化，重启即清空 |
| 运行环境 | Python 3.10.3 | |

**API Key 只在后端。** 前端所有请求走同源相对路径（`/api/...`），浏览器拿不到 Key；
key 只在服务端进程的环境变量里，只在 `_chat_completion()` 里被使用。

**三层防线**（这是整个设计的中心）：

```
① Prompt 约束        ② 服务端校验器           ③ 人工确认
   禁止推断              validate_items()         子女在页面上核对后
   禁止补全      →      blocking → 挡住确认  →    点"确认并生效"
   拿不准填 null         soft → 标黄提示           才算数
```

`app.py` 内部按注释分成六个区块：CONFIG / STATE / AI / VALIDATE / DOMAIN / ROUTES。

---

## 快速开始

```bash
git clone https://github.com/hhhuuu395/elder-pillbox
cd elder-pillbox

# 建虚拟环境装依赖
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows / Git Bash
# source .venv/bin/activate && pip install -r requirements.txt  # Linux / macOS

# 配置 DeepSeek Key（不配也能跑，见下面「无 Key 降级」）
export DEEPSEEK_API_KEY=sk-xxxxxxxx
# PowerShell:  $env:DEEPSEEK_API_KEY="sk-xxxxxxxx"

# 起服务
./.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

**端口随便换。** 前端不含任何硬编码的 host/port，全部是同源相对路径，
所以 `--port 9000` 之后直接开 `http://127.0.0.1:9000/` 就行，不用改任何代码。

打开地址：

| 地址 | 视图 |
|---|---|
| <http://127.0.0.1:8000/> | 子女端（默认） |
| <http://127.0.0.1:8000/?view=elder> | 老人端 |
| <http://127.0.0.1:8000/docs> | FastAPI 自动生成的接口文档 |

顶栏有链接互相切换，两个视图用 `?view=` 区分。

> **演示时开两个标签页并排**：左边子女端、右边老人端。
> 跨端效果只有并排才看得见——子女确认后老人端才刷出药；老人点"今天这顿没吃"后子女端铃铛才亮。

---

## 完整演示流程

页面底部有个折叠的「演示工具（dev）」，演示超时和三选一要靠它。

### 主链路（下面这条是实际走通的路径）

| # | 点哪里 | 看什么 |
|---|---|---|
| 1 | 子女端 → **📷 拍照 / 选择图片** | 先出「识别到的药盒文字」（固定处方原文），再出 AI 抽取的可编辑表单 |
| 2 | 看表单里的**布洛芬缓释胶囊**那一行 | 「每天几次」停在"请选择…"，**红框红字**提示 `原文是「遵医嘱」，无法确定每天吃几次，请手动选择`。AI 返回的 `times_per_day` 是 `null`，`frequency_raw` 保留了原文「遵医嘱」——**没有猜** |
| 3 | 直接点 **确认并生效** | 按钮此时是灰的，旁边写「还有 1 项待补充」；硬点也会被后端挡下，返回 400 并重新高亮 |
| 4 | 把布洛芬的「每天几次」改成 **每日 2 次** | 红框消失、提示变成「补全完成，可以确认」、按钮变蓝可点 |
| 5 | 点 **确认并生效** | 绿色横幅「✅ 用药计划已生效」 |
| 6 | 切到老人端（或另一个标签页按 **🔄 刷新**） | 按早/中/晚分组，当前时段有蓝色边框 +「现在」标签，每条药显示药名 + 剂量 + 原定时间 |
| 7 | 点某条药的 **已服用** | 该条立刻变成 `✓ 已服用 HH:MM`（就是刚才打卡的时间） |
| 8 | dev 区点 **+ 120 分钟** | 时钟往前推，刷新老人端后原先待服用的药变成 `⚠ 已超时` |
| 9 | 超时项自动弹窗 | 三个动作按钮：**我带着药出门了** / **我现在补吃** / **今天这顿没吃**；弹窗必须选一个才能关 |
| 10 | 点 **✕ 今天这顿没吃** | 该条变 `✕ 已漏服`；弹窗自动弹下一条（如果还有超时的） |
| 11 | 切到子女端看右上角 | 铃铛 **🔔 漏服通知** 上出现红点计数（实测显示 4 条，条数 = 标记为漏服的次数）；点开是漏服列表，每条写着「张桂英 2026-10-10 早上 的「××」没有服用」 |

**回到干净状态**：dev 区点 **重置全部状态**。

### 异常路径 A：AI 返回的不是合法 JSON

1. dev 区把「AI 返回模式」切成 `malformed` → 再点拍照
2. 表单是空的 + 红色横幅；点开「AI 原始返回（排查用）」能看到 AI 到底回了什么
3. 点 **+ 手动添加一条药** 手工录入 → 确认生效
   （这个按钮就是为这条路径准备的：AI 一条都没解析出来时表单是空的，没有输入框就没法"人工补全"）

### 异常路径 B：AI 返回缺字段

1. dev 区切 `missing_fields` → 点拍照
2. 红框红字 = 必填缺失（剂量缺失 / 遵医嘱的频次 / 频次越界），黄框黄字 = 只提示（疗程缺失）
3. 确认按钮置灰 + 「还有 N 项待补充」，补一个少一个，补完才可点
4. 这条路径的固定返回里还有一条**原文中根本没有的"维生素C片"**，用来演示防幻觉规则（黄字提示"原文中找不到这个药名"）

### 异常路径 C：超时未服三选一

1. 先让老人端有药（走完主链路）
2. **大概率不用动时钟**：计划是按"今天"展开的，只要当前时间已经过了某个时段 + 30 分钟宽限，
   那条药一生成就是 `已超时`。下午确认的计划，早上和中午两顿直接就超时了。
   想演示"从待服用到超时"的完整过程，就先 **− 120 分钟**把时钟拨回清晨（全是 `○ 待服用`），
   再 **+120 分钟**往前推。
   ⚠️ **别一路加到跨过午夜**——过了 0 点 `/api/doses/today` 会翻到第二天，
   第二天的药全在未来，就一条超时都看不到了。拨过头按 **− 120 分钟**拨回来。
3. 三个选项的结果：

| 选 | 老人端变成 | 子女端铃铛 |
|---|---|---|
| 🚶 我带着药出门了 | `⊘ 已带出（不计漏服）`，挂起 | **不动** |
| 💊 我现在补吃 | `✓ 已服用` | 不动 |
| ✕ 今天这顿没吃 | `✕ 已漏服` | **+1**，列表里出现这条 |

4. 弹窗一次只处理一条，选完自动弹下一条；**必须选一个才能关**（刻意的，超时未服不能"知道了但没处理"）

---

## API 一览

全部 14 个端点，从 `app.py` 核对。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 托管前端：返回 `static/index.html`；文件不存在时返回 404 JSON |
| GET | `/api/health` | 健康检查：模型名、base_url、**Key 是否已配**、当前 `parse_mode`、虚拟时钟 |
| POST | `/api/ocr/scan` | **Mock 拍照**，返回写死的药盒文字（无真实图像识别） |
| POST | `/api/parse` | **主入口**：调 AI 抽取 → 落成 `draft` 状态的 plan（不生成生效计划）。body `{raw_text?}`，不传就用 mock 文字 |
| GET | `/api/plans` | 列出内存里所有 plan |
| GET | `/api/plans/{plan_id}` | 取单个 plan |
| POST | `/api/plans/{plan_id}/confirm` | **子女确认**：对人工编辑后的 items **二次校验**，通过才转 `active` 并展开今日 Dose；有 blocking 返回 **400 + issues**，已 active 返回 409 |
| GET | `/api/doses/today` | **老人端药历**：按早/中/晚分好组、标好当前时段和宽限分钟数，附 `overdue_doses`。前端不做任何时间判断 |
| POST | `/api/doses/{dose_id}/take` | **打卡"已服用"**。超时后直接打卡等同补服（`resolved_action=make_up`）；已处理过的返回 409 |
| POST | `/api/doses/{dose_id}/resolve` | **超时三选一**，body `{action}`：`carried_out` / `make_up` / `missed`。非超时状态返回 409，action 非法返回 400。选 `missed` 会写一条通知 |
| GET | `/api/notifications` | 漏服通知列表（倒序）+ `count` |
| POST | `/api/dev/advance_time` | 演示用：虚拟时钟拨快/拨慢，body `{minutes}`，**可为负** |
| POST | `/api/dev/parse_mode` | 演示用：注入 AI 异常，body `{mode}`：`normal` / `missing_fields` / `malformed` |
| POST | `/api/dev/reset` | 演示用：清空全部内存状态（`parse_mode` 会保留） |

---

## 核心设计决策

### 1. 空值协议：AI 拿不准就返回 null，并保留原文

Prompt 里明确写着「**填 null 是正确输出，不是失败**」，并列出了触发条件
（`遵医嘱` / `按说明书` / `适量` / `必要时` / `随餐` / `按需`）：这些模糊表述对应的数值字段
必须为 `null`，同时把原词原样存进 `frequency_raw` / `duration_raw`。

实测有效：Mock 处方里布洛芬写的是「遵医嘱」，真实调 DeepSeek 返回的
`times_per_day` 就是 `null`，`frequency_raw` 是 `"遵医嘱"`——**模型没有猜**。
保留原文是为了让报错信息能说人话：校验器直接把它拼成
「原文是「遵医嘱」，无法确定每天吃几次，请手动选择」，子女一眼知道该填什么。

`times_per_day` 只允许 1-4 的整数或 `null`，**频次到早/中/晚的时段映射完全在后端**
（`FREQ_TO_SLOTS`）。这层不给模型，给了它就会开始自由发挥。

### 2. 双重校验：blocking 挡确认，soft_missing 只标黄

校验器 `validate_items()` 是**纯函数**，返回 `issues[]`，每条带 `blocking` 布尔值。
六条规则：

| # | 规则 | code | 是否阻塞 |
|---|---|---|---|
| 1 | 药名为空 | `missing` | ⛔ |
| 2 | 剂量数值或单位为空 | `missing` | ⛔ |
| 3 | 频次为空（含"遵医嘱"） | `missing` | ⛔ **不猜** |
| 4 | 频次超出 1-4 | `out_of_range` | ⛔ |
| 5 | 疗程为空 | `soft_missing` | ⚠️ 只提示（长期药本来就没疗程） |
| 6 | 药名在原文里找不到 | `fabricated` | ⚠️ 只提示（疑似 AI 编造） |

**同一个校验器跑两次**：`/api/parse` 之后跑一次（校验 AI），`/api/plans/{id}/confirm`
之后再跑一次（校验人工编辑过的结果）。子女人工改也可能改错，所以确认这道关必须重新校验。

前端那个「还有 N 项待补充」的置灰按钮只是**即时反馈**；真正的闸门是后端 confirm 的
400 + issues。绕过前端直接发请求也生成不了带缺口的计划。

### 3. 数值清洗：nan / inf / 超大数一律降级为 null，绝不 500

`_as_float()` 只放行有限数。`"nan"` / `"Infinity"` / `"-inf"` / `"1e999"` 全部降级为 `None`，
400 位整数调 `float()` 抛的 `OverflowError` 也接住。

原因很具体：starlette 的 `JSONResponse` 用 `json.dumps(allow_nan=False)` 序列化，
只要 `nan` 或 `inf` 进了响应体就会抛 `ValueError`，直接变成 **HTTP 500**。
降级成 `None` 之后，校验器把它当成"剂量缺失"报 blocking，走人工补全——
**这才是设计要的行为**。

这条是回归测试守住的：`curl_checks.sh` 第 3c 节对五个脏值逐个断言"必须是 400，不能是 500"。

### 4. 无 Key 降级：不返回假数据，明确告知并给人工出口

没配 `DEEPSEEK_API_KEY` 时，`/api/parse` **不会返回编造的药品数据**，
而是产出一条 blocking issue（code `no_api_key`，文案「后端未配置 DEEPSEEK_API_KEY，
无法调用 AI，请手动录入」）。前端表单是空的，靠「+ 手动添加一条药」手工录入后确认。

> 和"模拟数据"是两回事：`missing_fields` / `malformed` 两个 dev 模式返回的才是固定 fixture，
> 它们是**独立的演示工具**，专门用来稳定复现异常路径——真实调 API 很难每次都复现出缺字段。

**dev 模式的优先级高于 Key 检查**，因为它们是纯本地模拟、不走网络。
没有 Key 也能完整演示全部异常路径。

### 5. 超时是算出来的，不是存出来的

`overdue` 从来不被写进状态，而是由 `compute_status()` 用（虚拟）当前时间对比
`计划时间 + 30 分钟宽限` **派生**出来。

好处是拨时钟就能驱动超时，不需要后台任务、不需要定时器。
副作用是**时段一生成就可能已经是超时状态**——比如上午 11 点确认的计划，
早上 08:00 那一顿（宽限到 08:30）一生成就是 `已超时`。这是真实行为，不是 bug。

---

## 测试与验证

```bash
bash curl_checks.sh        # 32 项断言
```

需要服务已经在 8000 端口跑着。覆盖：主链路（mock 拍照 → AI 解析 → 子女确认 → 药历 → 打卡）
和全部异常路径（缺字段 / 非法 JSON / 确认被拦 / 规则 1-6 / 数值清洗 / 超时三选一）。

> 脚本里只有 **28 个 `check` 调用点**，但实际跑出 32 项——第 3c 节那个在
> `for BADVAL in ...` 循环里，五个脏值各跑一次。数调用点会以为对不上，看输出的
> `PASS` 行为准。

**第 7 节已修复时间依赖 bug。** 原来固定 `+720` 分钟拨快时钟，这会跨过午夜：
`/api/doses/today` 跟着翻到第二天，而第二天的时段全在未来，一条 `overdue` 都没有——
**中午之后跑必然失败**，上午跑却正常。现在改成锚定"今天 20:00"的相对偏移
（先读当前虚拟时间，算出到 20:00 还差多少分钟再拨），任何时刻跑结果都一致：
早/中/晚都过点，睡前 21:30 还没到。

前端（`static/index.html`）没有自动化测试。它对后端只有一条硬约束——**字段名必须对得上**，
这一条在开发时用一次性脚本核过（75 项字段契约 + 34 项渲染断言），但脚本没有留在仓库里，
后续靠人工走上面的演示流程回归。

---

## 已知限制

按实际情况写，没有粉饰：

- **单用户，无登录。** 所有人共享同一份内存状态，没有"张桂英的账号"这个概念；
  患者名 `PATIENT_NAME = "张桂英"` 是写死的常量。
- **无持久化，无备份策略。** 状态全在模块级 `STATE` 字典里，**进程一重启就全没了**。
  没有数据库、不落盘、没有导出。
- **只能单进程跑。** `STATE` 是进程内的字典，用 `--workers 2` 以上每个 worker 会各存一份，
  数据互相看不见。**别加 `--workers`。**
- **时钟快进是 dev 工具，不是真实调度。** 后端没有后台任务或定时器——
  状态只在有请求进来时才重新计算一次。所以它不会主动"到点提醒"，
  老人端不刷新就看不到变化。
- **无真实推送。铃铛是拉取式的。** 没有 WebSocket、没有短信、没有微信推送，
  子女端必须手动刷新（或由打卡/resolve/确认动作触发一次自动刷新）才能看到新通知。
  前端**不做轮询**，这是刻意的。
- **通知没有已读/未读状态。** 只有总条数（`/api/notifications/read` 被明确砍掉了），
  所以铃铛上的数字是"累计漏服条数"，不是"未读条数"，并且**不会清零**。
- **OCR 是假的。** `/api/ocr/scan` 返回写死的文本，没有真实图像识别，也没有上传图片。
- **无并发控制。** 多个请求同时改同一份 `STATE` 时没有加锁。
- **药品数据不做医学校验。** 不检查药物相互作用、不做剂量合理性判断、不给诊断建议——
  这是设计边界，不是遗漏。

---

## 附录：手动 curl 调试（Windows 编码坑）

`curl_checks.sh` 覆盖了下面这些，需要单条手测时再翻这一节。

> **Windows 必读（请求方向）**：Git Bash 会把命令行参数里的中文转成 ANSI 码页（GBK），
> 服务端按 UTF-8 解析就会返回 `There was an error parsing the body`。
> 所以**请求体含中文时，一律走 heredoc + `--data-binary @-`**，不经过 argv。
> 纯 ASCII 的请求体（如 `-d '{}'`）用 `-d` 没问题。

> **Windows 必读（响应方向）**：`python -m json.tool` 读 **stdin** 时用的是系统码页
> （`sys.stdin.encoding == 'gbk'`），把 UTF-8 的响应体按 GBK 解码就会错位，
> 报 `Expecting property name enclosed in double quotes: line 1 column N`。
> **服务端返回的 JSON 是合法的**，是校验工具解错了。正确做法是让响应先落盘、再显式 UTF-8 读：
>
> ```bash
> curl -s -X POST $BASE/api/parse -H 'Content-Type: application/json' -d '{}' -o resp.json
> python -c "import json;d=json.load(open('resp.json',encoding='utf-8'));print(d['ai_ok'],len(d['items']))"
> ```
>
> 或者给管道加 `export PYTHONIOENCODING=utf-8`。

> **`/tmp` 是第三个坑。** MSYS 的 `curl` 把 `/tmp/x` 写到 Git Bash 的 `/tmp`，
> 而 Windows 的 `python` 会去找 `C:\tmp` ——同一串路径两个程序理解不一样，
> 表现是"文件明明写了却找不到"。
> 内嵌在 `python -c "... '/tmp/x.json' ..."` **源码里**的路径不会被 MSYS 转换。
> 用相对路径，或让 bash 做重定向（`< file`）把路径交给 shell 展开。

### 主链路

```bash
BASE=http://127.0.0.1:8000

# 1) Mock 拍照，拿到固定药盒文字
curl -s -X POST $BASE/api/ocr/scan

# 2) 调 DeepSeek 解析成结构化数据（不传 raw_text 就用 mock 文字）
curl -s -X POST $BASE/api/parse -H 'Content-Type: application/json' -d '{}'

# 3) 子女确认。把上一步的 plan_id 填进来
curl -s -X POST $BASE/api/plans/<PLAN_ID>/confirm \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{"items":[
 {"name":"阿莫西林胶囊","dose_amount":0.25,"dose_unit":"g","times_per_day":3,"frequency_raw":"一日三次","duration_days":7,"duration_raw":"疗程7天"},
 {"name":"硝苯地平控释片","dose_amount":30,"dose_unit":"mg","times_per_day":1,"frequency_raw":"一日一次","duration_days":null,"duration_raw":""},
 {"name":"布洛芬缓释胶囊","dose_amount":1,"dose_unit":"粒","times_per_day":2,"frequency_raw":"遵医嘱","duration_days":null,"duration_raw":""}
]}
JSON

# 4) 老人端今日药历
curl -s $BASE/api/doses/today

# 5) 打卡
curl -s -X POST $BASE/api/doses/<DOSE_ID>/take

# 6) 超时三选一（action: carried_out | make_up | missed）
curl -s -X POST $BASE/api/doses/<DOSE_ID>/resolve \
  -H 'Content-Type: application/json' -d '{"action":"carried_out"}'

# 7) 漏服通知（只有 missed 会产生）
curl -s $BASE/api/notifications
```

### 异常路径

```bash
# A) AI 返回的不是合法 json -> 重试一次后仍失败，走 issues 通道，不生成计划
curl -s -X POST $BASE/api/dev/parse_mode -H 'Content-Type: application/json' -d '{"mode":"malformed"}'
curl -s -X POST $BASE/api/parse -H 'Content-Type: application/json' -d '{}'

# B) AI 返回缺字段 -> 高亮缺失项，强制人工补全
curl -s -X POST $BASE/api/dev/parse_mode -H 'Content-Type: application/json' -d '{"mode":"missing_fields"}'
curl -s -X POST $BASE/api/parse -H 'Content-Type: application/json' -d '{}'

# C) 缺口没补完就点确认 -> 400 + issues（规则1 药名为空、规则4 频次越界同时被拦）
curl -s -X POST $BASE/api/plans/<PLAN_ID>/confirm \
  -H 'Content-Type: application/json' -d '{"items":[{"name":"","dose_amount":1,"dose_unit":"pcs","times_per_day":9}]}'

# D) 切回正常模式
curl -s -X POST $BASE/api/dev/parse_mode -H 'Content-Type: application/json' -d '{"mode":"normal"}'
```

### 演示辅助

```bash
# 虚拟时钟：正数拨快（触发超时），负数回拨（回到干净的待服用状态）
curl -s -X POST $BASE/api/dev/advance_time -H 'Content-Type: application/json' -d '{"minutes":120}'
curl -s -X POST $BASE/api/dev/advance_time -H 'Content-Type: application/json' -d '{"minutes":-120}'

# 复位全部内存状态
curl -s -X POST $BASE/api/dev/reset
```

---

## 文件结构

```
elder-pillbox/
├─ app.py            后端全部逻辑（CONFIG / STATE / AI / VALIDATE / DOMAIN / ROUTES 六个区块）
├─ static/index.html 前端（单文件，HTML+CSS+原生 JS，不引框架）
├─ curl_checks.sh    后端链路自检脚本（32 项断言）
├─ requirements.txt  fastapi / uvicorn / httpx
├─ .env.example      环境变量参考模板（程序不会自动读取它）
├─ .gitignore
└─ README.md
```
