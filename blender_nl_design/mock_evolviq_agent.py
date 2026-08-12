#!/usr/bin/env python3
"""独立的 Blender 设计 Agent 本地服务（自然语言控制 Blender MVP 用）。

本服务是独立系统，不并入 EvolvIQ 核心，避免影响生产平台。
实现一套稳定的 HTTP 契约，使 Blender 插件可离线端到端运行；未来可由 EvolvIQ 编排层以
"独立 agent" 身份接入（契约不变），而非把逻辑塞进 EvolvIQ 内核。

契约：
  POST {base_url}/v1/agent/completion
  body (json): {"prompt": str, "context": {"app": "blender", "scene_state": {...}}}
  resp (json): {"code": str, "explanation": str}

依赖：仅 Python 标准库（http.server / json / re），无需 pip 安装。
运行：python mock_evolviq_agent.py
"""
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "127.0.0.1"
PORT = 8765

# 中文基本体关键词 -> bpy 操作模板（size=整体尺寸, r=半径）
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
    "红": (0.8, 0.1, 0.1, 1.0),
    "绿": (0.1, 0.8, 0.1, 1.0),
    "蓝": (0.1, 0.1, 0.8, 1.0),
    "黄": (0.9, 0.8, 0.1, 1.0),
    "白": (0.9, 0.9, 0.9, 1.0),
    "黑": (0.05, 0.05, 0.05, 1.0),
}


def build_code(prompt):
    """把自然语言指令转换为 bpy 代码字符串（占位用的简单意图匹配）。"""
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/agent/completion":
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        prompt = (data.get("prompt") or "").strip()
        code, explanation = build_code(prompt)
        self._send({"code": code, "explanation": explanation})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("Blender Design Agent -> http://%s:%s/v1/agent/completion" % (HOST, PORT))
    HTTPServer((HOST, PORT), Handler).serve_forever()
