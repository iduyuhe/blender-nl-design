#!/usr/bin/env python3
"""参数化造型库 nl_shapes —— 复杂造型的确定性 bpy 函数集合。

设计目标：让 LLM（或离线模板）调用这些函数，而不是裸写脆弱的建模代码。
所有函数确定性、可重复执行，覆盖「复合造型」而非仅基本体：
  旋转成型（杯子/花瓶/瓶子，bmesh.spin 旋转截面）
  挤压成型（齿轮，bmesh 2D 多边形沿 Z 拉伸）
  组件组合（桌子/椅子，基本体拼装后合并）
  3D 文字（text 对象 + 挤出）

本模块被「后端 agent_server」与「插件 nl_blender_design」共同 import：
  - 后端只用到 SHAPE_CATALOG / build_shape_code（不需要 bpy，已做 defer）。
  - 插件在 Blender 内把本模块注入沙箱 globals，使生成代码可调用 nl_shapes.*。
"""
import math

try:
    import bpy
    import bmesh
except ImportError:  # 后端进程无 bpy，仅用到元数据函数，不报错
    bpy = None
    bmesh = None

# ---------- 颜色表（与插件 COLORS 对齐） ----------
COLOR_TABLE = {
    "红": (0.8, 0.1, 0.1, 1.0), "绿": (0.1, 0.8, 0.1, 1.0),
    "蓝": (0.1, 0.1, 0.8, 1.0), "黄": (0.9, 0.8, 0.1, 1.0),
    "金": (0.85, 0.65, 0.1, 1.0), "白": (0.9, 0.9, 0.9, 1.0),
    "黑": (0.05, 0.05, 0.05, 1.0), "灰": (0.5, 0.5, 0.5, 1.0),
    "橙": (0.9, 0.5, 0.1, 1.0), "紫": (0.6, 0.2, 0.8, 1.0),
}


def parse_color(prompt, default=(0.8, 0.5, 0.2, 1.0)):
    for kw, val in COLOR_TABLE.items():
        if kw in prompt:
            return val
    return default


# ---------- 材质：PBR（金属度/粗糙度）+ 视口色 ----------
def apply_pbr(obj, color=(0.8, 0.5, 0.2, 1.0), metallic=0.1, roughness=0.6,
              name="NLMat"):
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
        bsdf.inputs["Metallic"].default_value = metallic
        bsdf.inputs["Roughness"].default_value = roughness
    mat.diffuse_color = color
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat


# ---------- 旋转成型基础工具：把 2D 截面绕 Y 轴旋转 ----------
def _revolve(profile, segments=64, name="Revolved"):
    """profile: 闭合截面点列表 [(r, y), ...]（r=半径, y=高度），绕 Y 轴旋转成型。"""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    n = len(profile)
    verts = [bm.verts.new((r, y, 0.0)) for r, y in profile]
    for i in range(n):
        j = (i + 1) % n
        bm.verts.ensure_lookup_table()
        bm.edges.new((verts[i], verts[j]))
    bm.verts.ensure_lookup_table()
    geom = bm.verts[:] + bm.edges[:]
    bmesh.ops.spin(bm, geom=geom, axis=(0.0, 1.0, 0.0),
                   angle=2.0 * math.pi, steps=segments, cent=(0.0, 0.0, 0.0))
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ---------- 拉伸成型基础工具：2D 多边形沿 Z 拉伸成棱柱 ----------
def _extrude_polygon(points2d, depth=0.3, name="Prism"):
    """points2d: 闭合多边形点列表 [(x, y), ...]（需从质心星形凸，如齿轮轮廓）。"""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    n = len(points2d)
    top = [bm.verts.new((x, y, depth / 2.0)) for x, y in points2d]
    bot = [bm.verts.new((x, y, -depth / 2.0)) for x, y in points2d]
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((top[i], top[j], bot[j], bot[i]))
    cx = sum(p[0] for p in points2d) / n
    cy = sum(p[1] for p in points2d) / n
    ct = bm.verts.new((cx, cy, depth / 2.0))
    cb = bm.verts.new((cx, cy, -depth / 2.0))
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((ct, top[i], top[j]))
        bm.faces.new((cb, bot[j], bot[i]))
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ---------- 1) 杯子：薄壁截面旋转成型（无布尔、无数据块删除） ----------
def make_cup(outer_radius=1.0, height=2.0, wall=0.12, color=(0.8, 0.5, 0.2, 1.0)):
    ri = max(0.05, outer_radius - wall)
    profile = [
        (ri, 0.0), (outer_radius, 0.0), (outer_radius, height),
        (ri, height), (ri, 0.0),
    ]
    obj = _revolve(profile, segments=64, name="Cup")
    apply_pbr(obj, color=color)
    bpy.context.view_layer.objects.active = obj
    return obj


