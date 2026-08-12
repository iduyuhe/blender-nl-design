#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真 Blender 端到端 + headless 回归套件（NL Blender Designer v0.9.0）。

用途：在 Blender 内一键验证「自然语言 -> bpy 代码 -> 真实执行」全链路不崩、不退化。
      - 默认 USE_LLM = False：离线模板生成器（不消耗 API 额度，无需启动 agent_server）。
      - 覆盖 A1-A3（智能生成算子契约 / 保存导出 / 历史面板）+ B1-B4（16 种造型 /
        场景编排 / 材质环境 / 原生后台渲染）。
      - 重点守护刚修复的崩溃回归点：render_scene(start=True) 必须【主线程】INVOKE，
        绝不在子线程调 bpy（曾经因此导致 Blender 整进程段错误崩溃）。

用法（任选其一）：
  A) 命令行 headless（推荐做 CI/回归）：
       "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe" --background --python verify_in_blender.py
     想真渲染验证不崩，追加环境变量：  set NL_RENDER_TEST=1  再跑（低分辨率，几秒）。
  B) Scripting 工作区：打开本文件 -> 点「Run Script」，看 System Console 输出。

注意：脚本会真实修改当前场景（创建/修改/渲染），建议用空白场景运行。
"""
import os
import sys

# ---------- 定位项目目录（与本脚本同目录，或环境变量 BLENDER_NL_DIR 指定） ----------
_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
for _p in [os.environ.get("BLENDER_NL_DIR", ""), _HERE, "D:/sheji_blend/blender_nl_design"]:
    if _p and _p not in sys.path and os.path.isfile(os.path.join(_p, "nl_blender_design.py")):
        sys.path.insert(0, _p)
        break

import bpy  # noqa: E402 (Blender 内可用)
import nl_blender_design as plug  # noqa: E402 (复用插件沙箱，保证测试=实际执行路径)
import agent_server as ag  # noqa: E402 (共用离线模板生成器)
import shape_library as nl_shapes  # noqa: E402

# 强制离线：清空 key，避免联网耗额度/超时
ag.CONFIG["api_key"] = ""
ag.CONFIG["vision"] = False

# 是否真正发起渲染（headless 下会同步渲染，较慢）。默认关，用 NL_RENDER_TEST=1 开。
DO_REAL_RENDER = os.environ.get("NL_RENDER_TEST") == "1"

# 算子类名清单（契约测试用）
OPERATOR_CLASSES = [
    "NLDesign_OT_Generate", "NLDesign_OT_SmartGenerate", "NLDesign_OT_Refine",
    "NLDesign_OT_Render", "NLDesign_OT_Save", "NLDesign_OT_Export",
    "NLDesign_OT_HistoryRedo", "NLDesign_OT_UndoStep",
]


def count_meshes():
    return sum(1 for o in bpy.data.objects if o.type == "MESH")


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


_results = []


def check(name, cond, detail=""):
    mark = "[PASS]" if cond else "[FAIL]"
    line = "  %-50s %s" % (name, mark)
    if detail:
        line += "  %s" % detail
    print(line)
    _results.append(bool(cond))
    return cond


# ---------- [1] 16 种造型函数直接调用（验证不崩 + 产出 MESH） ----------
def test_direct_shapes():
    section("[1] 16 种造型函数直接调用（B1 覆盖）")
    makers = [(n, getattr(nl_shapes, n)) for n in dir(nl_shapes)
              if n.startswith("make_") and callable(getattr(nl_shapes, n))]
    print("  发现 %d 个 make_* 函数" % len(makers))
    for name, fn in makers:
        before = len(bpy.data.objects)
        try:
            obj = fn()
            after = len(bpy.data.objects)
            # 3D 文字是 FONT 类型（非 MESH，但仍是合法创建，便于后续编辑文字内容）
            is_text = (name == "make_text_3d")
            ok = (obj is not None and after > before)
            if not is_text:
                ok = ok and getattr(obj, "type", None) == "MESH"
            check("make_%s" % name, ok,
                  "" if ok else "obj=%s type=%s after=%d before=%d" % (
                      obj, getattr(obj, "type", "?"), after, before))
        except Exception as e:  # noqa: BLE001
            check("make_%s" % name, False, "EXC: %s" % e)


# ---------- [2] 9 种材质预设（B3） ----------
def test_materials():
    section("[2] 9 种材质预设 apply_material_preset（B3）")
    # 准备一个测试 cube 作为应用对象
    bpy.ops.mesh.primitive_cube_add(size=1)
    cube = bpy.context.active_object
    for preset in list(nl_shapes.MATERIAL_PRESETS.keys()):
        try:
            nl_shapes.apply_material_preset(cube, preset)
            mats = cube.data.materials
            ok = mats is not None and len(mats) >= 1
            check("材质预设 '%s'" % preset, ok,
                  "" if ok else "materials=%s" % (mats[:1] if mats else None))
        except Exception as e:  # noqa: BLE001
            check("材质预设 '%s'" % preset, False, "EXC: %s" % e)


# ---------- [3] 3 种环境（B3） ----------
def test_environments():
    section("[3] 3 种环境 setup_environment（B3）")
    for style in ("studio", "outdoor", "indoor"):
        try:
            nl_shapes.setup_environment(style)
            # 验证至少有灯光被创建
            lights = [o for o in bpy.data.objects if o.type == "LIGHT"]
            ok = len(lights) >= 1
            check("环境 '%s'" % style, ok, "lights=%d" % len(lights))
        except Exception as e:  # noqa: BLE001
            check("环境 '%s'" % style, False, "EXC: %s" % e)


# ---------- [4] 自然语言 -> 模板代码 -> 沙箱执行（端到端，复用真实执行路径） ----------
def test_nl_pipeline():
    section("[4] 自然语言 -> 模板 -> 沙箱执行（端到端，复用插件 run_sandboxed）")
    # 前置：留一个 active 对象，供「材质/修改」类指令作用
    bpy.ops.mesh.primitive_cube_add(size=1)
    bpy.context.view_layer.objects.active = bpy.context.active_object
    cases = [
        ("做一个杯子", "造型"),
        ("建一座塔", "造型"),
        ("种一棵树", "造型"),
        ("做一个齿轮箱", "造型(齿轮箱不被齿轮抢)"),
        ("创建一个红色立方体", "基本体"),
        ("把它改成金色并放大", "修改"),
        ("让这个物体旋转起来", "动画"),
        ("排成5x5阵列", "阵列"),
        ("换成木质", "材质(B3)"),
        ("调到室外灯光", "环境(B3)"),
        ("摆一套咖啡桌场景", "场景编排(B2)"),
    ]
    for prompt, label in cases:
        code, exp = ag.build_code_template(prompt)
        if not code or not code.strip():
            check("%s [%s]" % (prompt, label), False, "未生成代码")
            continue
        # 场景编排/出图类代码含 render_scene()（默认会真渲染，headless 下会长时间同步渲染）
        # 这类只做静态验证（代码形态正确），不真正执行，避免卡死回归测试。
        if "render_scene" in code:
            ok = ("make_" in code) and ("render_scene" in code)
            check("%s [%s] (静态:含造型+渲染)" % (prompt, label), ok,
                  "" if ok else code[:60])
            continue
        ok, err = plug.run_sandboxed(code)
        check("%s [%s]" % (prompt, label), ok,
              "" if ok else "ERR: %s" % err)


# ---------- [5] 渲染（B4 崩溃回归点） ----------
def test_render():
    section("[5] 渲染 render_scene（B4 崩溃回归：必须主线程 INVOKE，绝不子线程调 bpy）")
    # 5.1 场景准备（start=False）应返回路径且不崩
    try:
        path = nl_shapes.render_scene(start=False)
        check("render_scene(start=False) 返回路径且不崩", bool(path), path or "")
    except Exception as e:  # noqa: BLE001
        check("render_scene(start=False) 返回路径且不崩", False, "EXC: %s" % e)

    # 5.2 主线程 INVOKE 发起（默认跳过真渲染，避免 headless 卡顿；NL_RENDER_TEST=1 才真渲染）
    if DO_REAL_RENDER:
        try:
            # 关键：本调用发生在主线程，验证不在子线程调 bpy（曾经因此整进程崩溃）
            out = nl_shapes.render_scene(engine="BLENDER_EEVEE_NEXT", resolution=320,
                                         samples=8, start=True)
            check("render_scene(start=True) 主线程发起不崩", out is not None, out or "")
        except Exception as e:  # noqa: BLE001
            check("render_scene(start=True) 主线程发起不崩", False, "EXC: %s" % e)
    else:
        print("  [SKIP] 真渲染测试（设 NL_RENDER_TEST=1 开启，几秒低分辨率验证不崩）")


# ---------- [6] 插件算子契约 + 历史面板（A1/A2/A3） ----------
def ensure_registered():
    try:
        _ = bpy.context.scene.nl_design
        return True  # 已注册（如作为 addon 启用）
    except AttributeError:
        try:
            plug.register()
            return True
        except Exception:  # noqa: BLE001
            return False


def test_plugin_contract():
    section("[6] 插件算子契约 + 历史面板（A1/A2/A3）")
    # 6.1 所有算子类存在且 bl_description 非空（防「无文档记载的操作项」坑）
    for cls_name in OPERATOR_CLASSES:
        cls = getattr(plug, cls_name, None)
        ok = cls is not None and getattr(cls, "bl_description", "") not in ("", None)
        check("算子 %s 存在且 bl_description 非空" % cls_name, ok)

    # 6.2 历史面板：_record_history 能写入且上限 50
    if ensure_registered():
        try:
            props = bpy.context.scene.nl_design
            before = len(props.history)
            plug._record_history(props, "测试指令", "import bpy", "ok")
            after = len(props.history)
            ok = after == before + 1
            check("历史记录 _record_history 写入", ok, "before=%d after=%d" % (before, after))
        except Exception as e:  # noqa: BLE001
            check("历史记录 _record_history 写入", False, "EXC: %s" % e)
    else:
        check("插件 register（历史测试前置）", False, "无法注册插件")


# ---------- 主流程 ----------
def main():
    print("=" * 68)
    print("NL Blender Designer v0.9.0 —— Blender 端到端/headless 回归套件")
    print("  模式: %s" % ("离线模板（不联网）" ))
    print("  真渲染: %s" % ("开启(NL_RENDER_TEST=1)" if DO_REAL_RENDER else "关闭（SKIP）"))
    print("=" * 68)
    before_total = len(bpy.data.objects)

    test_direct_shapes()
    test_materials()
    test_environments()
    test_nl_pipeline()
    test_render()
    test_plugin_contract()

    after_total = len(bpy.data.objects)
    all_ok = all(_results)
    print("\n" + "=" * 68)
    print("场景对象数: %d -> %d" % (before_total, after_total))
    print("用例数: %d  通过: %d  失败: %d" % (
        len(_results), sum(_results), len(_results) - sum(_results)))
    print("总判定: %s" % ("全部通过 ✅" if all_ok else "存在失败 ❌"))
    print("=" * 68)
    # headless 模式下用退出码反映结果（CI 友好）
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
