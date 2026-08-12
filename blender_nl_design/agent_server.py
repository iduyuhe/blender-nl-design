#!/usr/bin/env python3
"""独立的 Blender 设计 Agent 服务（真实 LLM 后端）。

调用云端 LLM（OpenAI / DeepSeek 兼容接口）把自然语言转成 bpy 代码。
与 mock_evolviq_agent.py 共用同一 HTTP 契约，Blender 插件无需改动：
  POST {base_url}/v1/agent/completion
  body: {"prompt": str, "context": {"app": "blender", "scene_state": {...},
         "last_error": str, "last_code": str, "feedback": str, "image": str(base64)}}
  resp: {"code": str, "explanation": str}

三种请求模式（由 context 自动切换）：
  1. 常规生成：仅有 prompt（且意图为「新建」）
  2. 修改已有对象：prompt 含「改/旋转/移动/阵列」等修改类词且场景非空（复用 REFINE_PROMPT）
  3. 自动修正：带上 last_error + last_code（执行报错后由插件重试）
  4. 反馈细化：带上 feedback（用户自然语言修改指令）

配置（优先级：环境变量 > 同目录 config.json）：
  BLENDER_AGENT_API_KEY / BLENDER_AGENT_BASE_URL / BLENDER_AGENT_MODEL / BLENDER_AGENT_VISION
  config.json 示例：{"api_key": "...", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "vision": false}
  缺少 api_key（或调用失败）时自动回退到离线模板生成器，保证永远可用。

依赖：仅 Python 标准库。运行：python agent_server.py
"""
import json
import os
import re
import base64
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

# 参数化造型库：复杂造型确定性函数（被插件与后端共用；后端进程无 bpy 也能 import）
try:
    import shape_library as nl_shapes
    _HAS_SHAPES = True
except Exception:
    nl_shapes = None
    _HAS_SHAPES = False

HOST = "127.0.0.1"
PORT = 8765