# ---------- 2) 花瓶：花瓶截面旋转成型 ----------
def make_vase(height=2.5, radius=1.0, color=(0.2, 0.6, 0.8, 1.0)):
    # 经典花瓶轮廓：底窄-腹鼓-颈收-口微张
    profile = [
        (0.45 * radius, 0.0), (0.9 * radius, 0.15 * height),
        (radius, 0.45 * height), (0.75 * radius, 0.75 * height),
        (0.55 * radius, 0.9 * height), (0.62 * radius, height),
        (0.55 * radius, height),
    ]
    obj = _revolve(profile, segments=64, name="Vase")
    apply_pbr(obj, color=color)
    bpy.context.view_layer.objects.active = obj
    return obj


# ---------- 3) 瓶子：瓶身+瓶颈截面旋转成型 ----------
def make_bottle(height=3.0, radius=0.8, color=(0.1, 0.6, 0.2, 1.0)):
    profile = [
        (0.0, 0.0), (radius, 0.0), (radius, 0.7 * height),
        (radius * 0.6, 0.82 * height), (radius * 0.35, 0.95 * height),
        (radius * 0.35, height), (0.0, height),
    ]
    obj = _revolve(profile, segments=64, name="Bottle")
    apply_pbr(obj, color=color)
    bpy.context.view_layer.objects.active = obj
    return obj


# ---------- 4) 齿轮：2D 齿廓拉伸 ----------
def make_gear(teeth=12, outer_radius=1.0, root_radius=0.75, thickness=0.3,
              color=(0.6, 0.6, 0.65, 1.0)):
    pts = []
    ta = 2.0 * math.pi / teeth
    for i in range(teeth):
        a0 = i * ta
        a_ts = a0 + ta * 0.12
        a_te = a0 + ta * 0.38
        a_re = a0 + ta * 0.5
        pts.append((root_radius * math.cos(a0), root_radius * math.sin(a0)))
        pts.append((outer_radius * math.cos(a_ts), outer_radius * math.sin(a_ts)))
        pts.append((outer_radius * math.cos(a_te), outer_radius * math.sin(a_te)))
        pts.append((root_radius * math.cos(a_re), root_radius * math.sin(a_re)))
    obj = _extrude_polygon(pts, depth=thickness, name="Gear")
    apply_pbr(obj, color=color, metallic=0.8, roughness=0.35)
    bpy.context.view_layer.objects.active = obj
    return obj


# ---------- 5) 桌子：桌面 + 四腿组合后合并 ----------
def make_table(width=2.0, depth=1.2, height=0.9, top_thick=0.1,
               leg_radius=0.06, color=(0.55, 0.35, 0.18, 1.0)):
    parts = []
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, height - top_thick / 2))
    top = bpy.context.active_object
    top.scale = (width, depth, top_thick)
    top.name = "TableTop"
    parts.append(top)
    lx, ly = width / 2 - leg_radius * 2, depth / 2 - leg_radius * 2
    leg_h = height - top_thick
    for sx, sy in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=leg_radius, depth=leg_h,
            location=(sx * lx, sy * ly, leg_h / 2))
        parts.append(bpy.context.active_object)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = top
    bpy.ops.object.join()
    apply_pbr(top, color=color)
    bpy.context.view_layer.objects.active = top
    return top


