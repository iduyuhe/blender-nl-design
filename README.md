# NL Blender Designer

> 用自然语言控制 Blender 进行 3D 建模 / 渲染 / 导出的独立 Agent 系统。
> 当前版本 **v0.9.1** · 开源协议 Apache-2.0 · 主仓库 [Gitee](https://gitee.com/i4hub/blender-nl-design)，[GitHub](https://github.com/iduyuhe/blender-nl-design) 镜像

一句话：在 Blender 里用**中文说话或打字**，就能生成杯子、椅子、小屋、齿轮、完整场景，并一键渲染出图、导出工程。背后是一个**本地 LLM Agent**（DeepSeek / 离线模板双链路）把自然语言翻译成 `bpy` 代码，在**受限沙箱**里安全执行。

---

## 这是什么

传统 Blender 建模门槛高、操作繁琐。本项目把「自然语言 → 3D 模型」做成一套**独立部署**的 Agent 系统：

- **插件侧**（Blender N 面板）采集你的自然语言指令；
- 通过 HTTP 把指令发给一个**独立 LLM 后端**（不依赖 Blender 进程）；
- 后端返回可执行的 `bpy` 代码 + 解释，插件在**受限沙箱**里执行，安全生成 / 修改物体。

系统刻意保持「独立」，不并入任何生产平台内核，方便二次开发与审计。

---

## 核心架构

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

---

## 功能清单（v0.9.1）

| 模块 | 能力 |
|------|------|
| **自然语言生成** | 一句话生成 16 种参数化造型（杯/椅/桌/瓶/花瓶/齿轮/文字/小屋/塔/拱门/螺栓/轴承/齿轮箱/树/石/云） |
| **场景编排** | 一句话排多对象 + 布光 + 渲染（如「摆一套咖啡桌场景」「建一个小院子」） |
| **材质 / 环境** | 一句话切换 9 种材质（木/玻璃/金属/陶瓷/银…）+ 3 种环境布光（室外/室内/影棚） |
| **智能生成（自修正 A1）** | 生成 → 执行 → 评估 → 不达标自动重写（最多 3 轮，支持视觉截图回传） |
| **一键渲染（B4）** | Blender 原生后台渲染 job，UI 不阻塞、绝不崩溃 |
| **保存 / 导出（A2）** | 保存 .blend；导出 glTF(.glb)/STL/FBX |
| **历史指令（A3）** | 列出每次指令，可重跑 / 撤销上一步 |
| **语音输入（C3）** | 本地离线中文语音识别（Vosk），说完后自动填入指令框 |
| **风险闸门** | 正则扫描高危模式，执行前弹窗确认 |
| **EvolvIQ 网关上报（C2）** | 可选、配置门控、best-effort 的 usage 上报钩子（默认关闭） |
| **自动化回归（C1）** | 后端离线测试 + Blender headless 端到端测试，守护「生成逻辑不退化 / 绝不在子线程调 bpy」 |

> v0.9.1 重点修复「点击生成并建模没反应」：根因是主线程同步 `urllib` 冻结 UI，已全链路异步化（后台线程取码 + 主线程 `bpy.app.timers` 轮询），UI 不再假死。

---

## 快速开始

### 1. 启动后端（独立终端）

双击 `blender_nl_design/start_agent.bat`，看到 `Listening at: http://127.0.0.1:8765` 即成功。
（无 DeepSeek key 时自动切换到离线模板，功能照常可用。）

### 2. 启用 Blender 插件

- 偏好设置 → 插件 → 搜索 `NL Blender Designer` → 勾选；
- 若装过旧版，先取消勾再勾以重载。

### 3. 使用

右侧 N 面板 → **NL Design** 标签页：

- **生成并建模**：输入自然语言 → 生成模型；
- **智能生成（自修正）**：带自动评估闭环；
- **一键渲染**：后台渲染出图（存 `~/Pictures/nl_blender_renders/`）；
- **反馈 / 细化**：用自然语言修改已有对象；
- **保存 / 导出**、**历史指令**等面板操作。

---

## 语音输入（C3）

点击面板「🎤 语音输入」→ 插件以**子进程**启动 `voice_capture.py`（独立 venv 的 Vosk 中文识别），识别结果由 Blender 主线程 `bpy.app.timers` 轮询填入指令框。**识别全程在独立进程，绝不碰 bpy，严守线程铁律。**

首次使用双击 `blender_nl_design/voice_setup.bat` 安装语音 venv 与中文模型（仅一次）。本机若已预装则可直接点击使用。详见 [blender_nl_design/README.md](blender_nl_design/README.md) 的 C3 章节。

---

## 安全机制

- **执行沙箱**：模块白名单 + 内建函数白名单 + 禁止 `__` 双下划线访问 + 超 8000 字符拒执行；仅允许 `sys`/`os`/`os.path`/`shape_library` 等必要导入。
- **风险闸门**：正则扫描高危模式（如 `os.system`、`shutil.rmtree`、`eval/exec`、删除对象、无限循环等），开启确认后执行前弹窗列出风险与代码预览。

---

## 自动化回归测试（C1）

两套测试守护「生成逻辑不退化」+「绝不在子线程调 bpy（防整进程崩溃）」：

```bash
# 1. 后端离线测试（纯 Python，不依赖 Blender）
python blender_nl_design/verify_backend.py

# 2. Blender 端到端 headless 测试（必须在 Blender 内跑）
"D:/Program Files/Blender Foundation/Blender 4.5/blender.exe" --background --python blender_nl_design/verify_in_blender.py
# 想真渲染验证不崩：先 set NL_RENDER_TEST=1 再跑
```

---

## 与 EvolvIQ 的关系

本项目是**独立系统**，不并入 EvolvIQ 核心，避免影响生产平台。保持兼容的 HTTP 契约；未来若需协同，由 EvolvIQ 编排层以「独立 agent」身份接入，而非耦合内核。C2 仅预留一个**可选、默认关闭**的上报钩子。

---

## 仓库结构

```
.
├── README.md                 # 本文件（项目落地页）
├── LICENSE                   # Apache-2.0
├── requirements.txt          # 可选语音模块依赖（venv 独立安装，不影响主程序）
└── blender_nl_design/        # 全部源码与详细文档
    ├── nl_blender_design.py  # Blender 插件（N 面板入口，需同步到 addons 目录）
    ├── agent_server.py       # 独立 LLM 后端（标准库 http.server，仅用标准库）
    ├── shape_library.py      # 参数化造型库（16 造型 + 材质/环境 + 布光/相机/渲染）
    ├── voice_capture.py      # C3 语音采集（Vosk 离线识别，独立进程）
    ├── voice_setup.py        # C3 一键安装（建 venv + 装依赖 + 下载模型）
    ├── sync_addon.py         # 把插件同步到用户 addons 目录（改源码后必跑）
    ├── verify_backend.py     # 后端离线回归测试
    ├── verify_in_blender.py  # Blender headless 端到端回归测试
    ├── mock_evolviq_agent.py # EvolvIQ 网关 mock（联调用）
    ├── start_agent.bat       # 后端启动器
    ├── start_mock.bat        # mock 后端启动器
    ├── README.md             # 详细使用文档（安装/全部指令示例/语音/安全/测试/版本历史）
    └── AUDIT_REPORT.md       # 代码审计报告
```

> 注：`config.json`（DeepSeek key 等）被 `.gitignore` 排除，不入库。后端无 key 时自动离线模板回退。

---

## 文档导航

- **详细使用文档**（安装细节、全部自然语言指令示例、语音配置、安全、评估、版本历史）：[`blender_nl_design/README.md`](blender_nl_design/README.md)
- **代码审计报告**：[`blender_nl_design/AUDIT_REPORT.md`](blender_nl_design/AUDIT_REPORT.md)

---

## 许可证

Apache-2.0。详见 [LICENSE](LICENSE)。
