# NL Blender Designer — 自然语言控制 Blender 设计系统

用自然语言控制 Blender 建模的独立 Agent 系统。当前版本 **v0.9.0**（Phase A + Phase B 已交付，C1 自动化回归测试已落地；C2 EvolvIQ 可选上报钩子 + C3 本地语音输入已交付）。

## 架构

```
┌─────────────────────┐   HTTP POST    ┌──────────────────────────┐
│ Blender 插件         │  /v1/agent/    │ 独立 LLM 后端             │
│ (nl_blender_design) │ ─────────────▶ │ (agent_server.py)        │
│ 自然语言 → bpy 执行  │ ◀───────────── │ DeepSeek / 离线模板回退   │
└─────────────────────┘   code/explanation│ + 评估接口 /evaluate   │
                                            └──────────────────────────┘
```

- 独立部署，不并入 EvolvIQ 核心；未来可由 EvolvIQ 网关以独立 agent 身份接入（仅改 `AGENT_BASE_URL`）。
- HTTP 契约：`POST {base_url}/v1/agent/completion`，body `{"prompt", "context":{...}}`，resp `{"code","explanation"}`。
- 评估契约：`POST {base_url}/v1/agent/evaluate`，resp `{"pass":bool, "issues":[...]}`。

## 文件

| 文件 | 作用 |
|------|------|
| `nl_blender_design.py` | Blender 单文件插件（N 面板入口）。需同步到用户 addons 目录。 |
| `agent_server.py` | 独立 LLM 后端（标准库 http.server）。真实 LLM + 离线模板双链路。 |
| `shape_library.py` | 参数化造型库（16 种造型 + 材质/环境预设 + 布光/相机/渲染）。插件与后端共用。 |
| `config.json` | DeepSeek key 等配置（gitignored，不入库）。 |
| `sync_addon.py` | 把插件同步到用户 addons 目录（改完源码后跑一次）。 |
| `start_agent.bat` / `start_mock.bat` | 后端启动器（纯 ASCII / CRLF / 无 BOM）。 |
| `verify_in_blender.py` | Blender 端到端/headless 回归套件（离线不耗额度，覆盖全部造型/材质/环境/渲染/算子契约）。 |
| `verify_backend.py` | 纯 Python 后端离线回归测试（不依赖 Blender，可在任意 Python 环境跑）。 |

## 快速开始

1. 启动后端（一个独立终端）：双击 `start_agent.bat`，看到 `Listening at: http://127.0.0.1:8765` 即成功。
2. Blender 里启用插件：偏好设置 → 插件 → 搜索 `NL Blender Designer` → 勾选（若已装过旧版，先取消勾再勾以重载）。
3. 右侧 N 面板 → **NL Design** 标签页。

## 面板功能（v0.8.0）

- **生成并建模**：自然语言 → Agent → 沙箱执行 bpy（单次）。
- **智能生成（自修正）A1**：生成 → 执行 → 截图+场景状态评估 → 不达标自动重写（最多 3 轮）。
  - 勾选「截图回传（视觉评估）」会把 3D 视图截图传给视觉模型（需 `config.json` 中 `vision:true` 且模型支持视觉）。
  - 未开启截图时，后端基于场景状态 JSON 做结构化评估；无 key 时离线判定（至少生成一个网格对象）。
- **一键渲染出图 B4**：用 Blender 原生后台渲染 job（`bpy.ops.render.render('INVOKE_DEFAULT')` 在**主线程**发起），渲染由 Blender 自己的 job 系统异步执行，**UI 完全不阻塞且绝不崩溃**（绝不在子线程调 bpy）。完成后 PNG 存到 `~/Pictures/nl_blender_renders/`，状态栏显示路径。
- **反馈/细化**：自然语言修改已有对象；支持截图回传作视觉参考。
- **保存 / 导出（A2）**：保存 .blend 工程；导出 glTF(.glb)/STL/FBX 到 `~/Pictures/nl_blender_exports/`。
- **历史指令（A3）**：列出每次指令（最新在前），可「重跑」或「撤销上一步」。
- 开关：`执行失败自动修正`、`高风险操作前确认`（风险闸门）。
- **语音输入（C3）**：面板顶部「语音输入」按钮 → 本地离线语音识别（Vosk 中文模型），说完后自动把文字填入指令框；可勾选「语音识别后自动生成」一步到位。识别在独立进程完成，Blender 主线程仅轮询结果，**UI 不卡、绝不崩溃**（详见文末 C3 章节）。