# ---------- 6) 椅子：座面 + 靠背 + 四腿 ----------
def make_chair(seat_h=0.45, seat_w=0.5, seat_d=0.5, color=(0.5, 0.4, 0.3, 1.0)):
    parts = []
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, seat_h))
    seat = bpy.context.active_object
    seat.scale = (seat_w, seat_d, 0.08)
    seat.name = "ChairSeat"
    parts.append(seat)
    back_h = 0.5
    bpy.ops.mesh.primitive_cube_add(
        size=1.0, location=(0, -seat_d / 2, seat_h + back_h / 2))
    back = bpy.context.active_object
    back.scale = (seat_w, 0.08, back_h)
    back.name = "ChairBack"
    parts.append(back)
    leg_r = 0.04
    leg_h = seat_h - 0.08
    lx, ly = seat_w / 2 - leg_r * 2, seat_d / 2 - leg_r * 2
    for sx, sy in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=leg_r, depth=leg_h, location=(sx * lx, sy * ly, leg_h / 2))
        parts.append(bpy.context.active_object)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = seat
    bpy.ops.object.join()
    apply_pbr(seat, color=color)
    bpy.context.view_layer.objects.active = seat
    return seat


# ---------- 7) 3D 文字 ----------
def make_text_3d(text="A", size=1.0, depth=0.2, color=(0.9, 0.9, 0.2, 1.0)):
    bpy.ops.object.text_add(location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.data.body = text
    obj.data.size = size
    obj.data.extrude = depth
    obj.name = "Text3D"
    apply_pbr(obj, color=color)
    bpy.context.view_layer.objects.active = obj
    return obj


# ---------- 8) 建筑类 ----------
def make_house(w=3.0, d=3.0, h=2.5, color=(0.82, 0.75, 0.68, 1.0)):
    parts = []
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, h / 2))
    body = bpy.context.active_object
    body.scale = (w, h, d)
    body.name = "HouseBody"
    parts.append(body)
    bpy.ops.mesh.primitive_cone_add(radius1=w * 0.72, radius2=w * 0.72, depth=h * 0.7,
                                    vertices=4, location=(0, 0, h + h * 0.35))
    roof = bpy.context.active_object
    roof.scale = (1.0, d / w, 1.0)
    roof.rotation_euler = (0.0, 0.0, 0.785)
    roof.name = "HouseRoof"
    parts.append(roof)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    apply_pbr(body, color=color)
    bpy.context.view_layer.objects.active = body
    return body


def make_tower(height=4.0, radius=0.8, tiers=3, color=(0.72, 0.72, 0.78, 1.0)):
    parts = []
    seg_h = height / tiers
    for i in range(tiers):
        r = max(0.2, radius * (1 - i * 0.16))
        bpy.ops.mesh.primitive_cylinder_add(
            radius=r, depth=seg_h * 0.9, location=(0, 0, seg_h * (i + 0.5)))
        parts.append(bpy.context.active_object)
    bpy.ops.mesh.primitive_cone_add(
        radius1=max(0.2, radius * (1 - 0.16 * (tiers - 1)) * 0.9),
        radius2=max(0.2, radius * (1 - 0.16 * (tiers - 1)) * 0.9),
        depth=seg_h, location=(0, 0, height + seg_h * 0.5))
    parts.append(bpy.context.active_object)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    apply_pbr(parts[0], color=color)
    bpy.context.view_layer.objects.active = parts[0]
    return parts[0]


def make_arch(width=2.0, height=3.0, thickness=0.3, color=(0.86, 0.83, 0.78, 1.0)):
    parts = []
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(-width / 2 - thickness / 2, 0, height / 2))
    l = bpy.context.active_object
    l.scale = (thickness, thickness, height)
    l.name = "ArchL"
    parts.append(l)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(width / 2 + thickness / 2, 0, height / 2))
    r = bpy.context.active_object
    r.scale = (thickness, thickness, height)
    r.name = "ArchR"
    parts.append(r)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, height))
    top = bpy.context.active_object
    top.scale = (width + thickness * 2, thickness, thickness)
    top.name = "ArchTop"
    parts.append(top)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = l
    bpy.ops.object.join()
    apply_pbr(l, color=color)
    bpy.context.view_layer.objects.active = l
    return l


# ---------- 9) 机械类 ----------
def make_bolt(radius=0.3, length=1.0, head_h=0.3, color=(0.7, 0.7, 0.75, 1.0)):
    parts = []
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=length, location=(0, 0, length / 2))
    shaft = bpy.context.active_object
    shaft.name = "BoltShaft"
    parts.append(shaft)
    bpy.ops.mesh.primitive_cylinder_add(
        radius=radius * 1.6, depth=head_h, location=(0, 0, length + head_h / 2))
    head = bpy.context.active_object
    head.name = "BoltHead"
    parts.append(head)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = shaft
    bpy.ops.object.join()
    apply_pbr(shaft, color=color, metallic=0.9, roughness=0.3)
    bpy.context.view_layer.objects.active = shaft
    return shaft


