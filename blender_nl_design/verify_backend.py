#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 Python 后端离线回归测试（NL Blender Designer v0.8.0）。

不依赖 Blender / bpy，可在任意 Python 环境直接运行，用于守护「后端生成逻辑」
不退化。覆盖：代码提取 / 离线模板意图分类（16 种造型 + 场景编排 + 材质环境 +
基本体 + 修改 + 动画 + 阵列）/ 评估回退判定 / 离线 LLM 模板生成。

用法（项目目录内）：
  python verify_backend.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import agent_server as ag

# 强制离线：清空 key，避免联网耗额度/超时
ag.CONFIG["api_key"] = ""
ag.CONFIG["vision"] = False


def check(name, cond, detail=""):
    mark = "[PASS]" if cond else "[FAIL]"
    print("  %-46s %s %s" % (name, mark, detail))
    return cond


def test_extract_code():
    print("\n[1] extract_code（从模型回复中提取 python 块）")
    ok = True
    sample = "好的，代码：\n```python\nimport bpy\nbpy.ops.mesh.primitive_cube_add()\n```\n完毕"
    code = ag.extract_code(sample)
    ok &= check("提取首个 python 块", "primitive_cube_add" in code, repr(code[:40]))
    ok &= check("无代码块返回空串", ag.extract_code("没有代码") == "", "应返回空")
    return ok


def test_shape_templates():
    print("\n[2] build_code_template —— 16 种造型关键词命中")
    ok = True
    cases = [
        ("做一个杯子", "make_cup"), ("来个花瓶", "make_vase"),
        ("生成一个蓝色瓶子", "make_bottle"), ("做一个12齿齿轮", "make_gear"),
        ("加张桌子", "make_table"), ("做一把椅子", "make_chair"),
        ("写个文字 工业5.0", "make_text_3d"),
        ("做一个小屋", "make_house"), ("建一座塔", "make_tower"),
        ("加一个拱门", "make_arch"), ("来个螺栓", "make_bolt"),
        ("放个轴承", "make_bearing"), ("做个齿轮箱", "make_gearbox"),
        ("种一棵树", "make_tree"), ("放块石头", "make_rock"),
        ("来朵云", "make_cloud"),
    ]
    for prompt, expect in cases:
        code, _ = ag.build_code_template(prompt)
        hit = expect in code
        ok &= check(prompt, hit, "" if hit else "-> " + code[:50])
    return ok


def test_scene_orchestration():
    print("\n[3] build_code_template —— 场景编排（B2）")
    ok = True
    for prompt in ("摆一套咖啡桌场景", "建一个小院子", "搭一个机械装置", "布置一张餐桌"):
        code, exp = ag.build_code_template(prompt)
        hit = "render_scene" in code and "make_" in code
        ok &= check(prompt, hit, ("" if hit else "-> " + code[:50]))
        if hit:
            print("        %s" % exp)
    # _build_scene_template 直接断言
    sc = ag._build_scene_template("建一个小院子")
    ok &= check("_build_scene_template 含 make_house+setup_environment+render",
                "make_house" in sc and "setup_environment" in sc and "render_scene" in sc)
    return ok


def test_material_env_templates():
    print("\n[4] build_code_template —— 材质/环境（B3）")
    ok = True
    for prompt, expect in (
        ("换成木质", "apply_material_preset"),
        ("改成玻璃材质", "apply_material_preset"),
        ("调到室外灯光", "setup_environment"),
        ("用影棚布光", "setup_environment"),
    ):
        code, _ = ag.build_code_template(prompt)
        ok &= check(prompt, expect in code, "" if expect in code else "-> " + code[:50])
    return ok


def test_basic_intents():
    print("\n[5] build_code_template —— 基本体/修改/动画/阵列")
    ok = True
    ok &= check("基本体 红色立方体", "primitive_cube_add" in ag.build_code_template("创建一个红色立方体")[0])
    ok &= check("修改 改成金色", "active_object" in ag.build_code_template("把它改成金色")[0]
                or "selected_objects" in ag.build_code_template("把它改成金色")[0])
    ok &= check("动画 旋转起来", "keyframe" in ag.build_code_template("让这个物体旋转起来")[0].lower()
                or "rotation" in ag.build_code_template("让这个物体旋转起来")[0].lower())
    ok &= check("阵列 排成5x5", "for " in ag.build_code_template("排成5x5阵列")[0])
    return ok


