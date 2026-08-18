---
name: canteen-radar-dev
description: 继续开发食堂味蕾雷达（食堂选餐 Agent 系统）。需要新增打分器、千帆客户端、Agent 编排、FastAPI 接口或前端页面，或需要了解本项目分层约定与验证方式时使用。
---

# 食堂味蕾雷达 开发指引

基于百度智能云千帆平台的大模型 Agent 食堂选餐系统，参赛作品。
仓库 `D:\canteenflavorradar`，远程 `github.com/WulingShan503/canteenflavorradar`。

## 开始前必做

1. 读 `README.md` 的「开发进度」清单，确认当前停在哪一步。
2. 读 `backend/app/models/enums.py`，所有新代码必须复用这里的枚举，不要自己造词。
3. 确认本步范围只覆盖清单里的一到两项——用户明确要求小步交付。

## 分层约定（不要破坏）

| 层 | 位置 | 由谁负责 |
| --- | --- | --- |
| 偏好解析 | `app/agent/` | 大模型，失败降级到关键词规则 |
| 硬性过滤 | `app/services/dish_repository.py` | 纯规则 |
| 排序打分 | `app/services/scorer.py` | 纯规则，输出 ScoreBreakdown |
| 推荐语生成 | `app/agent/` | 大模型，失败用规则理由兜底 |
| 模型调用 | `app/services/qianfan_client.py` | 只管发请求，失败抛异常不兜底 |
| HTTP 接口 | `app/api/` | FastAPI 路由，薄一层 |

接口层的约定：`app/api/schemas.py` 是对外契约，和 `app/models/` 的领域模型分开，
改内部字段不直接破坏前端。路由函数只做「校验参数 → 调一层业务 → 返回」，
不写业务逻辑。`QianfanError` 不会漏到路由层——编排层已经降级成规则结果了。

铁律：

- **过敏原、饮食限制（素食/清真等）永远由规则层拦截，绝不写成 prompt 约束。**
  `DishRepository.RELAX_STEPS` 逐级放宽列表里不得加入 `avoid_allergens` 或 `dietary_tags`。
- 大模型调用一律要有降级路径，千帆不可用时系统降级但不能瘫痪，
  响应里用 `RecommendResponse.fallback_used` 标记。
- 打分要保留各维度明细，不能只给总分——推荐理由要有据可依，调参也需要看是哪一维起作用。
- 打分层只排序候选集，**不许把被过滤掉的菜捞回来**，也不许在这层做安全判断。
- 权重之和必须为 100（`ScoreWeights.total()`），综合分才能落在 0-100；
  新增权重方案时先跑 `TestWeights::test_all_weight_sets_sum_to_100`。
- 用户没提的维度给中性分 `NEUTRAL`（0.5），不做惩罚；不能因为「没说预算」就扣分。
- 排序必须稳定可复现：并列时按销量、评分、菜品 id 逐级兜底。
- 千帆的 API Key 只从环境变量读，不写进代码或提交进仓库。
- `QianfanClient` 只管调用，失败一律抛 `QianfanError`，**不许在客户端里 return 兜底文案**；
  否则上层无法通过 `fallback_used` 告知用户已降级。
- 不配密钥也必须能启动跑通（纯规则模式），评审和本地开发都依赖这点。
- **模型解析出的偏好只是输入，不是许可**：解析结果必须原封不动过一遍
  `DishRepository.find_candidates`，绝不能因为「模型已经考虑过过敏原」就跳过过滤。
- 模型脏输出一律「能救的部分先救下来」，不要让整条请求失败：
  `extract_json` 捞代码块、`build_preference` 逐字段剔非法值、
  推荐语缺条目就单条兜底。
- 关键词规则里**金额正则必须带「块/元」单位**。曾经写成可选，
  结果「排队不超过 10 分钟」被解析成预算 10 元、「蛋白质至少 30 克」
  被解析成最低预算 30 元——凭空给用户加了没提的条件。
- 单字口味词要过 `word_hit`（`FLAVOR_EXCLUSIONS`）：「海鲜」里的「鲜」、
  「控糖」里的「糖」、「芝麻」里的「麻」都不是口味诉求。
- 过敏原关键词必须与「过敏/忌/不能吃」标记**在同一分句**，
  否则「海鲜过敏，想吃鸡蛋」会把蛋类也当成过敏原。

## 验证方式（重要）

本机**没有可用的 Python**，`pytest` 跑不了。写完带判断逻辑的代码后：

1. 在 `backend/` 下建临时 `_verify_xxx.js`，直接读 `app/data/*.json`，
   用 Node 等价复刻这段 Python 逻辑，逐条打印 PASS/FAIL。
2. 跑 `node _verify_xxx.js` 确认全通过。
3. **立刻 `rm` 掉临时脚本**，不要提交。
4. 同时把对应的 `pytest` 用例写进 `backend/tests/`，交给用户装好环境后自己跑。
5. 向用户汇报时说清：哪些是我实跑验证过的，哪些需要他 `pytest` 确认。