def make_bearing(outer=1.0, inner=0.5, thickness=0.4, color=(0.82, 0.82, 0.88, 1.0)):
    bpy.ops.mesh.primitive_torus_add(
        major_radius=outer, minor_radius=thickness / 2, location=(0, 0, 0))
    ring = bpy.context.active_object
    ring.name = "Bearing"
    apply_pbr(ring, color=color, metallic=0.85, roughness=0.35)
    bpy.context.view_layer.objects.active = ring
    return ring


def make_gearbox(w=2.0, h=1.5, d=1.5, color=(0.4, 0.5, 0.6, 1.0)):
    parts = []
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, h / 2))
    box = bpy.context.active_object
    box.scale = (w, h, d)
    box.name = "Gearbox"
    parts.append(box)
    for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=0.12, depth=0.2,
            location=(sx * (w / 2 - 0.3), sy * (d / 2 - 0.3), h + 0.05))
        parts.append(bpy.context.active_object)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = box
    bpy.ops.object.join()
    apply_pbr(box, color=color, metallic=0.6, roughness=0.4)
    bpy.context.view_layer.objects.active = box
    return box


# ---------- 10) 有机体类 ----------
def make_tree(height=3.0, color_trunk=(0.4, 0.28, 0.16, 1.0),
              color_leaf=(0.15, 0.5, 0.2, 1.0)):
    bpy.ops.mesh.primitive_cylinder_add(
        radius=0.2, depth=height * 0.6, location=(0, 0, height * 0.3))
    trunk = bpy.context.active_object
    trunk.name = "TreeTrunk"
    apply_pbr(trunk, color=color_trunk)
    for i in range(3):
        r = 1.2 - i * 0.3
        bpy.ops.mesh.primitive_ico_sphere_add(
            radius=r, location=(0, 0, height * 0.6 + i * 0.7))
        leaf = bpy.context.active_object
        leaf.name = "TreeLeaf%d" % i
        apply_pbr(leaf, color=color_leaf)
    objs = [o for o in bpy.data.objects
            if o.name == "TreeTrunk" or o.name.startswith("TreeLeaf")]
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = trunk
    bpy.ops.object.join()
    bpy.context.view_layer.objects.active = trunk
    return trunk


def make_rock(scale=1.0, color=(0.5, 0.5, 0.52, 1.0)):
    bpy.ops.mesh.primitive_ico_sphere_add(
        radius=scale, subdivisions=1, location=(0, 0, scale * 0.5))
    rock = bpy.context.active_object
    rock.name = "Rock"
    rock.scale = (1.0, 0.8, 0.7)
    apply_pbr(rock, color=color, roughness=0.9, metallic=0.0)
    bpy.context.view_layer.objects.active = rock
    return rock


def make_cloud(scale=1.0, color=(0.95, 0.95, 0.98, 1.0)):
    parts = []
    for dx, dy, r in ((0.0, 0.0, 1.0), (0.8, 0.1, 0.7), (-0.8, 0.1, 0.7),
                      (0.4, 0.4, 0.6)):
        bpy.ops.mesh.primitive_ico_sphere_add(
            radius=r * scale, location=(dx * scale, dy * scale, r * scale * 0.6))
        parts.append(bpy.context.active_object)
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    apply_pbr(parts[0], color=color, roughness=1.0, metallic=0.0)
    bpy.context.view_layer.objects.active = parts[0]
    return parts[0]