def test_evaluate_offline():
    print("\n[6] call_evaluate —— 离线评估回退（无 key）")
    ok = True
    # 空场景 + 创建指令 -> 不应通过（没生成网格）
    r = ag.call_evaluate("创建一个红色立方体", scene_state={"mesh_objects": []}, image=None)
    ok &= check("空场景创建指令 -> 不通过",
                r["pass"] is False and bool(r["issues"]), "pass=%s issues=%s" % (r["pass"], r["issues"]))
    # 已有网格 -> 通过
    r2 = ag.call_evaluate("创建一个红色立方体", scene_state={"mesh_objects": [{"name": "Cube"}]}, image=None)
    ok &= check("已有网格 -> 通过", r2["pass"] is True)
    # 阵列意图但仅一个对象 -> 不通过
    r3 = ag.call_evaluate("排成5x5", scene_state={"mesh_objects": [{"name": "a"}]}, image=None)
    ok &= check("阵列意图但仅1个对象 -> 不通过", r3["pass"] is False)
    return ok


def test_llm_offline_template():
    print("\n[7] call_llm —— 离线模板回退（无 key）")
    ok = True
    code, exp = ag.call_llm("做一个白色陶瓷杯子", {"app": "blender", "scene_state": {}})
    ok &= check("离线生成含 make_cup", "make_cup" in code, "" if "make_cup" in code else code[:50])
    ok &= check("返回 explanation 非空", bool(exp), repr(exp[:20]))
    return ok


def test_parse_eval_json():
    print("\n[8] _parse_eval_json —— 评估 JSON 解析容错")
    ok = True
    ok &= check("解析标准 json",
                ag._parse_eval_json('{"pass": true, "issues": ["a"]}') == {"pass": True, "issues": ["a"]})
    ok &= check("解析带噪声文本",
                ag._parse_eval_json('前缀 {"pass": false} 后缀') == {"pass": False, "issues": []})
    r = ag._parse_eval_json("纯文本无json")
    ok &= check("无 json 回退为不通过", r["pass"] is False and bool(r["issues"]))
    return ok


def test_evolviq_report():
    print("\n[9] EvolvIQ 网关可选上报钩子（C2，配置门控）")
    ok = True
    # 默认关闭：enabled=False -> 返回 None，不发送、不报错
    ag.CONFIG["evolviq_report_enabled"] = False
    ok &= check("默认关闭 -> 不触发返回 None",
                ag.report_to_gateway("completion", {"prompt": "x"}) is None)
    # 开启 dry_run -> 返回 payload 且不发网络、不报错
    ag.CONFIG["evolviq_report_enabled"] = True
    ag.CONFIG["evolviq_dry_run"] = True
    ag.CONFIG["evolviq_gateway_url"] = "http://example.invalid/report"
    p = ag.report_to_gateway("completion", {"prompt": "做一个杯子", "ok": True})
    ok &= check("开启 dry_run -> 返回 payload",
                isinstance(p, dict) and p.get("app") == "blender" and p.get("event") == "completion")
    ok &= check("dry_run payload 含透传数据",
                p.get("prompt") == "做一个杯子" and p.get("ok") is True)
    # enabled 但无 url 且非 dry_run -> 安全返回 None，不崩
    ag.CONFIG["evolviq_dry_run"] = False
    ag.CONFIG["evolviq_gateway_url"] = ""
    ok &= check("开启但无 url -> 安全返回 None",
                ag.report_to_gateway("evaluate", {"prompt": "x", "pass": True}) is None)
    # 开启真实（不可达）url -> best-effort，绝不抛异常污染主流程
    ag.CONFIG["evolviq_gateway_url"] = "http://127.0.0.1:9/unreachable"
    try:
        ag.report_to_gateway("completion", {"prompt": "x", "ok": False})
        ok &= check("不可达 url best-effort 不抛异常", True)
    except Exception as e:  # noqa: BLE001
        ok &= check("不可达 url best-effort 不抛异常", False, repr(e))
    # 复位，避免影响其他测试
    ag.CONFIG["evolviq_report_enabled"] = False
    ag.CONFIG["evolviq_dry_run"] = False
    return ok


def main():
    print("=" * 68)
    print("NL Blender Designer v0.9.1 —— 后端离线回归测试（不依赖 Blender）")
    print("=" * 68)
    results = [
        test_extract_code(),
        test_shape_templates(),
        test_scene_orchestration(),
        test_material_env_templates(),
        test_basic_intents(),
        test_evaluate_offline(),
        test_llm_offline_template(),
        test_parse_eval_json(),
        test_evolviq_report(),
    ]
    all_ok = all(results)
    print("\n" + "=" * 68)
    print("总判定: %s" % ("全部通过 ✅" if all_ok else "存在失败 ❌"))
    print("=" * 68)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