# ---------- 配置读取 ----------
def load_config():
    cfg = {
        "api_key": os.environ.get("BLENDER_AGENT_API_KEY", ""),
        "base_url": os.environ.get("BLENDER_AGENT_BASE_URL", "https://api.deepseek.com/v1"),
        "model": os.environ.get("BLENDER_AGENT_MODEL", "deepseek-chat"),
        "endpoint": "/chat/completions",
        "vision": os.environ.get("BLENDER_AGENT_VISION", "false").lower() in ("1", "true", "yes"),
        # C2: EvolvIQ 网关可选上报钩子（默认关闭，不耦合内核）
        "evolviq_report_enabled": os.environ.get("BLENDER_AGENT_EVOLVIQ_REPORT", "false").lower() in ("1", "true", "yes"),
        "evolviq_gateway_url": os.environ.get("BLENDER_AGENT_EVOLVIQ_GATEWAY", ""),
        "evolviq_api_key": os.environ.get("BLENDER_AGENT_EVOLVIQ_KEY", ""),
        "evolviq_dry_run": os.environ.get("BLENDER_AGENT_EVOLVIQ_DRYRUN", "false").lower() in ("1", "true", "yes"),
    }
    try:
        with open(os.path.join(os.path.dirname(__file__), "config.json"), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
        if isinstance(cfg.get("vision"), str):
            cfg["vision"] = cfg["vision"].lower() in ("1", "true", "yes")
        if isinstance(cfg.get("evolviq_report_enabled"), str):
            cfg["evolviq_report_enabled"] = cfg["evolviq_report_enabled"].lower() in ("1", "true", "yes")
        if isinstance(cfg.get("evolviq_dry_run"), str):
            cfg["evolviq_dry_run"] = cfg["evolviq_dry_run"].lower() in ("1", "true", "yes")
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cfg

CONFIG = load_config()


# ---------- C2: EvolvIQ 网关可选上报钩子 ----------
def report_to_gateway(event, data=None):
    """可选上报到 EvolvIQ 网关（配置门控、best-effort、绝不污染主流程）。

    默认关闭（evolviq_report_enabled=false）；开启后每次 completion/evaluate
    成功后发送一条 usage 记录。dry_run 模式只打印 payload 不发网络，便于测试。
    未配置网关地址、或上报失败均被静默吞掉，主服务不受影响。
    """
    import time
    if not CONFIG.get("evolviq_report_enabled"):
        return None
    payload = {"app": "blender", "event": event, "ts": time.time()}
    if data:
        payload.update(data)
    url = CONFIG.get("evolviq_gateway_url") or ""
    key = CONFIG.get("evolviq_api_key") or ""
    if CONFIG.get("evolviq_dry_run"):
        print("[EvolvIQ report dry-run] ->", url, json.dumps(payload, ensure_ascii=False))
        return payload
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as _resp:
            _ = _resp.read()
    except Exception:
        # 上报失败不影响主服务
        pass
    return payload


SYSTEM_PROMPT = (
    "你是一名 Blender Python (bpy) 专家。用户用自然语言描述想在 Blender 中创建或修改的内容。"
    "只输出一段可直接在 Blender 内置 Python 中执行的 bpy 代码，用 ```python 代码块包裹，"
    "不要输出任何解释性文字、不要输出代码块之外的内容。代码要求："
    "以 import bpy 开头；"
    "创建基本体优先使用 bpy.ops.mesh.primitive_*_add；"
    "修改/操作场景中已有对象时，使用 bpy.context.active_object、bpy.context.selected_objects "
    "或 bpy.data.objects['名称']，不要重复创建已有对象；"
    "材质使用 bpy.data.materials.new 后赋值给 obj.data.materials；"
    "动画使用 obj.keyframe_insert(data_path='location' 或 'rotation_euler', frame=...) 插入关键帧，"
    "并设置 bpy.context.scene.frame_start / frame_end；"
    "阵列/多个复制：循环中使用 bpy.ops.object.duplicate() 并设置副本 location 偏移，"
    "或直接添加多个基本体并设置不同 location；"
    "复杂造型（杯子/花瓶/瓶子/齿轮/桌子/椅子/3D文字）请优先调用 nl_shapes 库的确定性函数，"
    "不要裸写脆弱建模代码。nl_shapes 可用函数签名：\n"
    + ("\n".join("  " + line for line in nl_shapes.SHAPE_CATALOG) if _HAS_SHAPES else "")
    + "\n调用示例：import bpy\nnl_shapes.make_cup(color=(0.8,0.1,0.1,1))\n"
    "make_* 函数会自动创建对象、赋 PBR 材质并设为选中对象。\n"
    "出图/渲染：当用户希望得到最终渲染图（而非仅建模）时，调用 "
    "nl_shapes.render_scene() 做三点布光+相机取景+一键渲染；"
    "或单独调用 nl_shapes.setup_three_point_lighting() / setup_camera_to_object()。"
    "场景函数签名：\n"
    + ("\n".join("  " + line for line in nl_shapes.SCENE_CATALOG) if _HAS_SHAPES else "")
    + "\n"
    "【场景编排】当用户希望一次性排布一套多对象场景（例如\"摆一套咖啡桌场景\""
    "\"建一个小院子\"\"布置餐桌\"\"搭一个机械装置\"），应生成多行代码："
    "依次调用多个 nl_shapes.make_* 函数，并通过给对象设置 .location 排布位置，"
    "必要时调用 nl_shapes.setup_environment('outdoor'/'indoor'/'studio') 设定灯光，"
    "最后调用 nl_shapes.render_scene() 出图。材质预设可调用 "
    "nl_shapes.apply_material_preset(obj, '木'/'金属'/...)。"
    + ("\n材质/环境预设：\n" + "\n".join("  " + line for line in nl_shapes.MATERIAL_CATALOG) if _HAS_SHAPES else "")
    + "\n确保语法正确、安全、可重复执行。"
)

FIX_PROMPT = (
    "你是一名 Blender Python (bpy) 调试专家。下面这段之前生成的代码执行时报错了。"
    "请修正错误，只输出修正后的【完整】python 代码块（用 ```python 包裹），不要任何解释。"
    "保留用户原始意图，仅修复导致报错的问题。"
)

REFINE_PROMPT = (
    "你是一名 Blender Python (bpy) 专家。用户希望基于当前场景，按反馈/修改指令做【增量修改】。"
    "只输出一段可执行的 python 代码块（用 ```python 包裹），不要任何解释。"
    "代码应作用于场景中的已有对象（如 bpy.data.objects['...'] 或 bpy.context.active_object / "
    "bpy.context.selected_objects），不要重复创建已有对象。"
    "支持的能力：修改材质颜色、缩放、移动位置、旋转；为对象添加关键帧动画（旋转/位移）；"
    "基于选中对象做阵列复制。确保语法正确、安全、可重复执行。"
)

# ---------- 视觉反馈闭环：评估阶段提示词 ----------
EVAL_PROMPT = (
    "你是一名严格的 Blender 场景质检专家。下面给出用户的原始指令、当前场景状态（结构化 JSON）"
    "以及可选的 3D 视图截图。请判断：刚才生成的成果是否【已经满足】用户的指令。"
    "输出一个紧凑的 JSON，格式固定为：\n"
    "{\"pass\": true/false, \"issues\": [\"问题1\", \"问题2\"]}\n"
    "规则：\n"
    "1. 若成果明显满足指令（如指令要红色立方体、场景里确有红色立方体），pass 设为 true，issues 为空数组。\n"
    "2. 若不满足（缺对象/数量不对/颜色不符/位置错/比例失调/明显穿模等），pass 设为 false，"
    "并在 issues 中写出【具体、可执行】的修改建议（例如\"把立方体改成红色\"\"补一个把手\"\"放大一倍\"），"
    "不要写空泛的评语。\n"
    "3. 仅输出这个 JSON，不要任何额外文字或代码块。\n"
    "注意：scene_state.mesh_objects 已列出当前所有网格对象的名称/位置/缩放，请优先据此判断。"
)

# ---------- 自然语言意图分类（决定走「常规生成」还是「修改已有对象」） ----------
CREATE_KEYWORDS = ["创建", "生成", "新建", "加个", "做一个", "来个", "建一个", "画一个", "添加"]
MODIFY_KEYWORDS = ["改", "变成", "设为", "调整", "移动", "旋转", "缩放", "放大", "缩小",
                   "调成", "换成", "给", "让它", "把它", "删", "去掉", "复制", "阵列", "排成"]


def _is_modify(prompt, scene_state):
    """prompt 是否意在修改/操作场景已有对象（而非新建）。"""
    if any(k in prompt for k in CREATE_KEYWORDS):
        return False
    if scene_state and scene_state.get("mesh_objects"):
        if any(k in prompt for k in MODIFY_KEYWORDS):
            return True
    return False


# ---------- 离线模板生成器（无 key / 调用失败时回退） ----------
PRIMITIVES = {
    "立方体": "bpy.ops.mesh.primitive_cube_add(size={size})",
    "方块": "bpy.ops.mesh.primitive_cube_add(size={size})",
    "球": "bpy.ops.mesh.primitive_uv_sphere_add(radius={r})",
    "球体": "bpy.ops.mesh.primitive_uv_sphere_add(radius={r})",
    "圆柱": "bpy.ops.mesh.primitive_cylinder_add(radius={r}, depth={size})",
    "圆柱体": "bpy.ops.mesh.primitive_cylinder_add(radius={r}, depth={size})",
    "圆锥": "bpy.ops.mesh.primitive_cone_add(radius1={r}, depth={size})",
    "平面": "bpy.ops.mesh.primitive_plane_add(size={size})",
    "圆环": "bpy.ops.mesh.primitive_torus_add(major_radius={r})",
}
COLORS = {
    "红": (0.8, 0.1, 0.1, 1.0), "绿": (0.1, 0.8, 0.1, 1.0),
    "蓝": (0.1, 0.1, 0.8, 1.0), "黄": (0.9, 0.8, 0.1, 1.0),
    "白": (0.9, 0.9, 0.9, 1.0), "黑": (0.05, 0.05, 0.05, 1.0),
}

_ANIM_KEYWORDS = ("动画", "旋转起来", "动起来", "关键帧", "转圈", "位移动画", "旋转一圈", "自转")
_ARRAY_KEYWORDS = ("阵列", "排列成", "排成", "网格", "一排", "克隆")


def _looks_create(prompt):
    return any(k in prompt for k in CREATE_KEYWORDS)


def _looks_modify(prompt):
    return any(k in prompt for k in MODIFY_KEYWORDS)


def _build_modify_template(prompt):
    lines = [
        "import bpy",
        "obj = bpy.context.active_object",
        "if obj is None:",
        "    raise RuntimeError('请先在 3D 视图中选中要修改的对象')",
    ]
    for kw, val in COLORS.items():
        if kw in prompt:
            rr, gg, bb, aa = val
            lines += [
                "mat = bpy.data.materials.new(name='NLMat')",
                "mat.diffuse_color = ({rr}, {gg}, {bb}, {aa})".format(rr=rr, gg=gg, bb=bb, aa=aa),
                "if obj.data.materials:",
                "    obj.data.materials[0] = mat",
                "else:",
                "    obj.data.materials.append(mat)",
            ]
            break
    if any(k in prompt for k in ("大", "放大", "放大一倍")):
        lines.append("obj.scale = (obj.scale[0]*2, obj.scale[1]*2, obj.scale[2]*2)")
    elif any(k in prompt for k in ("小", "缩小", "缩小一半")):
        lines.append("obj.scale = (obj.scale[0]/2, obj.scale[1]/2, obj.scale[2]/2)")
    if "右" in prompt:
        lines.append("obj.location.x += 2")
    elif "左" in prompt:
        lines.append("obj.location.x -= 2")
    elif "上" in prompt:
        lines.append("obj.location.z += 2")
    elif "下" in prompt:
        lines.append("obj.location.z -= 2")
    elif "前" in prompt:
        lines.append("obj.location.y += 2")
    elif "后" in prompt:
        lines.append("obj.location.y -= 2")
    return "\n".join(lines)


def _build_animation_template(prompt):
    if any(k in prompt for k in ("旋转", "转圈", "自转", "转一")):
        return "\n".join([
            "import bpy",
            "obj = bpy.context.active_object",
            "if obj is None:",
            "    bpy.ops.mesh.primitive_cube_add(size=2)",
            "    obj = bpy.context.active_object",
            "sc = bpy.context.scene",
            "sc.frame_start = 1",
            "sc.frame_end = 100",
            "obj.rotation_euler = (0, 0, 0)",
            "obj.keyframe_insert(data_path='rotation_euler', frame=1)",
            "obj.rotation_euler = (0, 0, 6.2832)",
            "obj.keyframe_insert(data_path='rotation_euler', frame=100)",
        ])
    return "\n".join([
        "import bpy",
        "obj = bpy.context.active_object",
        "if obj is None:",
        "    bpy.ops.mesh.primitive_cube_add(size=2)",
        "    obj = bpy.context.active_object",
        "sc = bpy.context.scene",
        "sc.frame_start = 1",
        "sc.frame_end = 100",
        "obj.location = (0, 0, 0)",
        "obj.keyframe_insert(data_path='location', frame=1)",
        "obj.location = (5, 0, 0)",
        "obj.keyframe_insert(data_path='location', frame=100)",
    ])


def _build_array_template(prompt):
    m = re.search(r"(\d+)\s*[x×*]\s*(\d+)", prompt)
    if m:
        cols, rows = int(m.group(1)), int(m.group(2))
    else:
        m2 = re.search(r"(\d+)\s*个", prompt)
        n = int(m2.group(1)) if m2 else 9
        cols = rows = max(1, int(round(n ** 0.5)))
        if cols * rows < n:
            cols += 1
    spacing = 2.0
    return "\n".join([
        "import bpy",
        "src = bpy.context.active_object",
        "if src is None:",
        "    bpy.ops.mesh.primitive_cube_add(size=1)",
        "    src = bpy.context.active_object",
        "cols, rows = %d, %d" % (cols, rows),
        "spacing = %s" % spacing,
        "for i in range(cols):",
        "    for j in range(rows):",
        "        if i == 0 and j == 0:",
        "            continue",
        "        bpy.ops.object.duplicate()",
        "        new = bpy.context.active_object",
        "        new.location = (i*spacing, j*spacing, 0)",
    ])


def _looks_scene(prompt):
    SCENE_KEYWORDS = ["场景", "院子", "庭院", "餐桌", "咖啡桌", "布置", "摆一套",
                      "摆一个", "搭一个", "搭一套", "机械装置", "景观", "房间", "工作室"]
    return any(k in prompt for k in SCENE_KEYWORDS)


def _build_scene_template(prompt):
    """多对象场景编排：识别场景类型，组合多个 make_* + 灯光 + 出图。"""
    lines = ["import bpy", ""]
    if any(k in prompt for k in ("咖啡桌", "餐桌", "餐桌", "饭桌", "摆一套", "摆一个")):
        lines += [
            "# 咖啡桌场景：桌子 + 椅子 + 杯子",
            "t = nl_shapes.make_table(width=2.0, depth=1.2, height=0.9, color=(0.55,0.35,0.18,1))",
            "t.location = (0, 0, 0)",
            "for i, (sx, sy) in enumerate([(-1,-1),(1,-1),(-1,1),(1,1)]):",
            "    c = nl_shapes.make_chair(seat_h=0.45, color=(0.5,0.4,0.3,1))",
            "    c.location = (sx*1.6, sy*1.0, 0)",
            "cup = nl_shapes.make_cup(color=(0.9,0.9,0.95,1))",
            "cup.location = (0, 0, 0.95)",
        ]
    elif any(k in prompt for k in ("院子", "庭院", "景观", "小院")):
        lines += [
            "# 小院场景：房子 + 树 + 石头",
            "h = nl_shapes.make_house(color=(0.82,0.75,0.68,1))",
            "h.location = (-2, 0, 0)",
            "tr = nl_shapes.make_tree(height=3.0)",
            "tr.location = (2, 1, 0)",
            "rk = nl_shapes.make_rock(scale=0.8)",
            "rk.location = (2, -1.5, 0)",
        ]
    elif any(k in prompt for k in ("机械装置", "机械", "齿轮箱", "变速箱")):
        lines += [
            "# 机械装置：齿轮箱 + 螺栓 + 轴承",
            "g = nl_shapes.make_gearbox(color=(0.4,0.5,0.6,1))",
            "g.location = (0, 0, 0)",
            "b = nl_shapes.make_bolt(color=(0.7,0.7,0.75,1))",
            "b.location = (1.5, 0, 0.3)",
            "be = nl_shapes.make_bearing(color=(0.82,0.82,0.88,1))",
            "be.location = (-1.5, 0, 0.3)",
        ]
    else:
        # 默认通用场景：桌 + 椅 + 基本体，体现编排能力
        lines += [
            "# 通用场景：桌子 + 椅子 + 装饰球",
            "t = nl_shapes.make_table(color=(0.6,0.4,0.25,1))",
            "t.location = (0, 0, 0)",
            "c = nl_shapes.make_chair(color=(0.5,0.4,0.3,1))",
            "c.location = (0, 1.1, 0)",
            "bpy.ops.mesh.primitive_ico_sphere_add(radius=0.4, location=(0,0,1.0))",
        ]
    env = "outdoor" if any(k in prompt for k in (" outdoors", "院子", "庭院", "景观", "户外", "室外")) else "studio"
    lines += [
        "",
        "nl_shapes.setup_environment(%r)" % env,
        "nl_shapes.render_scene(engine='CYCLES', samples=64)",
    ]
    return "\n".join(lines)


def build_code_template(prompt):
    """离线模板生成器：覆盖 场景编排 / 复杂造型 / 场景渲染 / 动画 / 阵列 / 修改现有对象 / 基本体。"""
    # 0) 场景编排（多对象一键布场，优先级最高）
    if _HAS_SHAPES and _looks_scene(prompt):
        return _build_scene_template(prompt), "已生成「场景编排」代码（多对象+灯光+出图）"
    # 0.5) 复杂造型（nl_shapes 确定性函数，离线/在线共用形态）
    if _HAS_SHAPES:
        shape_code = nl_shapes.build_shape_code(prompt)
        if shape_code:
            return shape_code, "已生成「复杂造型」代码（nl_shapes 参数化函数）"
    # 0.5) 场景渲染 / 布光 / 相机（nl_shapes 确定性函数）
        scene_code = nl_shapes.build_scene_code(prompt)
        if scene_code:
            return scene_code, "已生成「场景渲染」代码（nl_shapes 布光+相机+出图）"
    # 1) 动画（优先：旋转/让它 等词也属 MODIFY，需先于修改判断）
    if any(k in prompt for k in _ANIM_KEYWORDS):
        return _build_animation_template(prompt), "已生成「动画」代码"
    # 2) 阵列 / 多个复制
    if any(k in prompt for k in _ARRAY_KEYWORDS) or ("复制" in prompt and any(c.isdigit() for c in prompt)):
        return _build_array_template(prompt), "已生成「阵列」代码"
    # 3) 修改现有对象
    if _looks_modify(prompt) and not _looks_create(prompt):
        return _build_modify_template(prompt), "已生成「修改现有对象」代码"
    # 4) 基本体（原逻辑）
    size = 2.0
    sm = re.search(r"(?:尺寸|大小|半径)\s*(\d+(?:\.\d+)?)", prompt)
    if sm:
        size = float(sm.group(1))
    r = size / 2.0
    prim = None
    for kw, tpl in PRIMITIVES.items():
        if kw in prompt:
            prim = tpl.format(size=size, r=r)
            break
    if prim is None:
        return (
            "import bpy\n"
            "# 未识别基本体：请在指令中指定 立方体/球/圆柱/圆锥/平面/圆环\n",
            "未识别到已知基本体，返回占位代码",
        )
    lines = ["import bpy", prim, "obj = bpy.context.active_object"]
    color = None
    for kw, val in COLORS.items():
        if kw in prompt:
            color = val
            break
    if color:
        rr, gg, bb, aa = color
        lines.append("mat = bpy.data.materials.new(name='NLMat')")
        lines.append("mat.diffuse_color = ({rr}, {gg}, {bb}, {aa})".format(
            rr=rr, gg=gg, bb=bb, aa=aa))
        lines.append("if obj is not None:")
        lines.append("    if obj.data.materials:")
        lines.append("        obj.data.materials[0] = mat")
        lines.append("    else:")
        lines.append("        obj.data.materials.append(mat)")
    note = "并着色" if color else ""
    return "\n".join(lines), "已生成基本体%s代码" % note


def extract_code(text):
    """只截取首个 ```python 代码块；无围栏时从 import bpy / bpy. 起截取。"""
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    idx = text.find("import bpy")
    if idx == -1:
        idx = text.find("bpy.")
    if idx != -1:
        return text[idx:].strip()
    # 没有任何代码特征：返回空串，避免把自然语言整段当代码执行
    return ""


def _build_messages(prompt, context):
    ctx = context or {}
    last_error = ctx.get("last_error")
    last_code = ctx.get("last_code")
    feedback = ctx.get("feedback")
    image = ctx.get("image")
    scene_state = ctx.get("scene_state")
    mode = ctx.get("mode")  # evaluate 走评估模式

    if mode == "evaluate":
        sys_p = EVAL_PROMPT
        user_text = (
            "用户原始指令：{prompt}\n"
            "当前场景状态：{scene}\n"
            "请输出质检 JSON（{\"pass\": bool, \"issues\": [...]})。"
        ).format(prompt=prompt, scene=json.dumps(scene_state, ensure_ascii=False))
        if image and CONFIG.get("vision"):
            user_content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + image}},
            ]
        else:
            user_content = user_text
        return [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": user_content},
        ]

    if last_error and last_code:
        sys_p = FIX_PROMPT
        user_text = (
            "之前的代码：\n```python\n{code}\n```\n"
            "执行报错：\n{err}\n"
            "当前场景状态：{scene}\n"
            "请修正后只输出修正后的完整 python 代码块。"
        ).format(code=last_code, err=last_error,
                 scene=json.dumps(scene_state, ensure_ascii=False))
    elif feedback or _is_modify(prompt, scene_state):
        sys_p = REFINE_PROMPT
        fb = feedback or prompt
        user_text = (
            "当前场景状态：{scene}\n"
            "用户反馈/修改指令：{fb}\n"
            "请只输出修改后的 python 代码块（作用于场景已有对象，不要重复创建）。"
        ).format(scene=json.dumps(scene_state, ensure_ascii=False), fb=fb)
    else:
        sys_p = SYSTEM_PROMPT
        user_text = prompt

    if image and CONFIG.get("vision"):
        user_content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + image}},
        ]
    else:
        user_content = user_text

    return [
        {"role": "system", "content": sys_p},
        {"role": "user", "content": user_content},
    ]