# ---------- 材质预设库（B3） ----------
MATERIAL_PRESETS = {
    "木":   {"metallic": 0.0, "roughness": 0.7, "color": (0.55, 0.35, 0.18, 1.0)},
    "金属": {"metallic": 0.9, "roughness": 0.3, "color": (0.7, 0.7, 0.75, 1.0)},
    "玻璃": {"metallic": 0.0, "roughness": 0.05, "color": (0.9, 0.95, 1.0, 1.0),
             "transmission": 0.9},
    "陶瓷": {"metallic": 0.0, "roughness": 0.2, "color": (0.95, 0.95, 0.95, 1.0)},
    "塑料": {"metallic": 0.0, "roughness": 0.5, "color": (0.9, 0.3, 0.3, 1.0)},
    "石材": {"metallic": 0.0, "roughness": 0.9, "color": (0.6, 0.6, 0.6, 1.0)},
    "布料": {"metallic": 0.0, "roughness": 1.0, "color": (0.8, 0.8, 0.85, 1.0)},
    "金":   {"metallic": 1.0, "roughness": 0.2, "color": (0.85, 0.65, 0.1, 1.0)},
    "银":   {"metallic": 1.0, "roughness": 0.25, "color": (0.85, 0.85, 0.9, 1.0)},
}


def apply_material_preset(obj, preset, color=None):
    """按预设名给对象换 PBR 材质（木/金属/玻璃/陶瓷/塑料/石材/布料/金/银）。"""
    info = MATERIAL_PRESETS.get(preset, MATERIAL_PRESETS["金属"])
    c = color if color is not None else info["color"]
    mat = bpy.data.materials.new(name="NL_%s" % preset)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (c[0], c[1], c[2], 1.0)
        bsdf.inputs["Metallic"].default_value = info["metallic"]
        bsdf.inputs["Roughness"].default_value = info["roughness"]
        if "transmission" in info and "Transmission" in bsdf.inputs:
            bsdf.inputs["Transmission"].default_value = info["transmission"]
    mat.diffuse_color = c
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat


# ---------- 环境光预设（B3） ----------
def setup_environment(style="studio"):
    sc = bpy.context.scene
    if style == "outdoor":
        if "NL_Sun" not in bpy.data.objects:
            ld = bpy.data.lights.new("NL_Sun", type='SUN')
            sun = bpy.data.objects.new("NL_Sun", ld)
            bpy.context.collection.objects.link(sun)
        else:
            sun = bpy.data.objects["NL_Sun"]
        sun.location = (5, 5, 10)
        sun.data.energy = 3.0
        if sc.world is None:
            sc.world = bpy.data.worlds.new("NL_World")
        sc.world.use_nodes = True
        try:
            bg = sc.world.node_tree.nodes.get("Background")
            if bg:
                bg.inputs["Color"].default_value = (0.5, 0.7, 1.0, 1.0)
        except Exception:
            pass
        return [sun]
    elif style == "indoor":
        lights = []
        for name, loc, e in (("NL_InA", (-3, 2, 3), 60),
                             ("NL_InB", (3, -2, 3), 40)):
            if name not in bpy.data.objects:
                ld = bpy.data.lights.new(name, type='POINT')
                o = bpy.data.objects.new(name, ld)
                bpy.context.collection.objects.link(o)
            else:
                o = bpy.data.objects[name]
            o.location = loc
            o.data.energy = e
            lights.append(o)
        return lights
    else:  # studio
        return setup_three_point_lighting()


# ---------- 离线/模板代码生成：返回调用 nl_shapes 的代码字符串 ----------
_SHAPE_KEYWORDS = [
    ("杯子", "cup"), ("杯", "cup"),
    ("花瓶", "vase"),
    ("瓶子", "bottle"), ("瓶", "bottle"),
    ("齿轮", "gear"),
    ("桌子", "table"), ("桌", "table"),
    ("椅子", "chair"), ("椅", "chair"),
    ("文字", "text"), ("字母", "text"), ("字", "text"),
    ("小屋", "house"), ("房子", "house"), ("房屋", "house"),
    ("塔", "tower"), ("宝塔", "tower"),
    ("拱门", "arch"), ("拱桥", "arch"),
    ("螺栓", "bolt"), ("螺丝", "bolt"),
    ("轴承", "bearing"),
    ("齿轮箱", "gearbox"), ("变速箱", "gearbox"),
    ("树", "tree"), ("树木", "tree"),
    ("石头", "rock"), ("岩石", "rock"),
    ("云", "cloud"), ("云朵", "cloud"),
]

