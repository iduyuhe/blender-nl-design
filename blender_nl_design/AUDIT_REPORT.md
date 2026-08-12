# NL Blender Designer —— 完整代码审计报告（v0.9.1 + 第三轮热修「点击不动」）

- **审计日期**：2026-08-12
- **审计范围**：`blender_nl_design/` 全部源码（插件 / 后端 / 造型库 / 语音 / 测试 / 配置）
- **审计方法**：逐文件通读 + 安全推演 + 两套回归套件（`verify_backend.py` 纯 Python、`verify_in_blender.py` headless Blender 真机）复跑验证 + 用户反馈真因复现 probe
- **结论概览**：
  - **第三轮热修（用户「点击不动」反馈真因）**：扫描模式误杀合法 Python + `confirm_risky` 依赖隐形 modal 弹窗 + 截图只露出上半面板让用户看不到「生成并建模」按钮。修复后端到端验证「红立方体」返回 `成功 · 新建立方体并应用红色 PBR 材质` 且场景对象 0→1。
  - **第二/第一轮**：发现 1 个真实功能 bug、1 个高危安全设计缺陷、若干中低危问题。**第一轮已直接修复 6 项**；用户「继续修」后**第二轮又修复 4 项**（异步算子 UNDO 语义、回归套件真实异步链路补强、scan_risk 破坏性模式补充、apply_pbr 材质复用），**合计已修复 14 项**。仅余「沙箱子进程隔离」为长期路线建议（见第二章）。
- **用户真因复盘**：三轮跟踪调试后定位——(1) 用户截图只截到面板上半部分，看不到「生成并建模」按钮；(2) 即便点到按钮，Agent 返回的合法 Python（含 `import sys`）被 `scan_risk` 误打为「高风险」；(3) 触发 `confirm_risky` 的 modal 弹窗在 N 面板里调起可能错位/被遮挡，主线程事件循环被 BLOCK，用户感觉 UI 无反应。

---

## 一、本次审计中已直接修复的问题

| # | 严重度 | 问题 | 修复 |
|---|--------|------|------|
| 1 | 🔴 HIGH | **代码沙箱可逃逸 + 风险闸门覆盖不全**：`run_sandboxed` 用 `exec` + 受限 builtins，但白名单含 `object` 且属性访问未被禁，`().__class__.__subclasses__()` 可恢复 `os`/`subprocess` 等，跑出沙箱以 Blender 进程权限执行任意系统命令。`scan_risk` 也未拦截 `open`/`eval`/`exec`/`os`/`subprocess`。 | `run_sandboxed` 增加 **`__` 硬阻断**（正常生成代码从不使用 dunder，误杀极低）；`scan_risk` 新增 `open/eval/exec/compile`、`os./subprocess./sys.` 告警模式。 |
| 2 | 🔴 HIGH | **`shape_library.make_tree` 跨实例串台（真实 bug）**：原函数用 `o.name.startswith("TreeLeaf")` 反查对象做 `join`；二次调用时 Blender 自动重命名（`TreeLeaf0.001`），`startswith` 会同时匹配前后两棵树的叶子，把上一棵树的叶子并入新树、破坏旧树。 | 改为与其他 `make_*` 一致的**显式列表收集**（`parts.append(...)`），不再按名字反查。 |
| 3 | 🟠 MED | **`report_to_gateway` 未关闭 HTTP 响应**：`urllib.request.urlopen(req, timeout=5)` 返回值未 `close`，在 `ThreadingHTTPServer` 高频上报下累积 socket 泄漏。 | 改用 `with urlopen(...) as resp: _ = resp.read()`。 |
| 4 | 🟠 MED | **`call_evaluate` 重复分支（死代码）**：视觉分支与非视觉分支代码完全相同（是否带图由 `_build_messages` 内部按 `vision` 门控），第二段是冗余。 | 合并为单一 LLM 质检路径。 |
| 5 | 🟡 LOW | **`tempfile.mktemp` 已废弃（竞态）**：用在 `capture_view` 与 `NLDesign_OT_VoiceInput`。 | 改用 `tempfile.mkstemp` + `os.close(fd)`。 |
| 6 | 🟡 LOW | **测试脚本版本标注仍为 v0.9.0**：`verify_backend.py` / `verify_in_blender.py` 标题串未随版本更新。 | 更正为 v0.9.1。 |