改动 `app/data/*.json` 后也用 Node 校验一遍字段完整性和枚举取值合法性。

### 前端要真在浏览器里跑一遍

本机装的是 Edge（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`）。
`--dump-dom` 和 `--screenshot` 在这台机器上输出为空（被同步登录弹窗抢了焦点），
**要用 CDP**：带 `--headless=new --remote-debugging-port=9222 --user-data-dir=<临时目录>`
启动，再用 Node 走 WebSocket 发 `Page.navigate` / `Runtime.evaluate` /
`Page.captureScreenshot`。这样能拿到真实的 DOM、控制台报错和截图。

必查项：`Runtime.exceptionThrown`（有没有 JS 异常）、`Log.entryAdded`（资源 404）、
渲染出的卡片数、窄屏 375px 有没有横向溢出。
纯静态页也要验 live 模式——用 Node 写个临时 stub server 提供 `/api/*`，
就能测到 `api.js` 的探活切换逻辑，不必等 Python 环境。

**`node --check` 每个 JS 文件**。分块写大文件时很容易留下重复的收尾括号
（这次 `app.js` 末尾就多了一个 `})();`，浏览器里表现为整个脚本不执行、
页面只有静态骨架），而这类错误光看渲染结果不容易定位。

## 收尾流程

每步做完，按顺序：

1. 本地 `git commit`（身份已在仓库本地配好，中文提交信息，正文列要点）。
   **默认不 push**，远程推送等用户明确要求。
2. 更新 `README.md` 的开发进度清单和目录结构。
3. 更新记忆 `canteen-flavor-radar-architecture.md` 里的进度段落。
4. 同步本文件的进度与新增约定。
5. 向用户汇报本步成果，停下等下一步指示，不要自动往下做。

## 进度

- [x] 骨架、enums 共享词表、Dish/UserPreference/Recommendation 模型
- [x] 示例数据 3 食堂 30 道菜（含过敏原、停供、各辣度边界）
- [x] `DishRepository`：硬性过滤 + 逐级放宽 + pytest 用例
- [x] `DishScorer`：六维加权打分、三套场景权重、`rank_diverse` 多样性约束、
      规则版 `reasons`；`build_reasons` 的输出后续要作为事实依据喂进推荐语 prompt
- [x] `app/config.py` + `.env.example`：pydantic-settings 配置，密钥只从环境变量读
- [x] `QianfanClient`：httpx 异步、token 缓存与并发去重、指数退避重试、
      熔断半开探测；只抛 `QianfanError` 家族，不做业务兜底。
      测试用 `httpx.MockTransport`，不联网不需要真密钥
- [x] `app/agent/`：`PreferenceParser`（模型 JSON + 关键词规则降级）、
      `keyword_rules.py`（词表与正则）、`CommentWriter`（规则理由作事实依据）、
      `RecommendAgent`（四层串联 + 凑整餐）
- [x] `app/api/`（routes + schemas）与 `app/main.py`：五个接口、CORS、
      lifespan 预热与释放连接池、中文化的 422 报错、500 兜底不漏堆栈
- [x] `frontend/`：纯静态单页（无构建工具）。`api.js` 自动在真实后端与
      内置 `demo-engine.js`（规则层等价 JS 移植）之间切换，
      所以 `file://` 双击打开也能看到完整效果
- [ ] 后端骨架已完整。可选的后续方向：菜品评价写入、真实菜单接入、
      推荐结果埋点与调参、Docker 化部署

## 前端约定

`frontend/js/demo-engine.js` 是后端规则层的等价移植，
**改了 `dish_repository.py` 或 `scorer.py` 的规则，必须同步改它**，
否则演示模式和真实后端的结果会不一致。对应关系写在该文件顶部注释里。

改了 `backend/app/data/*.json` 后跑 `node scripts/build-demo-data.js` 同步快照。
数据必须挂在 `global.` 上（顶层 `const` 不会成为 `window` 的属性）。

得分条长度用「占该维权重上限的比例」，不是绝对分——
口味满分 30 和便利满分 10 画在一起没法比。权重为 0 的维度不画。
- [ ] Agent 编排 `app/agent/`：偏好解析 + 推荐语生成 + 凑整餐 MealPlan
- [ ] FastAPI 接口 `app/api/`：推荐、菜品查询、食堂列表
- [ ] 前端页面

## 数据字段速查

菜品字段含义见 `app/models/dish.py`。示例数据里几个用于测边界的点：
`D1025 芝士焗饭` 是唯一 `available: false` 的菜；`D1017 酸辣粉` 含花生；
`D1011 兰州牛肉面` 和 `D1017` 含香菜（测忌口）；`D1021 香煎鸡胸沙拉` 是高蛋白低脂代表；
辣度分布 0 分 19 道、5 分 1 道（`D1007 水煮肉片`）。
