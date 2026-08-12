"""把最新版插件同步到用户 addons 目录（一次性的迁移脚本，不进 addons 系统）。"""
import shutil, os, re

SRC = r"D:\sheji_blend\blender_nl_design"
DST = r"C:\Users\Administrator\AppData\Roaming\Blender Foundation\Blender\4.5\scripts\addons"

for fn in ["nl_blender_design.py", "shape_library.py", "voice_capture.py"]:
    src = os.path.join(SRC, fn)
    dst = os.path.join(DST, fn)
    if not os.path.exists(src):
        print("MISSING SRC:", src); continue
    shutil.copy2(src, dst)
    print(f"copied: {fn} -> {os.path.getsize(dst)} bytes -> {dst}")

print("--- 复核版本号 ---")
for label, f in [("SRC", os.path.join(SRC, "nl_blender_design.py")),
                  ("DST", os.path.join(DST, "nl_blender_design.py"))]:
    s = open(f, encoding="utf-8").read()
    m = re.search(r'"version":\s*\(([^)]+)\)', s)
    print(f"  {label}: {m.group(0) if m else '?'}")

print("--- 用户 addons 目录现有文件 ---")
for f in sorted(os.listdir(DST)):
    print(" ", f)