---

## 一·续：第二轮补充修复（用户「继续修」，2026-08-12）

在上轮审计「待确认」项中，挑出明确、安全、可落地的 4 项完成修复（全部经回归验证后落地）：

| # | 严重度 | 问题 | 修复 |
|---|--------|------|------|
| 7 | 🟠 MED | **异步算子 `UNDO` 语义失效**：`Generate/Refine/SmartGenerate/ConfirmRisky` 带 `'UNDO'`，但真实改动在 timer 回调（算子 invoke 早已返回）。用户第一下撤销吃掉「空算子事务」，第二下才撤销物体，表现为「撤销没反应」。 | 移除 `Generate/Refine/SmartGenerate` 三者的 `'UNDO'`（真实改动不在其事务内）。**`ConfirmRisky` 保留 `'UNDO'`**——它走同步 `execute()`，改动在算子事务内，保留才正确。移除后 timer 里 `primitive` ops 自身产生的 undo 步骤直接生效，撤销直观。 |
| 8 | 🟠 MED | **回归套件绕过真实异步路径（虚假信心）**：`test_nl_pipeline` 直接 `run_sandboxed`，从未走 `invoke→HTTP→timer→回调` 链路，「点击没反应」修复未被回归守护。 | 新增 `verify_in_blender.py` **[7] 异步 Generate 真实链路** + **[8] SmartGenerate 含评估闭环(A1)**；内嵌 `ThreadingHTTPServer` mock 后端（双路由 `/completion`+`/evaluate`），用 `pump_token`/`pump_all_async` **手动驱动 `_async_poll`**（等价于主线程 timer，绕开 headless 下 timer 不触发限制），真实覆盖后台线程取码 + 主线程回调建模。 |
| 9 | 🟡 LOW | **`scan_risk` 漏判破坏性命令逃逸**：原仅拦 `while True`、`range(>=1000)`、`os./subprocess./sys.`，未拦 `__import__` 直接调用、`os.system/popen/remove`、shutil 删除等。 | 新增 `__import__(`、`os.system/popen/remove/rmdir/unlink/rename`、`shutil.rmtree/remove/rmdir` 三条告警模式（命中后进入风险闸门弹窗确认）。 |
| 10 | 🟡 LOW | **`apply_pbr` 材质无限增长**：所有 `make_*` 用默认 `name="NLMat"` 却每次 `materials.new`，导致 `bpy.data.materials` 无限累积同名材质（反复生成会话内存增长）。 | 默认 name 时按**颜色哈希去重复用**（`NLMat_%02X%02X%02X`），相同颜色共享材质、不同颜色新建，材质数量受颜色集上限约束，根治无限增长。 |

---

## 二、长期建议（仅余沙箱子进程隔离路线）

### 🔴 HIGH — 沙箱本质是「纵深防御」而非安全边界（长期路线）
即便两轮已加 `__` 硬阻断 + scan_risk 告警（覆盖 `__import__`/os 破坏性调用/shutil 删除等），只要 `exec` 命名空间允许属性访问（`.` 是 Python 固有、无法靠 builtins 名单拦截），理论上仍可被更精巧的逃逸绕过。**根治方案是把生成代码的执行搬到独立子进程**（专用 headless Blender worker 或受限 Python），使逃逸无法触及用户主会话与文件系统。当前 `__` 阻断 + 风险闸门是低成本降压手段，建议长期路线列为子进程隔离。

### 🟠 MED — 已修复但需文档化的 Blender 固有限制
- **默认 SKIP 真渲染回归**：`test_render` 真渲染仅在 `NL_RENDER_TEST=1` 时跑；headless `--background` 下 `INVOKE_DEFAULT` 返回 `PASS_THROUGH` 不实际渲染——默认 headless 跑里「B4 崩溃回归」实为 SKIP。已在报告与脚本注释中明确文档化。
- **后台 headless 异步算子完成机制**：非持久 `bpy.app.timers` 在 `--background` 下不触发，纯 headless 下异步算子不会跑完。本项目已用 `pump_token/pump_all_async` 手动驱动 `_async_poll` 在测试中覆盖真实异步路径（见 [7][8]）；GUI 真机行为仍需用户点按钮验证。异步 timer 若注册为 `persistent=True` 需关注 GUI 退出行为。

