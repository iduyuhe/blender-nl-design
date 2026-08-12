bl_info = {
    "name": "NL Blender Designer",
    "author": "工业5点0产业生态联盟",
    "version": (0, 9, 0),
    "blender": (3, 0, 0),
    "location": "3D View > Sidebar > NL Design",
    "description": "用自然语言控制 Blender 建模（独立设计 Agent 系统：复杂造型库 + 反馈闭环 + 执行沙箱 + 风险闸门 + 语音输入）",
    "category": "Object",
}

import bpy
import os
import re
import json
import time
import sys
import subprocess
import builtins as _builtins
import urllib.request
import urllib.error
import traceback

# 参数化造型库：复杂造型确定性函数（杯子/花瓶/瓶子/齿轮/桌子/椅子/3D文字）
# 注入沙箱 globals，使 Agent 生成的代码可调用 nl_shapes.*（对应本模块）。
try:
    import shape_library as nl_shapes
except Exception:
    nl_shapes = None

# === 后端配置：指向独立的 Blender Design Agent 服务 ===
# 本系统是独立部署，不并入 EvolvIQ 核心，避免影响生产平台。
# 契约：POST {AGENT_BASE_URL}{AGENT_ENDPOINT}
#   body: {"prompt": str, "context": {"app":"blender","scene_state":{...},
#          "last_error":str,"last_code":str,"feedback":str,"image":str(base64)}}
#   resp: {"code": str, "explanation": str}
# 未来若需由 EvolvIQ 编排层调用，只需把 AGENT_BASE_URL 改成其网关地址（契约不变）。
AGENT_BASE_URL = "http://127.0.0.1:8765"
AGENT_ENDPOINT = "/v1/agent/completion"
TIMEOUT = 30

# === C3：本地语音输入（Vosk 离线中文识别）===
# 语音识别在【独立进程】里跑（托管 venv 的 Python + vosk + sounddevice），
# 插件只以子进程方式启动它，并通过 bpy.app.timers（主线程）轮询结果文件，
# 绝不在子线程调用 bpy（遵循 Blender 线程铁律）。以下路径可用环境变量覆盖。
_VOICE_VENV_PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/voice/Scripts/python.exe"
VOICE_PYTHON = os.environ.get("NL_VOICE_PYTHON", _VOICE_VENV_PY)
VOICE_SCRIPT = os.environ.get(
    "NL_VOICE_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_capture.py"))
VOICE_MODEL = os.environ.get(
    "NL_VOICE_MODEL",
    os.path.join(os.path.expanduser("~"), ".cache", "vosk", "vosk-model-small-cn-0.22"))

# === 执行沙箱：限制可导入模块与可用内建函数，降低生成代码破坏系统的风险 ===
ALLOWED_MODULES = {
    "bpy", "math", "mathutils", "random", "json", "time",
    "bmesh", "collections", "itertools", "re",
}

# nl_shapes 造型库（Blender 内 import 成功时注入沙箱 globals）
_SANDBOX_EXTRA = {"nl_shapes": nl_shapes} if nl_shapes is not None else {}


def _safe_import(name, *args, **kwargs):
    top = name.split(".")[0]
    if top in ALLOWED_MODULES:
        return _builtins.__import__(name, *args, **kwargs)
    raise ImportError("出于安全限制，禁止导入模块: %s" % name)