## 自然语言指令示例

### 造型（LLM 或离线模板 → 调 `nl_shapes.*`）
- 原 7 种：`做一个杯子` / `生成齿轮` / `建一把椅子` / `建一张桌子` / `做花瓶` / `做瓶子` / `3D 文字 "A"`
- B1 新增建筑：`盖个小屋` / `建一座塔` / `做一个拱门`
- B1 新增机械：`来个螺栓` / `放个轴承` / `做个齿轮箱`
- B1 新增有机体：`种一棵树` / `放块石头` / `来朵云`

### 材质与环境（B3，一句话切换质感与灯光）
- `换成木质` / `改成玻璃材质` / `镀金属` / `陶瓷质感` / `银质`（作用于选中对象）
- `调到室外灯光` / `室内布光` / `用影棚布光`

### 场景编排（B2，一句话排多对象 + 出图）
- `摆一套咖啡桌场景` → 桌 + 4 椅 + 杯子 + 影棚布光 + 渲染
- `建一个小院子` → 房子 + 树 + 石头 + 室外光 + 渲染
- `搭一个机械装置` → 齿轮箱 + 螺栓 + 轴承 + 影棚布光 + 渲染

### 其他
- 修改：`把它改成金色` / `放大一倍` / `移到右边`
- 动画：`让它旋转起来`
- 阵列：`排成 5x5`
- 出图：`渲染一张产品图`

## 安全机制

- 执行沙箱：模块白名单 + 内建函数白名单 + 超 8000 字符拒执行。
- 风险闸门：正则扫描高危模式，开启确认后执行前弹窗列出风险与代码预览。

## 回归测试（C1 自动化）

两套测试守护「生成逻辑不退化」+「绝不在子线程调 bpy（防整进程崩溃）」：

**1. 后端离线测试** `verify_backend.py`（纯 Python，不依赖 Blender / bpy）：
```bash
python verify_backend.py
```
覆盖：代码提取 / 16 种造型关键词命中 / 场景编排 / 材质环境 / 基本体·修改·动画·阵列 / 评估回退判定 / LLM 模板。失败退出码 1（CI 友好）。

**2. Blender 端到端 headless 测试** `verify_in_blender.py`（必须在 Blender 内跑）：
```bash
"D:/Program Files/Blender Foundation/Blender 4.5/blender.exe" --background --python verify_in_blender.py
```
覆盖：16 种造型函数直接调用 / 9 种材质 / 3 种环境 / 自然语言→模板→沙箱执行端到端 / 渲染场景准备（B4 崩溃回归点）/ 8 个算子契约（bl_description 非空）/ 历史面板写入。
想真渲染验证不崩：`set NL_RENDER_TEST=1` 再跑（低分辨率，几秒）。

> 注：本套件在真 Blender 4.5.1 下首次跑即抓出 `make_house`/`make_tower` 的 `primitive_cone_add(radius=...)` 参数错误（应为 `radius1`/`radius2`）——这类造型手工真机从未测到，凸显回归测试价值。

## 评估说明（A1 视觉反馈闭环）

闭环流程：生成代码 → 沙箱执行 → 收集场景状态(+可选截图) → 调用 `/v1/agent/evaluate` → 若 `pass=false` 把 `issues` 作为反馈让 Agent 重写 → 最多 3 轮。

- 视觉评估需要：`config.json` 设 `"vision": true` 且所用模型支持多模态图像输入；否则自动降级为基于场景状态 JSON 的结构化评估。
- 评估服务不可用时，退化为「执行成功即交付」并提示。

## C2：EvolvIQ 网关可选上报钩子

为未来与 EvolvIQ 编排层协同预留的**可选、配置门控、不耦合内核**的 usage 上报能力。本系统是独立部署，不并入 EvolvIQ 核心；当需要在网关侧统计/审计 Agent 调用时，只需配置以下项（环境变量优先，其次 `config.json`）：

| 配置项 | 环境变量 | 说明 |
|--------|----------|------|
| `evolviq_report_enabled` | `BLENDER_AGENT_EVOLVIQ_REPORT` | 是否开启上报（默认 `false`） |
| `evolviq_gateway_url` | `BLENDER_AGENT_EVOLVIQ_GATEWAY` | 网关接收地址（如 `http://网关/v1/agent/usage`） |
| `evolviq_api_key` | `BLENDER_AGENT_EVOLVIQ_KEY` | 网关鉴权 Bearer Token |
| `evolviq_dry_run` | `BLENDER_AGENT_EVOLVIQ_DRYRUN` | 调试模式：只打印 payload 不发网络 |