def call_llm(prompt, context=None):
    """返回 (code, explanation)。无 key 或调用失败时回退离线模板。"""
    if not CONFIG.get("api_key"):
        code, exp = build_code_template(prompt)
        return code, exp or "（离线模板生成）"
    url = CONFIG["base_url"].rstrip("/") + CONFIG["endpoint"]
    payload = {
        "model": CONFIG["model"],
        "messages": _build_messages(prompt, context),
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + CONFIG["api_key"],
    })
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        content = j["choices"][0]["message"]["content"]
        return extract_code(content), ""
    except Exception as e:  # noqa: BLE001 - 失败时回退，保证可用
        code, exp = build_code_template(prompt)
        return code, "LLM 调用失败(%s)，已回退模板生成" % e


def _evaluate_offline(prompt, scene_state, image):
    """无 key 或视觉不可用时的确定性回退评估：仅按场景状态做最基本达标判断。"""
    meshes = (scene_state or {}).get("mesh_objects") or []
    if not meshes:
        return {"pass": False, "issues": ["场景中没有网格对象，似乎什么都没生成"]}
    # 基本体/造型类指令：至少应有一个对象
    if any(k in prompt for k in ["阵列", "排成", "几个", "多个"]):
        if len(meshes) < 2:
            return {"pass": False, "issues": ["指令要求多个对象，但场景里只有 %d 个" % len(meshes)]}
    return {"pass": True, "issues": []}