_EXTRACTORS = {
    "cup": lambda p: "make_cup(color=%s)" % _c(p),
    "vase": lambda p: "make_vase(color=%s)" % _c(p),
    "bottle": lambda p: "make_bottle(color=%s)" % _c(p),
    "gear": lambda p: _gear_args(p),
    "table": lambda p: "make_table(color=%s)" % _c(p),
    "chair": lambda p: "make_chair(color=%s)" % _c(p),
    "text": lambda p: _text_args(p),
    "house": lambda p: "make_house(color=%s)" % _c(p),
    "tower": lambda p: "make_tower(color=%s)" % _c(p),
    "arch": lambda p: "make_arch(color=%s)" % _c(p),
    "bolt": lambda p: "make_bolt(color=%s)" % _c(p),
    "bearing": lambda p: "make_bearing(color=%s)" % _c(p),
    "gearbox": lambda p: "make_gearbox(color=%s)" % _c(p),
    "tree": lambda p: "make_tree()",
    "rock": lambda p: "make_rock()",
    "cloud": lambda p: "make_cloud()",
}


def _c(prompt):
    return str(parse_color(prompt))


def _gear_args(prompt):
    m = re_num(prompt)
    teeth = int(m) if m else 12
    return "make_gear(teeth=%d, color=%s)" % (teeth, _c(prompt))


def _text_args(prompt):
    # 取关键词之后的文字作为内容
    for kw in ("文字", "字母"):
        if kw in prompt:
            txt = prompt.split(kw, 1)[1].strip().strip("\"' ")
            if txt:
                return "make_text_3d(text=%r, color=%s)" % (txt, _c(prompt))
    return "make_text_3d(text='A', color=%s)" % _c(prompt)


def re_num(prompt):
    import re
    m = re.search(r"(\d+)", prompt)
    return m.group(1) if m else None


def build_shape_code(prompt):
    """根据关键词返回调用 nl_shapes 的代码字符串（离线/在线共用同一形态）。"""
    # 最长关键词优先，避免「齿轮」抢先匹配「齿轮箱」等更具体词
    for kw, name in sorted(_SHAPE_KEYWORDS, key=lambda kv: -len(kv[0])):
        if kw in prompt:
            return "import bpy\nnl_shapes.%s" % _EXTRACTORS[name](prompt)
    # 材质预设：选中对象换材质
    for mat_kw in ("木纹", "木质", "木材质", "金属质感", "玻璃材质", "陶瓷材质",
                   "塑料材质", "石材", "布料", "镀金", "银质"):
        if mat_kw in prompt:
            preset = {"木纹": "木", "木质": "木", "木材质": "木", "金属质感": "金属",
                      "玻璃材质": "玻璃", "陶瓷材质": "陶瓷", "塑料材质": "塑料",
                      "石材": "石材", "布料": "布料", "镀金": "金", "银质": "银"}[mat_kw]
            return ("import bpy\n"
                    "obj = bpy.context.active_object or "
                    "[o for o in bpy.data.objects if o.type=='MESH'][0]\n"
                    "nl_shapes.apply_material_preset(obj, %r)" % preset)
    # 环境光预设
    if "室外" in prompt or "户外" in prompt or "自然光" in prompt:
        return "import bpy\nnl_shapes.setup_environment('outdoor')"
    if "室内" in prompt or "居家" in prompt:
        return "import bpy\nnl_shapes.setup_environment('indoor')"
    if "棚拍" in prompt or "影棚" in prompt or "摄影棚" in prompt:
        return "import bpy\nnl_shapes.setup_environment('studio')"
    return None


# ---------- 8) 三点布光：主光 + 补光 + 轮廓光 ----------
def setup_three_point_lighting(look_at=(0, 0, 1), intensity_key=800, intensity_fill=400,
                               intensity_rim=600):
    """新建三点布光（Key/Fill/Rim），自动对准场景中心高度。返回灯光对象列表。"""
    lights = []
    specs = [
        ("NL_Key",  (4.0, -4.0, 6.0),  intensity_key),
        ("NL_Fill", (-5.0, -3.0, 3.0), intensity_fill),
        ("NL_Rim",  (0.0, 6.0, 5.0),   intensity_rim),
    ]
    for name, loc, energy in specs:
        if name in bpy.data.objects:
            obj = bpy.data.objects[name]
        else:
            ldata = bpy.data.lights.new(name=name, type='AREA')
            obj = bpy.data.objects.new(name, ldata)
            bpy.context.collection.objects.link(obj)
        obj.location = loc
        if obj.data:
            obj.data.energy = energy
            obj.data.size = 4.0
        # 对准场景中心高度
        direction = (look_at[0] - loc[0], look_at[1] - loc[1], look_at[2] - loc[2])
        obj.rotation_euler = _look_rotation(direction)
        lights.append(obj)
    return lights