行为：每次 `/completion` 与 `/evaluate` 成功后，best-effort POST 一条 `{"app":"blender","event":...,"ts":...,"prompt":...,"ok":...}` 记录；**未开启、网关不可达、或上报异常均被静默吞掉，主服务不受影响**。回归测试 `verify_backend.py` 第 [9] 组覆盖「默认关闭不触发 / 开启返回正确 payload / 异常不污染主流程」。

## C3：本地语音输入（Vosk 离线中文识别）

点击面板「语音输入」→ 插件以**子进程**启动 `voice_capture.py`（托管 venv 的 Python + vosk + sounddevice），麦克风语音经 Vosk 流式识别为文字，结果写入临时 JSON，Blender 主线程用 `bpy.app.timers` 轮询读取并填入指令框。

**线程安全**：语音识别全程在独立进程运行、绝不碰 bpy；插件只在主线程轮询结果文件并安全调用 bpy，严格遵守 Blender 线程铁律（绝不在子线程调 bpy，杜绝崩溃）。

### 首次使用：安装语音模块

双击 `voice_setup.bat`（或手动）：
```bat
REM 在 blender_nl_design 目录内执行
voice_setup.bat
```
它会：① 在 `C:\Users\Administrator\.workbuddy\binaries\python\envs\voice` 建 venv；② 安装 `vosk sounddevice numpy`；③ 下载 Vosk 中文模型 `vosk-model-small-cn-0.22` 到 `~/.cache/vosk/`。

如需自定义路径，可用环境变量覆盖（在系统/用户环境变量里设）：
- `NL_VOICE_PYTHON`：语音 venv 的 python 路径
- `NL_VOICE_SCRIPT`：`voice_capture.py` 路径
- `NL_VOICE_MODEL`：模型目录（默认 `~/.cache/vosk/vosk-model-small-cn-0.22`）

### 用法
1. Blender 中启用插件并打开 NL Design 面板。
2. 点击「🎤 语音输入」→ 对麦克风说一句自然语言指令（如「做一个红色杯子」）。
3. 说完停顿约 1 秒（或最长 15 秒）自动停止，识别文字填入指令框；勾选「语音识别后自动生成」可直接出模型。
4. 未装模型/无麦克风时，面板会给出明确报错提示，不影响其他功能。

> 注：语音依赖在独立 venv，不污染 Blender 自带 Python 与系统环境。

> **本机已预装**：venv（`...\envs\voice`，含 vosk/sounddevice/numpy）与中文模型（`~/.cache/vosk/vosk-model-small-cn-0.22`）均已就绪，直接点击「语音输入」即可使用，**无需重跑 `voice_setup.bat`**。仅当迁移到新机器时才需重跑安装脚本。模型已从官方源下载并校验完整（ZIP_OK + 引擎加载通过）。

## 版本历史

- v0.1.0 MVP：基础生成 + mock 后端
- v0.2.0 收紧解析 + 反馈闭环 + 沙箱加固
- v0.3.0 风险闸门二次确认
- v0.4.0 意图扩展（动画/修改/阵列）
- v0.5.0 复杂造型库（7 种）
- v0.6.0 产品级出图（布光+相机+渲染）
- v0.7.0 Phase A：A1 视觉反馈闭环 + A2 工程保存/导出 + A3 历史指令面板
- v0.8.0 Phase B：B1 造型库扩至 16 种（建筑/机械/有机体）+ B3 材质/环境预设 + B2 场景编排 + B4 原生后台渲染 job（主线程 INVOKE，杜绝子线程崩溃）
- v0.8.0 + C1：新增自动化回归测试套件（`verify_backend.py` 离线 + `verify_in_blender.py` Blender headless，49 用例全绿），并由 headless 测试抓出修复 B1 造型库 `make_house`/`make_tower` 的 cone operator 参数错误
- v0.9.0 Phase C：C2 新增 EvolvIQ 网关可选上报钩子（配置门控、best-effort、不耦合内核；`verify_backend` 第 [9] 组守护）；C3 新增本地语音输入（Vosk 离线中文识别，子进程执行 + 主线程 timers 轮询，严守 Blender 线程铁律）；顺带修复 Blender 4.5 引擎枚举改名（`BLENDER_EEVEE`→`BLENDER_EEVEE_NEXT`）导致的渲染回退失效，真机 headless 回归 50/50 全绿且真渲染不崩