def call_evaluate(prompt, scene_state=None, image=None):
    """返回 {"pass": bool, "issues": [str]}。有 key 时让 LLM 基于场景状态 JSON
    （+可选视觉图，需 vision=true）做质检；无 key / 调用失败则离线判定。"""
    if not CONFIG.get("api_key"):
        return _evaluate_offline(prompt, scene_state, image)
    # 有 key：统一走 LLM 质检（是否附带视觉图由 _build_messages 内部按 vision 门控）。
    try:
        url = CONFIG["base_url"].rstrip("/") + CONFIG["endpoint"]
        payload = {
            "model": CONFIG["model"],
            "messages": _build_messages(prompt, {
                "mode": "evaluate", "scene_state": scene_state, "image": image}),
            "temperature": 0.1,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + CONFIG["api_key"],
        })
        with urllib.request.urlopen(req, timeout=40) as resp:
            content = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
        return _parse_eval_json(content)
    except Exception:
        return _evaluate_offline(prompt, scene_state, image)


def _parse_eval_json(text):
    """从模型返回文本里抽取 {pass, issues} JSON；解析失败则保守判为不通过。"""
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            obj = json.loads(m.group(0))
            return {
                "pass": bool(obj.get("pass", False)),
                "issues": list(obj.get("issues", []) or []),
            }
    except Exception:
        pass
    return {"pass": False, "issues": ["无法解析评估结果，请检查生成内容"]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path not in ("/v1/agent/completion", "/v1/agent/evaluate"):
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        prompt = (data.get("prompt") or "").strip()
        context = data.get("context") or {}
        if path == "/v1/agent/evaluate":
            result = call_evaluate(
                prompt,
                scene_state=context.get("scene_state"),
                image=context.get("image"),
            )
            self._send(result)
            try:
                report_to_gateway("evaluate", {"prompt": prompt, "pass": result.get("pass")})
            except Exception:
                pass
            return
        code, explanation = call_llm(prompt, context)
        try:
            report_to_gateway("completion", {
                "prompt": prompt,
                "ok": bool(code),
                "explanation": explanation,
            })
        except Exception:
            pass
        self._send({"code": code, "explanation": explanation})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("Blender Design Agent (LLM) -> http://%s:%s/v1/agent/completion" % (HOST, PORT))
    print("model: %s | base_url: %s | vision: %s" % (
        CONFIG["model"], CONFIG["base_url"], CONFIG.get("vision")))
    # ThreadingHTTPServer：单线程 HTTPServer 会在慢 LLM 请求期间阻塞后续请求
    # （重试/并发点击会排队），改为多线程，避免「点击无响应」被放大。
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