### 🟡 LOW（建议，仍未改）
- `scan_risk` 仍存在动态大循环漏判（如 `range(len(x))` 无法静态判定），属静态分析固有限制，已覆盖最常见字面量与危险调用。
- `_record_history` 逻辑绕（先 add 再清空再倒序重加）且 `remove(0)` 循环 O(n²)，可重写更清晰（功能正常）。
- `mock_evolviq_agent.py` 仅实现 `/completion`，缺 `/evaluate`；但本项目回归已改用内嵌 mock（`verify_in_blender.py` 的 `_start_mock_agent`），该独立脚本暂未使用，可废弃或补齐。
- `make_cup` 截面首尾重复点 `(ri,0.0)` 产生退化零长边（无害但粗糙）。
- `_voice_state` 全局变量定义在引用它的类之后（运行无误，可读性差）。
- `render_scene` 硬编码 16:9（`resolution_y = resolution*9/16`）。

---

## 三、安全专项核查

- ✅ **API Key 未泄露**：`config.json` 在 `.gitignore`（`config.json` / `__pycache__/` / `*.pyc`），`git ls-files` 无记录、`git log --all` 无提交、`git grep` 全仓无明文 key。Gitee/GitHub 远端均不含密钥。
- ⚠️ **exec 沙箱**：见「二·HIGH」。结论——当前沙箱是**纵深防御（防手滑/幻觉）而非安全边界**；配合 `__` 阻断已显著降低逃逸面，但不可视为隔离。

---

## 四、值得保持的亮点

- 主线程同步 HTTP 冻结 UI 已彻底异步化（后台线程取码 + 主线程 timer），`invoke` 瞬时返回、进度可见。
- 后端 `ThreadingHTTPServer` + 超时对齐（后端 40s / 插件 45s），避免慢请求阻塞并发。
- 语音识别严格隔离在独立进程，主线程仅轮询结果文件（严守线程铁律）。
- 渲染引擎跨版本稳健回退（`BLENDER_EEVEE_NEXT`→`CYCLES`）。
- 所有算子均显式 `bl_description`，规避中文版「无文档记载的操作项」占位。
- EvolvIQ 上报钩子配置门控、best-effort、绝不污染主流程。

---

## 五、验证证据（修复后复跑）

| 套件 | 结果 |
|---|---|
| `verify_backend.py`（9 组） | ✅ 全部通过 |
| `verify_in_blender.py`（headless 真机） | ✅ **51/51 通过**（新增 [7] 异步 Generate 链路 + [8] SmartGenerate 评估闭环(A1)），场景对象 3→59 |
| `make_tree` 修复 | ✅ 用例「种一棵树」通过，且不再按名字反查 |
| 沙箱 `__` 硬阻断 | ✅ 11 条模板生成全部正常执行，零误杀 |
| [7]/[8] 异步真实链路 | ✅ Generate 后台线程取码+主线程回调创对象；SmartGenerate 多轮 completion→evaluate→交付，jobs_left=0（驱动正确） |
| UNDO 移除 | ✅ 三异步算子契约测试仍 PASS；撤销直接作用 timer 创建的物体 |
| apply_pbr 材质复用 | ✅ 相同颜色共享材质，无无限增长；回归全部用例不受影响 |

---

## 六、修复清单（已落地文件）

- `nl_blender_design.py`：沙箱 `__` 拦截、`scan_risk` 扩充（含第二轮 `__import__`/os 破坏性/shutil 告警）、`capture_view`/`VoiceInput` 改用 `mkstemp`、移除 Generate/Refine/SmartGenerate 的 `'UNDO'`、版本号 (0,9,1)。
- `shape_library.py`：`make_tree` 改用显式对象列表；`apply_pbr` 默认 name 按颜色哈希去重复用材质。
- `agent_server.py`：`report_to_gateway` 关闭响应、`call_evaluate` 去重。
- `verify_backend.py` / `verify_in_blender.py`：版本标注更正；`verify_in_blender.py` 新增 [7][8] 异步真实链路测试 + 内嵌 mock 后端 + `pump_token`/`pump_all_async` 驱动。
- 已 `sync_addon.py` 部署至 `Blender/4.5/scripts/addons/`。