def _look_rotation(direction):
    import math
    dx, dy, dz = direction
    yaw = math.atan2(dx, dy)
    horiz = math.sqrt(dx * dx + dy * dy)
    pitch = math.atan2(horiz, dz)
    return (math.pi / 2 - pitch, 0.0, yaw)


# ---------- 9) 相机：新建/复用并取景整个场景 ----------
def setup_camera_to_object(target=None, distance=8.0, frame=True):
    """新建相机（若已有 NL_Camera 则复用），对准 target（默认全部选中/首个网格），自动取景。
    返回相机对象。"""
    cam_obj = bpy.data.objects.get("NL_Camera")
    if cam_obj is None:
        cdata = bpy.data.cameras.new("NL_Camera")
        cam_obj = bpy.data.objects.new("NL_Camera", cdata)
        bpy.context.collection.objects.link(cam_obj)
        bpy.context.scene.camera = cam_obj
    # 取目标点：优先 target，否则选中对象中心，否则首个网格
    if target is None:
        sel = [o for o in bpy.context.selected_objects if o.type == 'MESH']
        if sel:
            target = sel[0]
        else:
            meshes = [o for o in bpy.data.objects if o.type == 'MESH']
            target = meshes[0] if meshes else None
    center = target.location if target is not None else (0, 0, 1)
    if hasattr(center, 'x'):
        center = (center.x, center.y, center.z)
    cam_obj.location = (center[0] + distance * 0.7, center[1] - distance,
                        center[2] + distance * 0.5)
    cam_obj.rotation_euler = _look_rotation(
        (center[0] - cam_obj.location[0], center[1] - cam_obj.location[1],
         center[2] - cam_obj.location[2]))
    if frame:
        try:
            bpy.context.view_layer.objects.active = cam_obj
            cam_obj.select_set(True)
            bpy.ops.view3d.camera_to_view_selected()
        except Exception:
            pass
    return cam_obj


# ---------- 10) 一键渲染出图 ----------
def render_scene(output_path=None, resolution=1920, engine='CYCLES',
                 samples=64, look_at=(0, 0, 1), start=True):
    """设置 EEVEE/CYCLES 渲染并输出 PNG。

    start=True（默认）：用 bpy.ops.render.render('INVOKE_DEFAULT') 启动 Blender
        原生后台渲染 job —— 在主线程发起、立即返回，渲染由 Blender 自己的 job
        系统异步执行，UI 完全不阻塞且绝不崩溃（绝不可在子线程调 bpy）。
    start=False：只准备场景/输出路径并返回路径，不触发渲染（供自定义流程调用）。

    返回输出文件路径。
    """
    import os
    sc = bpy.context.scene
    # 引擎：优先用用户指定；失败则按版本兼容回退。
    # Blender 4.2+ 把老的 'BLENDER_EEVEE' 改名为 'BLENDER_EEVEE_NEXT'，
    # 因此这里做跨版本稳健回退：指定引擎 -> BLENDER_EEVEE_NEXT -> CYCLES。
    _engine_candidates = [engine, 'BLENDER_EEVEE_NEXT', 'CYCLES']
    _engine_set = False
    for _eng in _engine_candidates:
        try:
            sc.render.engine = _eng
            _engine_set = True
            break
        except Exception:
            continue
    if not _engine_set:
        print("=== NL render: 无可用的渲染引擎，回退失败 ===")
    sc.render.resolution_x = resolution
    sc.render.resolution_y = int(resolution * 9.0 / 16.0)
    sc.render.resolution_percentage = 100
    if hasattr(sc.cycles, 'samples'):
        sc.cycles.samples = samples
    # 布光 + 相机
    setup_three_point_lighting(look_at=look_at)
    setup_camera_to_object(target=None, distance=8.0)
    # 输出路径
    if output_path is None:
        out_dir = os.path.join(os.path.expanduser("~"), "Pictures", "nl_blender_renders")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, "render_%s.png" % _ts())
    sc.render.filepath = output_path
    if not start:
        return output_path
    try:
        # INVOKE_DEFAULT 在【主线程】发起后台渲染 job，立即返回，UI 不阻塞。
        bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
        return output_path
    except Exception as e:
        print("=== NL render failed: %s ===" % e)
        return None