_SAFE_BUILTIN_NAMES = [
    # 基础类型与构造
    "True", "False", "None", "bool", "int", "float", "complex", "str", "bytes",
    "bytearray", "list", "dict", "set", "frozenset", "tuple", "object",
    # 容器/迭代
    "range", "len", "enumerate", "zip", "map", "filter", "iter", "next",
    "reversed", "sorted", "min", "max", "sum", "any", "all", "slice",
    # 数学/转换
    "abs", "round", "divmod", "pow", "hash", "bin", "hex", "oct", "ord", "chr",
    "ascii", "repr", "format",
    # 类型/自省
    "type", "isinstance", "issubclass", "hasattr", "getattr",
    "callable", "id", "property", "staticmethod", "classmethod", "super",
    # 输出
    "print",
    # 异常类（允许代码 try/except 与主动 raise）
    "BaseException", "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "AttributeError", "NameError",
    "ZeroDivisionError", "OverflowError", "StopIteration",
]
SAFE_BUILTINS = {n: getattr(_builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(_builtins, n)}
SAFE_BUILTINS["__import__"] = _safe_import  # 仅放行白名单模块

MAX_CODE_LEN = 8000


def run_sandboxed(code):
    """在受限命名空间中执行生成代码，返回 (success: bool, error_str: str)。"""
    if len(code) > MAX_CODE_LEN:
        return False, "生成代码过长（%d > %d 字符），已拒绝执行" % (len(code), MAX_CODE_LEN)
    g = {"__builtins__": SAFE_BUILTINS, "bpy": bpy}
    g.update(_SANDBOX_EXTRA)
    try:
        exec(compile(code, "<nl_agent>", "exec"), g)
        return True, ""
    except Exception as e:  # noqa: BLE001 - 捕获后回传给 Agent 用于修正
        return False, "%s: %s" % (type(e).__name__, e)


# === 风险闸门：扫描生成代码中可能破坏场景/卡死/错乱的高危模式 ===
# 每条：(正则, 风险说明)。命中任意一条且开启「高风险操作前确认」时，执行前弹窗确认。
RISK_PATTERNS = [
    (re.compile(r"bpy\.ops\.object\.delete"), "将删除对象（可能清空场景内容）"),
    (re.compile(r"bpy\.data\.objects\.remove"), "将移除对象数据"),
    (re.compile(r"bpy\.data\.\w+\.remove"), "将移除 Blender 数据块"),
    (re.compile(r"while\s+(?:True|1)\s*:"), "存在无限循环（会导致 Blender 卡死）"),
    (re.compile(r"range\(\s*\d{4,}\s*\)"), "循环次数过大（>=1000，可能卡顿）"),
    (re.compile(r"bpy\.ops\.wm\.quit_blender"), "将退出 Blender"),
    (re.compile(r"bpy\.ops\.wm\.save_as_mainfile"), "将保存/覆盖工程文件（可能丢失未备份内容）"),
    (re.compile(r"bpy\.ops\.wm\.open_mainfile"), "将打开其他工程文件（当前场景会被替换）"),
    (re.compile(r"bpy\.ops\.render\.render"), "将执行渲染（可能长时间阻塞 Blender 主线程）"),
    (re.compile(r"time\.sleep\s*\("), "将暂停主线程（time.sleep 会导致 Blender 卡顿无响应）"),
]


def scan_risk(code):
    """返回风险描述列表；空列表表示安全。"""
    found = []
    for pat, msg in RISK_PATTERNS:
        if pat.search(code):
            found.append(msg)
    return found


# ---------- 场景状态（供反馈闭环使用） ----------
def get_scene_state():
    try:
        meshes = []
        for o in bpy.data.objects:
            if o.type == 'MESH':
                meshes.append({
                    "name": o.name,
                    "location": [round(x, 2) for x in o.location],
                    "scale": [round(x, 2) for x in o.scale],
                })
        active = bpy.context.active_object
        selected = [{"name": o.name, "type": o.type} for o in bpy.context.selected_objects]
        return {
            "object_count": len(bpy.data.objects),
            "mesh_objects": meshes[:20],
            "active_object": {"name": active.name, "type": active.type} if active else None,
            "selected_objects": selected,
        }
    except Exception:
        return {}


def request_code(prompt, scene_state=None, last_error=None, last_code=None,
                 feedback=None, image=None):
    """调用独立 Blender Design Agent 后端，返回 (code, explanation, error)。"""
    if scene_state is None:
        scene_state = get_scene_state()
    context = {"app": "blender", "scene_state": scene_state}
    if last_error:
        context["last_error"] = last_error
    if last_code:
        context["last_code"] = last_code
    if feedback:
        context["feedback"] = feedback
    if image:
        context["image"] = image
    url = AGENT_BASE_URL.rstrip("/") + AGENT_ENDPOINT
    payload = json.dumps({
        "prompt": prompt,
        "context": context,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("code", ""), data.get("explanation", ""), None
    except urllib.error.URLError as e:
        return "", "", "无法连接 Agent 后端 (%s)：%s" % (url, e)
    except Exception as e:  # noqa: BLE001 - 向用户暴露真实错误更利于排查
        return "", "", "Agent 请求失败：%s" % e


EVAL_ENDPOINT = "/v1/agent/evaluate"


def request_evaluate(prompt, scene_state=None, image=None):
    """调用后端的评估接口，返回 (passed: bool, issues: list, error: str)。"""
    if scene_state is None:
        scene_state = get_scene_state()
    context = {"app": "blender", "scene_state": scene_state}
    if image:
        context["image"] = image
    url = AGENT_BASE_URL.rstrip("/") + EVAL_ENDPOINT
    payload = json.dumps({
        "prompt": prompt,
        "context": context,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("pass", False)), list(data.get("issues", []) or []), None
    except urllib.error.URLError as e:
        return False, [], "无法连接评估后端 (%s)：%s" % (url, e)
    except Exception as e:  # noqa: BLE001
        return False, [], "评估请求失败：%s" % e


def _record_history(props, prompt, code, status):
    """A3：把一次成功的指令写入历史列表（最多保留 50 条，最新的在最前）。"""
    try:
        import time as _time
        item = props.history.add()
        item.prompt = prompt
        item.code = code
        item.status = status
        item.time = _time.strftime("%H:%M:%S", _time.localtime())
        # 保持最新在前 + 总量上限
        items = list(props.history)
        while len(props.history) > 0:
            props.history.remove(0)
        for it in reversed(items[-50:]):
            n = props.history.add()
            n.prompt = it.prompt
            n.code = it.code
            n.status = it.status
            n.time = it.time
    except Exception:
        pass


# ---------- 可选：3D 视图截图回传（需视觉模型 + config vision=true） ----------
def capture_view(context):
    try:
        import tempfile
        import base64
        tmp = tempfile.mktemp(suffix=".png")
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                region = None
                for r in area.regions:
                    if r.type == 'WINDOW':
                        region = r
                        break
                if region is None:
                    continue
                try:
                    with context.temp_override(area=area, region=region):
                        bpy.ops.screen.screenshot_area(filepath=tmp)
                except Exception:
                    bpy.ops.screen.screenshot(filepath=tmp)
                if os.path.exists(tmp):
                    with open(tmp, 'rb') as f:
                        data = base64.b64encode(f.read()).decode('ascii')
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    return data
        return None
    except Exception:
        return None


class NLDesignHistoryItem(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(name="指令", default="")
    code: bpy.props.StringProperty(name="代码", default="")
    status: bpy.props.StringProperty(name="状态", default="")
    time: bpy.props.StringProperty(name="时间", default="")


class NLDesignProperties(bpy.types.PropertyGroup):
    prompt: bpy.props.StringProperty(
        name="指令",
        description="用自然语言描述要创建/修改的内容，例如：创建一个红色立方体",
        default="创建一个红色立方体")
    feedback: bpy.props.StringProperty(
        name="反馈",
        description="自然语言修改指令，例如：把它改成金色 / 放大一倍 / 移到右边",
        default="")
    last_status: bpy.props.StringProperty(name="状态", default="")
    last_code: bpy.props.StringProperty(name="上次代码", default="")
    auto_fix: bpy.props.BoolProperty(
        name="执行失败自动修正",
        description="执行报错时自动把错误回传给 Agent 重试一次",
        default=True)
    confirm_risky: bpy.props.BoolProperty(
        name="高风险操作前确认",
        description="检测到删除对象/无限循环/超大循环等高危代码时，执行前弹窗确认",
        default=True)
    use_screenshot: bpy.props.BoolProperty(
        name="截图回传（视觉评估）",
        description="智能生成时把 3D 视图截图传给视觉模型评估（需 config.json 中 vision=true）",
        default=False)
    auto_submit_voice: bpy.props.BoolProperty(
        name="语音识别后自动生成",
        description="语音识别完成后自动点击「生成并建模」",
        default=False)
    history_index: bpy.props.IntProperty(name="历史选中项", default=-1)
    history: bpy.props.CollectionProperty(type=NLDesignHistoryItem)


# ---------- 基类：统一「风险闸门」流程（fetch → 扫描风险 → 确认/直跑 → 执行） ----------
class NLDesign_OT_Base(bpy.types.Operator):
    _code = None
    _explanation = None
    _risks = []

    def invoke(self, context, event):
        if not self._fetch(context):
            return {'CANCELLED'}
        props = context.scene.nl_design
        self._risks = scan_risk(self._code)
        if self._risks and props.confirm_risky:
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        layout.label(text="检测到高风险操作，确认后再执行：")
        col = layout.column()
        for r in self._risks:
            col.label(text="• " + r)
        layout.separator()
        box = layout.box()
        box.label(text="将执行的代码（预览）：")
        lines = self._code.splitlines()
        for ln in lines[:16]:
            box.label(text=ln if ln.strip() else " ")
        if len(lines) > 16:
            box.label(text="…（已省略）")

    def execute(self, context):
        # 弹窗确认路径：invoke 已 fetch；直跑路径：invoke 也已 fetch。兜底再取一次。
        if self._code is None:
            if not self._fetch(context):
                return {'CANCELLED'}
        return self._run(context)

    # 子类实现
    def _fetch(self, context):
        raise NotImplementedError

    def _run(self, context):
        raise NotImplementedError


class NLDesign_OT_Generate(NLDesign_OT_Base):
    """自然语言生成代码并执行建模（含风险闸门与失败自动修正）。"""
    bl_idname = "nl_design.generate"
    bl_label = "生成并建模"
    bl_description = "根据自然语言指令生成 bpy 代码并执行建模"
    bl_options = {'REGISTER', 'UNDO'}

    def _fetch(self, context):
        props = context.scene.nl_design
        prompt = props.prompt.strip()
        if not prompt:
            self.report({'WARNING'}, "指令为空")
            return False
        code, explanation, err = request_code(prompt, get_scene_state())
        if err:
            self.report({'ERROR'}, err)
            props.last_status = err
            return False
        if not code.strip():
            msg = "Agent 未返回可执行代码"
            self.report({'WARNING'}, msg)
            props.last_status = msg
            return False
        self._code = code
        self._explanation = explanation
        props.last_code = code
        return True

    def _run(self, context):
        props = context.scene.nl_design
        code = self._code
        ok, exec_err = run_sandboxed(code)

        # 反馈闭环：首次执行失败且开启自动修正时，回传错误给 Agent 重试一次
        if (not ok) and props.auto_fix:
            self.report({'INFO'}, "首次执行失败，正在自动修正…")
            code2, exp2, err2 = request_code(
                props.prompt.strip(), get_scene_state(),
                last_error=exec_err, last_code=code)
            if not err2 and code2.strip():
                ok, exec_err = run_sandboxed(code2)
                self._explanation = exp2 or self._explanation
                props.last_code = code2

        if ok:
            status = "成功 · " + (self._explanation or "")
            self.report({'INFO'}, status)
            _record_history(props, props.prompt.strip(), code, status)
        else:
            status = "执行错误：%s" % exec_err
            self.report({'ERROR'}, status)
            print("=== NL Blender Designer 生成代码执行失败 ===\n" + exec_err)

        props.last_status = status
        return {'FINISHED'}


class NLDesign_OT_Refine(NLDesign_OT_Base):
    """基于自然语言反馈修改现有对象（材质/缩放/动画/阵列等）。"""
    bl_idname = "nl_design.refine"
    bl_label = "应用反馈"
    bl_description = "对当前场景应用反馈：换材质/缩放/动画/阵列"
    bl_options = {'REGISTER', 'UNDO'}

    def _fetch(self, context):
        props = context.scene.nl_design
        fb = props.feedback.strip()
        if not fb:
            self.report({'WARNING'}, "反馈为空")
            return False
        image = None
        if props.use_screenshot:
            image = capture_view(context)
        code, explanation, err = request_code(
            fb, get_scene_state(), feedback=fb, image=image)
        if err:
            self.report({'ERROR'}, err)
            props.last_status = err
            return False
        if not code.strip():
            msg = "Agent 未返回可执行代码"
            self.report({'WARNING'}, msg)
            props.last_status = msg
            return False
        self._code = code
        self._explanation = explanation
        props.last_code = code
        return True

    def _run(self, context):
        props = context.scene.nl_design
        ok, exec_err = run_sandboxed(self._code)
        if ok:
            status = "反馈已应用 · " + (self._explanation or "")
            self.report({'INFO'}, status)
            _record_history(props, props.feedback.strip(), self._code, status)
        else:
            status = "执行错误：%s" % exec_err
            self.report({'ERROR'}, status)
            print("=== NL Blender Designer 反馈代码执行失败 ===\n" + exec_err)
        props.last_status = status
        return {'FINISHED'}


MAX_REFINE_LOOPS = 3  # 视觉反馈闭环：最多自动重写轮数


class NLDesign_OT_SmartGenerate(NLDesign_OT_Base):
    """A1 视觉反馈闭环：生成→执行→截图+场景状态评估→不达标自动重写（最多 N 轮）。"""
    bl_idname = "nl_design.smart_generate"
    bl_label = "智能生成（自修正）"
    bl_description = "生成代码→建模→自动评估→不达标自动重写，最多 3 轮"
    bl_options = {'REGISTER', 'UNDO'}

    def _fetch(self, context):
        props = context.scene.nl_design
        prompt = props.prompt.strip()
        if not prompt:
            self.report({'WARNING'}, "指令为空")
            return False
        # 首轮：常规生成
        code, explanation, err = request_code(prompt, get_scene_state())
        if err:
            self.report({'ERROR'}, err)
            props.last_status = err
            return False
        if not code.strip():
            msg = "Agent 未返回可执行代码"
            self.report({'WARNING'}, msg)
            props.last_status = msg
            return False
        self._code = code
        self._explanation = explanation
        self._is_smart = True
        props.last_code = code
        return True

    def _run(self, context):
        props = context.scene.nl_design
        current_prompt = props.prompt.strip()
        issues_text = ""
        for loop in range(MAX_REFINE_LOOPS):
            # 1) 执行当前代码
            ok, exec_err = run_sandboxed(self._code)
            if not ok:
                # 执行报错：交给 Agent 修正一次（复用已有自动修正能力）
                self.report({'INFO'}, "第%d轮执行失败，正在修正…" % (loop + 1))
                code2, exp2, err2 = request_code(
                    current_prompt, get_scene_state(),
                    last_error=exec_err, last_code=self._code)
                if err2 or not code2.strip():
                    status = "执行错误且无法修正：%s" % exec_err
                    self.report({'ERROR'}, status)
                    props.last_status = status
                    return {'FINISHED'}
                self._code = code2
                self._explanation = exp2 or self._explanation
                props.last_code = code2
                continue  # 用修正后的代码重新执行

            # 2) 评估：截图 + 场景状态 → 后端判定 pass
            if props.use_screenshot:
                image = capture_view(context)
            else:
                image = None
            passed, issues, eval_err = request_evaluate(
                current_prompt, get_scene_state(), image=image)
            if eval_err:
                # 评估服务不可用：退化为"执行成功即交付"
                self.report({'WARNING'}, "评估不可用，按执行成功交付：" + eval_err)
                status = "成功（未评估）· " + (self._explanation or "")
                props.last_status = status
                self.report({'INFO'}, status)
                _record_history(props, current_prompt, self._code, status)
                return {'FINISHED'}

            if passed:
                status = "智能生成达标 · 第%d轮 · %s" % (loop + 1, self._explanation or "")
                self.report({'INFO'}, status)
                props.last_status = status
                _record_history(props, current_prompt, self._code, status)
                return {'FINISHED'}

            # 3) 不达标：把 issues 作为反馈，让 Agent 重写
            issues_text = "；".join(issues) if issues else "成果与指令不符，请改进"
            self.report({'INFO'}, "第%d轮未达标，自动重写…（%s）" % (loop + 1, issues_text))
            code3, exp3, err3 = request_code(
                current_prompt, get_scene_state(), feedback="请修正以下问题：" + issues_text)
            if err3 or not code3.strip():
                status = "重写失败，最后一轮成果已保留（%s）" % issues_text
                self.report({'WARNING'}, status)
                props.last_status = status
                _record_history(props, current_prompt, self._code, status)
                return {'FINISHED'}
            self._code = code3
            self._explanation = exp3 or self._explanation
            props.last_code = code3

        # 超过最大轮数
        status = "已达最大 %d 轮仍未完全达标，已保留最后成果（问题：%s）" % (
            MAX_REFINE_LOOPS, issues_text or "未知")
        self.report({'WARNING'}, status)
        props.last_status = status
        _record_history(props, current_prompt, self._code, status)
        return {'FINISHED'}


class NLDesign_OT_Render(bpy.types.Operator):
    """一键渲染出图（三点布光+相机+1920 PNG）。采用 Blender 原生后台渲染 job，UI 不阻塞。"""
    bl_idname = "nl_design.render"
    bl_label = "一键渲染出图"
    bl_description = "Blender 原生后台渲染 job（主线程发起、UI 不阻塞，绝不崩溃）"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if nl_shapes is None or not hasattr(nl_shapes, "render_scene"):
            self.report({'ERROR'}, "未加载 shape_library，无法渲染")
            return {'CANCELLED'}
        try:
            # render_scene(start=True) 内部用 INVOKE_DEFAULT 在【主线程】发起后台渲染 job，
            # 立即返回，渲染由 Blender 自己的 job 系统异步执行，UI 完全不卡、绝不崩溃。
            out = nl_shapes.render_scene()
        except Exception as e:  # noqa: BLE001
            self.report({'ERROR'}, "渲染失败：%s" % e)
            print("=== NL render exception ===\n%s" % e)
            return {'CANCELLED'}
        if out:
            status = "已提交后台渲染 -> %s（完成后去该路径查看 PNG）" % out
            self.report({'INFO'}, status)
        else:
            status = "渲染未产出文件（检查控制台）"
            self.report({'WARNING'}, status)
        context.scene.nl_design.last_status = status
        return {'FINISHED'}


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception:
        return False


class NLDesign_OT_Save(bpy.types.Operator):
    """A2：保存工程为 .blend 文件到导出目录。"""
    bl_idname = "nl_design.save"
    bl_label = "保存工程"
    bl_description = "保存 .blend 工程到 Pictures/nl_blender_exports/"
    bl_options = {'REGISTER'}

    def execute(self, context):
        import time as _time
        base = os.path.join(os.path.expanduser("~"), "Pictures", "nl_blender_exports")
        if not _ensure_dir(base):
            self.report({'ERROR'}, "无法创建导出目录")
            return {'CANCELLED'}
        out = os.path.join(base, "scene_%s.blend" % _time.strftime("%Y%m%d_%H%M%S"))
        try:
            bpy.ops.wm.save_as_mainfile(filepath=out)
        except Exception as e:  # noqa: BLE001
            self.report({'ERROR'}, "保存失败：%s" % e)
            return {'CANCELLED'}
        status = "已保存工程 -> %s" % out
        self.report({'INFO'}, status)
        context.scene.nl_design.last_status = status
        return {'FINISHED'}


class NLDesign_OT_Export(bpy.types.Operator):
    """A2：导出当前场景为选择格式（glTF / STL / FBX）。"""
    bl_idname = "nl_design.export"
    bl_label = "导出模型"
    bl_description = "导出为 GLB/STL/FBX（点按钮旁边的小三角选格式）"
    bl_options = {'REGISTER', 'UNDO'}

    fmt: bpy.props.EnumProperty(
        name="格式",
        items=[
            ('GLB', "glTF 2.0 (.glb)", "通用 3D 交换格式，含材质"),
            ('STL', "STL (.stl)", "3D 打印常用"),
            ('FBX', "FBX (.fbx)", "影视/游戏常用"),
        ],
        default='GLB')

    def execute(self, context):
        import time as _time
        base = os.path.join(os.path.expanduser("~"), "Pictures", "nl_blender_exports")
        if not _ensure_dir(base):
            self.report({'ERROR'}, "无法创建导出目录")
            return {'CANCELLED'}
        fname = "model_%s.%s" % (_time.strftime("%Y%m%d_%H%M%S"),
                                 self.fmt.lower() if self.fmt != 'GLB' else 'glb')
        out = os.path.join(base, fname)
        try:
            if self.fmt == 'GLB':
                bpy.ops.export_scene.gltf(filepath=out, use_selection=False)
            elif self.fmt == 'STL':
                bpy.ops.export_mesh.stl(filepath=out, use_selection=False)
            elif self.fmt == 'FBX':
                bpy.ops.export_scene.fbx(filepath=out, use_selection=False)
        except Exception as e:  # noqa: BLE001
            self.report({'ERROR'}, "导出失败：%s" % e)
            return {'CANCELLED'}
        status = "已导出 %s -> %s" % (self.fmt, out)
        self.report({'INFO'}, status)
        context.scene.nl_design.last_status = status
        return {'FINISHED'}


class NLDesign_OT_HistoryRedo(bpy.types.Operator):
    """A3：重跑历史中的某条指令（执行其保存的代码）。"""
    bl_idname = "nl_design.history_redo"
    bl_label = "重跑此指令"
    bl_description = "重跑历史面板里选中那条指令的代码"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        props = context.scene.nl_design
        if self.index < 0 or self.index >= len(props.history):
            self.report({'WARNING'}, "无效的历史项")
            return {'CANCELLED'}
        item = props.history[self.index]
        if not item.code.strip():
            self.report({'WARNING'}, "该历史项无代码")
            return {'CANCELLED'}
        ok, err = run_sandboxed(item.code)
        if ok:
            status = "已重跑：%s" % item.prompt
            self.report({'INFO'}, status)
            props.last_status = status
            props.last_code = item.code
        else:
            status = "重跑执行错误：%s" % err
            self.report({'ERROR'}, status)
            props.last_status = status
        return {'FINISHED'}


class NLDesign_OT_UndoStep(bpy.types.Operator):
    """A3：撤销上一步（Blender 撤销栈）。"""
    bl_idname = "nl_design.undo_step"
    bl_label = "撤销上一步"
    bl_description = "调用 Blender Undo 撤销最近一次生成"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            bpy.ops.ed.undo()
            self.report({'INFO'}, "已撤销上一步")
        except Exception as e:  # noqa: BLE001
            self.report({'WARNING'}, "撤销失败：%s" % e)
            return {'CANCELLED'}
        return {'FINISHED'}


class NLDesign_OT_VoiceInput(bpy.types.Operator):
    """C3：本地语音输入。启动外部 Vosk 语音识别进程，识别文字填入指令框。

    关键安全约束（Blender 线程铁律）：子进程独立运行、不碰 bpy；本插件只在
    主线程用 bpy.app.timers 轮询结果文件，绝不在子线程调用 bpy。
    """
    bl_idname = "nl_design.voice_input"
    bl_label = "语音输入"
    bl_description = "本地离线语音识别（Vosk）：说完后自动填入指令框"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _voice_state
        if _voice_state["active"]:
            self.report({'WARNING'}, "正在聆听中，请稍候")
            return {'CANCELLED'}
        if not os.path.isfile(VOICE_PYTHON):
            self.report({'ERROR'}, "未找到语音 Python 解释器：%s" % VOICE_PYTHON)
            return {'CANCELLED'}
        if not os.path.isfile(VOICE_SCRIPT):
            self.report({'ERROR'}, "未找到语音采集脚本：%s" % VOICE_SCRIPT)
            return {'CANCELLED'}
        if not os.path.isdir(VOICE_MODEL):
            self.report({'ERROR'},
                "未检测到 Vosk 中文模型，请先运行 voice_setup.bat 下载（目录：%s）" % VOICE_MODEL)
            return {'CANCELLED'}
        import tempfile
        out_path = tempfile.mktemp(suffix=".json", prefix="nl_voice_")
        cmd = [VOICE_PYTHON, VOICE_SCRIPT, "--model", VOICE_MODEL,
               "--out", out_path, "--timeout", "15"]
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = 0x08000000  # CREATE_NO_WINDOW：避免弹出黑框
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags)
        except Exception as e:  # noqa: BLE001
            self.report({'ERROR'}, "启动语音识别失败：%s" % e)
            return {'CANCELLED'}
        _voice_state["proc"] = proc
        _voice_state["out"] = out_path
        _voice_state["deadline"] = time.time() + 35  # 含模型加载的总超时
        _voice_state["active"] = True
        context.scene.nl_design.last_status = "🎤 正在聆听…（说完停顿约 1 秒自动停止，最长 15 秒）"
        bpy.app.timers.register(_voice_poll, first_interval=0.3)
        return {'FINISHED'}


# ---------- C3：语音识别结果轮询（主线程 timer，安全调用 bpy） ----------
_voice_state = {"proc": None, "out": None, "deadline": 0.0, "active": False}


def _voice_cleanup():
    global _voice_state
    st = _voice_state
    if st.get("out") and os.path.isfile(st["out"]):
        try:
            os.remove(st["out"])
        except Exception:
            pass
    st["proc"] = None
    st["out"] = None
    st["active"] = False


def _voice_poll():
    """由 bpy.app.timers 在主线程周期调用：读结果文件、安全更新 bpy 状态。"""
    global _voice_state
    st = _voice_state
    if not st["active"]:
        return None
    out_path = st["out"]
    # 1) 结果文件已写出
    if out_path and os.path.isfile(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                result = json.load(f)
        except Exception:
            result = {}
        status = result.get("status")
        text = result.get("text", "").strip()
        props = bpy.context.scene.nl_design
        if status == "ok":
            if text:
                props.prompt = text
                props.last_status = "✅ 语音识别：%s" % text
                if props.auto_submit_voice:
                    try:
                        bpy.ops.nl_design.generate()
                    except Exception as e:  # noqa: BLE001
                        props.last_status = "✅ 已识别（%s），自动生成失败：%s" % (text, e)
            else:
                props.last_status = "🎤 没听清，请重试"
        elif status == "error":
            props.last_status = "⚠️ 语音识别出错：%s" % result.get("error", "")
        _voice_cleanup()
        return None
    # 2) 尚未出结果：检查总超时
    if time.time() > st["deadline"]:
        proc = st["proc"]
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        bpy.context.scene.nl_design.last_status = "⚠️ 语音识别超时"
        _voice_cleanup()
        return None
    return 0.3  # 继续轮询


class NLDesign_PT_Panel(bpy.types.Panel):
    bl_label = "NL Design"
    bl_idname = "VIEW3D_PT_nl_design"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "NL Design"

    def draw(self, context):
        layout = self.layout
        props = context.scene.nl_design
        layout.prop(props, "prompt")
        box = layout.box()
        box.label(text="语音输入（C3 · 本地离线识别）")
        box.operator(NLDesign_OT_VoiceInput.bl_idname, icon='MIC')
        box.prop(props, "auto_submit_voice")
        layout.label(text="创建: 立方体/球/圆柱/圆锥/平面/圆环")
        layout.label(text="造型: 杯子/花瓶/瓶子/齿轮/桌子/椅子/文字")
        layout.label(text="修改: 改成红色 | 动画: 旋转起来 | 阵列: 排成5x5")
        layout.operator(NLDesign_OT_Generate.bl_idname, icon='MESH_CUBE')
        layout.operator(NLDesign_OT_SmartGenerate.bl_idname, icon='SHADERFX')
        layout.separator()
        box = layout.box()
        box.label(text="产品级出图")
        box.label(text="先把物体做好，再点下面渲染")
        box.operator(NLDesign_OT_Render.bl_idname, icon='RENDER_RESULT')
        layout.separator()
        box = layout.box()
        box.label(text="反馈 / 细化（多轮修正）")
        box.prop(props, "feedback", text="")
        row = box.row(align=True)
        row.operator(NLDesign_OT_Refine.bl_idname, icon='FILE_REFRESH')
        row.prop(props, "use_screenshot", text="截图回传", toggle=True)
        layout.separator()
        layout.prop(props, "auto_fix")
        layout.prop(props, "confirm_risky")
        layout.separator()

        # A2 工程保存与导出
        box = layout.box()
        box.label(text="保存 / 导出（A2）")
        box.operator(NLDesign_OT_Save.bl_idname, icon='FILE_TICK')
        row = box.row(align=True)
        row.operator(NLDesign_OT_Export.bl_idname, icon='EXPORT', text="导出(.glb)").fmt = 'GLB'
        row.operator(NLDesign_OT_Export.bl_idname, icon='EXPORT', text="导出(.stl)").fmt = 'STL'
        row.operator(NLDesign_OT_Export.bl_idname, icon='EXPORT', text="导出(.fbx)").fmt = 'FBX'
        layout.separator()

        # A3 历史指令面板
        box = layout.box()
        box.label(text="历史指令（A3，最新在前）")
        box.operator(NLDesign_OT_UndoStep.bl_idname, icon='LOOP_BACK')
        if len(props.history) == 0:
            box.label(text="（暂无记录，先做一次生成）")
        for i, item in enumerate(props.history):
            col = box.column(align=True)
            head = col.row(align=True)
            head.label(text="%s  %s" % (item.time, item.prompt[:18]))
            head.operator(NLDesign_OT_HistoryRedo.bl_idname, icon='PLAY',
                          text="重跑").index = i
        layout.separator()

        layout.label(text="状态: " + props.last_status)


classes = (
    NLDesignHistoryItem, NLDesignProperties, NLDesign_OT_Generate,
    NLDesign_OT_SmartGenerate, NLDesign_OT_Refine, NLDesign_OT_Render,
    NLDesign_OT_Save, NLDesign_OT_Export, NLDesign_OT_HistoryRedo,
    NLDesign_OT_UndoStep, NLDesign_OT_VoiceInput, NLDesign_PT_Panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nl_design = bpy.props.PointerProperty(type=NLDesignProperties)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.nl_design


if __name__ == "__main__":
    register()