def _ts():
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def build_scene_code(prompt):
    """根据场景意图返回调用 nl_shapes 的代码字符串。"""
    if any(k in prompt for k in ("渲染", "出图", "渲染图", "渲染一张", "拍照", "拍张", "截图成图", "产品图")):
        return "import bpy\nnl_shapes.render_scene()"
    if "布光" in prompt or "灯光" in prompt or "打光" in prompt:
        return ("import bpy\n"
                "nl_shapes.setup_three_point_lighting()\n"
                "nl_shapes.setup_camera_to_object()")
    if "相机" in prompt or "镜头" in prompt or "取景" in prompt:
        return "import bpy\nnl_shapes.setup_camera_to_object()"
    return None


# ---------- 给 LLM 的签约说明（函数签名） ----------
SHAPE_CATALOG = [
    "nl_shapes.make_cup(outer_radius=1.0, height=2.0, wall=0.12, color=(r,g,b,1))  # 杯子",
    "nl_shapes.make_vase(height=2.5, radius=1.0, color=(r,g,b,1))  # 花瓶",
    "nl_shapes.make_bottle(height=3.0, radius=0.8, color=(r,g,b,1))  # 瓶子",
    "nl_shapes.make_gear(teeth=12, outer_radius=1.0, root_radius=0.75, thickness=0.3, color=(r,g,b,1))  # 齿轮",
    "nl_shapes.make_table(width=2.0, depth=1.2, height=0.9, color=(r,g,b,1))  # 桌子",
    "nl_shapes.make_chair(seat_h=0.45, seat_w=0.5, seat_d=0.5, color=(r,g,b,1))  # 椅子",
    "nl_shapes.make_text_3d(text='A', size=1.0, depth=0.2, color=(r,g,b,1))  # 3D 文字",
    "nl_shapes.make_house(w=3.0, d=3.0, h=2.5, color=(r,g,b,1))  # 小屋/房子",
    "nl_shapes.make_tower(height=4.0, radius=0.8, tiers=3, color=(r,g,b,1))  # 塔/宝塔",
    "nl_shapes.make_arch(width=2.0, height=3.0, thickness=0.3, color=(r,g,b,1))  # 拱门/拱桥",
    "nl_shapes.make_bolt(radius=0.3, length=1.0, color=(r,g,b,1))  # 螺栓/螺丝",
    "nl_shapes.make_bearing(outer=1.0, inner=0.5, thickness=0.4, color=(r,g,b,1))  # 轴承",
    "nl_shapes.make_gearbox(w=2.0, h=1.5, d=1.5, color=(r,g,b,1))  # 齿轮箱/变速箱",
    "nl_shapes.make_tree(height=3.0)  # 树/树木",
    "nl_shapes.make_rock(scale=1.0)  # 石头/岩石",
    "nl_shapes.make_cloud(scale=1.0)  # 云/云朵",
]

# 材质/环境预设（B3）：一句话切换质感与灯光
MATERIAL_CATALOG = [
    "nl_shapes.apply_material_preset(obj, '木'|'金属'|'玻璃'|'陶瓷'|'塑料'|'石材'|'布料'|'金'|'银')  # 选中对象换材质",
    "nl_shapes.setup_environment('studio'|'outdoor'|'indoor')  # 影棚/室外/室内灯光",
]

# 场景/渲染类函数签名（供 LLM 在需要时调用，使成果成为产品级渲染图而非彩模）
SCENE_CATALOG = [
    "nl_shapes.render_scene(output_path=None, resolution=1920, engine='CYCLES', samples=64)  # 一键渲染出图（含三点布光+相机取景）",
    "nl_shapes.setup_three_point_lighting()  # 三点布光",
    "nl_shapes.setup_camera_to_object(distance=8.0)  # 新建相机并自动取景场景",
]
