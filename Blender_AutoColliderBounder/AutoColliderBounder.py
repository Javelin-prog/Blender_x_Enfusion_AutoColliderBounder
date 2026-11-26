"""Auto Collider Bounder.

Design rules:
  * "Create Collider" NEVER touches the selected objects: no edit-mode switching,
    no bpy.ops geometry operators, no selection/active changes. Every collider is
    built with bmesh into a brand new mesh datablock.
  * "Convert to Collider" is the only operator allowed to modify the selection,
    and it only modifies the objects it actually processes (skipped objects are
    left untouched).
  * Naming conventions are user-defined and stored in the add-on preferences, so
    they are shared by every .blend file.
"""

import math
import re
import uuid

import bmesh
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatVectorProperty,
                       IntProperty, StringProperty)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList
from mathutils import Matrix, Vector

ADDON_ID = __package__
LOD_STRING_PATTERN = re.compile(r'_LOD\d+$', re.IGNORECASE)
EPS = 1e-6

# (identifier, label, description) — identifier.lower() is the token property name
COLLIDER_TYPES = [
    ('BOX',      "Box",      "Bounding box collider"),
    ('SPHERE',   "Sphere",   "Bounding sphere collider"),
    ('CYLINDER', "Cylinder", "Bounding cylinder collider"),
    ('CAPSULE',  "Capsule",  "Bounding capsule collider"),
    ('CONVEX',   "Convex",   "Convex hull of the mesh"),
    ('TRIANGLE', "Triangle", "Triangulated copy of the mesh"),
]
TYPE_IDS = [t[0] for t in COLLIDER_TYPES]
# When a token is shared by several types (e.g. Unreal 'UCX'), this order decides
# which type it is read back as by "Update All Names".
TOKEN_READ_PRIORITY = ['CONVEX', 'TRIANGLE', 'BOX', 'SPHERE', 'CAPSULE', 'CYLINDER']

DEFAULT_CONVENTIONS = [
    {
        'name': "Unity / Enfusion",
        'pattern': "{type}_{name}",
        'merge_pattern': "",
        'tokens': {'BOX': 'UBX', 'SPHERE': 'USP', 'CYLINDER': 'UCL',
                   'CAPSULE': 'UCS', 'CONVEX': 'UCX', 'TRIANGLE': 'UTM'},
    },
    {
        'name': "Unreal Engine",
        'pattern': "{type}_{name}",
        'merge_pattern': "",
        'tokens': {'BOX': 'UBX', 'SPHERE': 'USP', 'CYLINDER': 'UCX',
                   'CAPSULE': 'UCP', 'CONVEX': 'UCX', 'TRIANGLE': 'UCX'},
    },
]
DEFAULT_MATERIAL_NAME = "Collider_Material"
DEFAULT_MATERIAL_COLOR = (1.0, 1.0, 0.0, 0.1)

AXIS_ITEMS = [
    ('AUTO', "Auto", "Use the longest dimension of the bounds"),
    ('X',    "X",    "Align along X"),
    ('Y',    "Y",    "Align along Y"),
    ('Z',    "Z",    "Align along Z"),
]

ALIGN_ITEMS = [
    ('LOCAL', "Object", "Collider follows the source object's rotation (tightest fit)"),
    ('WORLD', "World",  "Collider is aligned to the world axes"),
]


# ── Naming conventions (stored in add-on preferences) ─────────────────────────

def _tag_prefs_dirty(context):
    try:
        context.preferences.is_dirty = True
    except Exception:
        pass


class COLLIDER_PG_convention(PropertyGroup):
    # `name` is inherited from PropertyGroup
    uid: StringProperty(options={'HIDDEN'})
    pattern: StringProperty(
        name="Name Pattern", default="{type}_{name}",
        description="Collider name. Tokens: {type} = type token below, {name} = source object name "
                    "(LOD suffix removed). E.g. '{type}_{name}' or '{name}_{type}'")
    merge_pattern: StringProperty(
        name="Merge Pattern", default="",
        description="Optional. Name used when 'Merge Collider' is enabled. Extra token: {target} = merge target "
                    "name. E.g. '{type}_{name}_mergecol_{target}'. Leave empty to disable merging")
    box: StringProperty(name="Box", default="UBX", description="Token for box colliders. Empty = not available")
    sphere: StringProperty(name="Sphere", default="USP", description="Token for sphere colliders. Empty = not available")
    cylinder: StringProperty(name="Cylinder", default="UCL", description="Token for cylinder colliders. Empty = not available")
    capsule: StringProperty(name="Capsule", default="UCS", description="Token for capsule colliders. Empty = not available")
    convex: StringProperty(name="Convex", default="UCX", description="Token for convex colliders. Empty = not available")
    triangle: StringProperty(name="Triangle", default="UTM", description="Token for triangle mesh colliders. Empty = not available")
    material_name: StringProperty(
        name="Material", default=DEFAULT_MATERIAL_NAME,
        description="Material assigned to colliders. Picked by name in the current file; created with the "
                    "color below if it does not exist. Empty = no material")
    material_color: FloatVectorProperty(
        name="Color", subtype='COLOR', size=4, min=0.0, max=1.0, default=DEFAULT_MATERIAL_COLOR,
        description="Viewport color used when the material has to be created")

    def token(self, ctype):
        return getattr(self, ctype.lower()).strip()

    def tokens(self):
        return {t: self.token(t) for t in TYPE_IDS if self.token(t)}

    def has_merge(self):
        return bool(self.merge_pattern.strip())

    def problems(self):
        out = []
        if "{name}" not in self.pattern:
            out.append("Name Pattern must contain {name}")
        if "{type}" not in self.pattern:
            out.append("Name Pattern should contain {type}")
        if self.has_merge() and not all(t in self.merge_pattern for t in ("{name}", "{target}")):
            out.append("Merge Pattern must contain {name} and {target}")
        if not self.tokens():
            out.append("No collider type has a token")
        return out

    def format(self, ctype, name, target=None):
        pattern = self.merge_pattern if (target and self.has_merge()) else self.pattern
        return (pattern.replace("{type}", self.token(ctype))
                       .replace("{name}", name)
                       .replace("{target}", target or ""))


def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def _unique_convention_name(prefs, base):
    names = {c.name for c in prefs.conventions}
    if base not in names:
        return base
    i = 1
    while f"{base} {i}" in names:
        i += 1
    return f"{base} {i}"


def add_convention(prefs, data=None, copy_from=None):
    c = prefs.conventions.add()
    c.uid = uuid.uuid4().hex
    if copy_from is not None:
        for prop in ("pattern", "merge_pattern", "material_name", "material_color",
                     *[t.lower() for t in TYPE_IDS]):
            setattr(c, prop, getattr(copy_from, prop))
        c.name = _unique_convention_name(prefs, f"{copy_from.name} Copy")
    elif data is not None:
        c.name = _unique_convention_name(prefs, data['name'])
        c.pattern = data['pattern']
        c.merge_pattern = data['merge_pattern']
        for t in TYPE_IDS:
            setattr(c, t.lower(), data['tokens'].get(t, ""))
    else:
        c.name = _unique_convention_name(prefs, "Custom")
    prefs.active_index = len(prefs.conventions) - 1
    return c


def ensure_default_conventions(prefs, force=False):
    """Add the built-in conventions when the list is empty (or the missing ones when force=True)."""
    if prefs is None:
        return
    existing = {c.name for c in prefs.conventions}
    if prefs.conventions and not force:
        return
    for data in DEFAULT_CONVENTIONS:
        if data['name'] not in existing:
            add_convention(prefs, data=data)


def active_convention(context):
    prefs = get_prefs(context)
    if prefs is None or not prefs.conventions:
        return None
    uid = context.scene.collider_convention_uid
    for c in prefs.conventions:
        if c.uid == uid:
            return c
    return prefs.conventions[0]


# Scene-level convention picker: an enum whose items come from the preferences,
# stored as the convention's uid so renaming / reordering never breaks it.
_convention_enum_cache = []


def _convention_items(self, context):
    prefs = get_prefs(context)
    items = []
    if prefs is not None:
        for i, c in enumerate(prefs.conventions):
            items.append((c.uid, c.name, c.pattern, i))
    if not items:
        items = [('NONE', "(no conventions)", "", 0)]
    _convention_enum_cache[:] = items
    return _convention_enum_cache


def _convention_get(scene):
    prefs = get_prefs()
    if prefs is None:
        return 0
    for i, c in enumerate(prefs.conventions):
        if c.uid == scene.collider_convention_uid:
            return i
    return 0


def _convention_set(scene, value):
    prefs = get_prefs()
    if prefs is not None and 0 <= value < len(prefs.conventions):
        scene.collider_convention_uid = prefs.conventions[value].uid


# ── Small helpers ─────────────────────────────────────────────────────────────

def strip_lod(name):
    return LOD_STRING_PATTERN.sub("", name)


def rotation_of(obj, align):
    """3x3 rotation matrix used as the collider's orientation."""
    if align == 'WORLD':
        return Matrix.Identity(3)
    return obj.matrix_world.to_quaternion().to_matrix()


class EvaluatedMesh:
    """Context manager giving a read-only mesh for `obj` (with or without modifiers).
    Never modifies the object."""

    def __init__(self, obj, depsgraph, use_modifiers):
        self.obj = obj
        self.depsgraph = depsgraph
        self.use_modifiers = use_modifiers
        self._eval = None

    def __enter__(self):
        if self.use_modifiers:
            self._eval = self.obj.evaluated_get(self.depsgraph)
            return self._eval.to_mesh()
        return self.obj.data

    def __exit__(self, *exc):
        if self._eval is not None:
            self._eval.to_mesh_clear()
        return False


def world_points(objects, depsgraph, use_modifiers):
    pts = []
    for obj in objects:
        mw = obj.matrix_world
        with EvaluatedMesh(obj, depsgraph, use_modifiers) as me:
            pts.extend(mw @ v.co for v in me.vertices)
    return pts


def bounds_in_frame(points, rot):
    """Bounds of world `points` in a frame with rotation `rot`.
    Returns (size, world_center)."""
    rot_inv = rot.transposed()
    local = [rot_inv @ p for p in points]
    mn = Vector((min(v.x for v in local), min(v.y for v in local), min(v.z for v in local)))
    mx = Vector((max(v.x for v in local), max(v.y for v in local), max(v.z for v in local)))
    return mx - mn, rot @ ((mn + mx) * 0.5)


def pick_axis(size, axis):
    if axis == 'AUTO':
        return max(range(3), key=lambda i: size[i])
    return 'XYZ'.index(axis)


def axis_rotation(axis_index):
    """Rotation mapping +Z onto the requested axis."""
    if axis_index == 0:
        return Matrix.Rotation(math.pi / 2, 3, 'Y')
    if axis_index == 1:
        return Matrix.Rotation(-math.pi / 2, 3, 'X')
    return Matrix.Identity(3)


# ── bmesh primitive builders (all centered on origin, long axis = +Z) ─────────

def bm_box(size):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co = Vector((v.co.x * size.x, v.co.y * size.y, v.co.z * size.z))
    return bm


def bm_sphere(radius, segments):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=max(segments // 2, 3),
                              radius=radius)
    return bm


def bm_cylinder(radius, depth, segments):
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segments,
                          radius1=radius, radius2=radius, depth=depth)
    return bm


def bm_capsule(radius, total_height, segments):
    """Capsule along Z: hemispheres joined by a cylinder. total_height includes the caps."""
    half = max(total_height * 0.5 - radius, 0.0)
    rings = max(segments // 4, 2)  # rings per hemisphere
    thetas = [2 * math.pi * j / segments for j in range(segments)]

    bm = bmesh.new()
    ring_list = []

    def ring(rr, z):
        return [bm.verts.new((rr * math.cos(t), rr * math.sin(t), z)) for t in thetas]

    top = bm.verts.new((0.0, 0.0, half + radius))
    for i in range(1, rings + 1):            # top hemisphere: pole -> equator
        a = (math.pi / 2) * i / rings
        ring_list.append(ring(radius * math.sin(a), half + radius * math.cos(a)))
    start = 0 if half > EPS else 1           # avoid duplicating the equator for a sphere
    for i in range(start, rings):            # bottom hemisphere: equator -> pole
        a = (math.pi / 2) * i / rings
        ring_list.append(ring(radius * math.cos(a), -half - radius * math.sin(a)))
    bottom = bm.verts.new((0.0, 0.0, -half - radius))

    n = segments
    first, last = ring_list[0], ring_list[-1]
    for j in range(n):
        bm.faces.new((top, first[j], first[(j + 1) % n]))
        bm.faces.new((bottom, last[(j + 1) % n], last[j]))
    for a_ring, b_ring in zip(ring_list, ring_list[1:]):
        for j in range(n):
            k = (j + 1) % n
            bm.faces.new((a_ring[j], b_ring[j], b_ring[k], a_ring[k]))

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def bm_convex_hull(points):
    """Convex hull of `points` (already in collider space). None if degenerate."""
    bm = bmesh.new()
    for p in points:
        bm.verts.new(p)
    if len(bm.verts) < 4:
        bm.free()
        return None
    result = bmesh.ops.convex_hull(bm, input=list(bm.verts))
    junk = list({g for g in result['geom_interior'] + result['geom_unused']
                 if isinstance(g, bmesh.types.BMVert)})
    if junk:
        bmesh.ops.delete(bm, geom=junk, context='VERTS')
    if not bm.faces:
        bm.free()
        return None
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def bm_triangle_mesh(objects, to_collider, depsgraph, use_modifiers):
    """Triangulated copy of the faces of `objects`, transformed into collider space."""
    bm = bmesh.new()
    for obj in objects:
        m = to_collider @ obj.matrix_world
        flip = m.determinant() < 0
        with EvaluatedMesh(obj, depsgraph, use_modifiers) as me:
            verts = [bm.verts.new(m @ v.co) for v in me.vertices]
            for poly in me.polygons:
                idx = list(poly.vertices)
                if flip:
                    idx.reverse()
                try:
                    bm.faces.new([verts[i] for i in idx])
                except ValueError:
                    pass  # duplicate / degenerate face
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
    if not bm.faces:
        bm.free()
        return None
    bmesh.ops.triangulate(bm, faces=bm.faces)
    return bm


# ── Material / object creation ────────────────────────────────────────────────

def get_or_create_collider_material(convention):
    """Material from the convention (by name in the current file); created if missing. None = no material."""
    name = convention.material_name.strip()
    if not name:
        return None
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
        mat.diffuse_color = convention.material_color
        if hasattr(mat, "surface_render_method"):   # Blender 4.2+
            mat.surface_render_method = 'BLENDED'
        elif hasattr(mat, "blend_method"):
            mat.blend_method = 'BLEND'
    return mat


def assign_collider_material(mesh, material):
    if material is None:
        return
    mesh.materials.clear()
    mesh.materials.append(material)


def new_collider_object(context, name, bm, matrix_world, source_obj, material):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    assign_collider_material(mesh, material)

    obj = bpy.data.objects.new(name, mesh)
    linked = False
    for coll in source_obj.users_collection:
        if coll.library is None and coll.override_library is None:
            coll.objects.link(obj)
            linked = True
    if not linked:
        context.collection.objects.link(obj)
    obj.matrix_world = matrix_world
    return obj


def set_parent_keep_transform(child, parent_obj):
    world = child.matrix_world.copy()
    child.parent = parent_obj
    child.matrix_parent_inverse = parent_obj.matrix_world.inverted_safe()
    child.matrix_world = world


# ── Collider building ─────────────────────────────────────────────────────────

def build_collider_geometry(kind, objects, frame_obj, depsgraph, *, align, axis, segments,
                            use_modifiers):
    """Returns (bmesh, matrix_world) for the collider, or (None, reason)."""
    points = world_points(objects, depsgraph, use_modifiers)
    if not points:
        return None, "has no vertices"

    rot = rotation_of(frame_obj, align)
    size, center = bounds_in_frame(points, rot)
    matrix = Matrix.Translation(center) @ rot.to_4x4()
    to_collider = matrix.inverted_safe()

    if kind == 'BOX':
        bm = bm_box(size)
    elif kind == 'SPHERE':
        bm = bm_sphere(max(max(size) * 0.5, EPS), segments)
    elif kind in {'CYLINDER', 'CAPSULE'}:
        ai = pick_axis(size, axis)
        length = size[ai]
        radius = max(max(size[i] for i in range(3) if i != ai) * 0.5, EPS)
        if kind == 'CYLINDER':
            bm = bm_cylinder(radius, max(length, EPS), segments)
        else:
            bm = bm_capsule(radius, max(length, 2 * radius), segments)
        bmesh.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0), matrix=axis_rotation(ai))
    elif kind == 'CONVEX':
        bm = bm_convex_hull([to_collider @ p for p in points])
        if bm is None:
            return None, "is too flat / small for a convex hull"
    elif kind == 'TRIANGLE':
        bm = bm_triangle_mesh(objects, to_collider, depsgraph, use_modifiers)
        if bm is None:
            return None, "has no faces"
    else:
        return None, f"unknown collider type {kind}"
    return bm, matrix


# ── Shared operator UI / validation ───────────────────────────────────────────

def draw_type_and_merge_ui(op, context, layout):
    conv = active_convention(context)
    layout.prop(op, "collider_type")
    if conv is None:
        layout.label(text="No naming convention defined", icon='ERROR')
        return
    if not conv.token(op.collider_type):
        layout.label(text=f"No '{op.collider_type.title()}' token in '{conv.name}'", icon='ERROR')
    if conv.has_merge():
        layout.prop(op, "merge_collider")
        if op.merge_collider:
            layout.prop_search(op, "merge_target", context.scene, "objects")


def validate_common(op, context):
    """Returns (convention, merge_target_obj or None). Raises ValueError with a message."""
    prefs = get_prefs(context)
    ensure_default_conventions(prefs)
    conv = active_convention(context)
    if conv is None:
        raise ValueError("No naming convention defined (see Colliders > Naming Conventions)")
    problems = [p for p in conv.problems() if "must" in p]
    if problems:
        raise ValueError(f"Naming convention '{conv.name}': {problems[0]}")
    if not conv.token(op.collider_type):
        raise ValueError(f"'{op.collider_type.title()}' has no token in the '{conv.name}' naming convention")
    merge_target = None
    if op.merge_collider and conv.has_merge():
        merge_target = context.scene.objects.get(op.merge_target)
        if merge_target is None:
            raise ValueError("Please pick a valid merge target object")
    return conv, merge_target


def object_mode_poll(cls, context):
    if context.mode != 'OBJECT':
        cls.poll_message_set("Switch to Object Mode first")
        return False
    return True


# ── Collider operators ────────────────────────────────────────────────────────

class OBJECT_OT_create_collider(Operator):
    """Create a new collider object around the selected object(s). The selected objects are never modified"""
    bl_idname = "object.create_collider"
    bl_label = "Create Collider"
    bl_options = {'REGISTER', 'UNDO'}

    collider_type: EnumProperty(name="Collider Type", items=COLLIDER_TYPES, default='BOX')

    selection_mode: EnumProperty(
        name="Selection Mode",
        description="How multiple selected objects are handled",
        items=[
            ('BATCH',    "One per Object", "One collider per selected object, parented to its own source"),
            ('COMBINED', "Combined",       "A single collider around all selected objects, parented to the active object"),
        ],
        default='BATCH',
    )
    convex_as_box: BoolProperty(name="Convex As Box",
                                description="Build a box-shaped collider but name it as a convex collider",
                                default=False)
    align: EnumProperty(name="Orientation", items=ALIGN_ITEMS, default='LOCAL')
    axis: EnumProperty(name="Axis", description="Long axis of cylinder / capsule colliders",
                       items=AXIS_ITEMS, default='AUTO')
    segments: IntProperty(name="Segments", description="Resolution of round colliders",
                          default=16, min=6, max=64)
    use_modifiers: BoolProperty(name="Use Modifiers",
                                description="Build the collider from the mesh with modifiers applied",
                                default=True)
    parent_to_source: BoolProperty(name="Parent to Source",
                                   description="Parent the collider to the source object", default=True)
    merge_collider: BoolProperty(name="Merge Collider",
                                 description="Name the collider with the convention's Merge Pattern",
                                 default=False)
    merge_target: StringProperty(name="Merge Target")

    @classmethod
    def poll(cls, context):
        return object_mode_poll(cls, context)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        draw_type_and_merge_ui(self, context, layout)
        if self.collider_type == 'CONVEX':
            layout.prop(self, "convex_as_box")
        layout.separator()
        layout.prop(self, "selection_mode", expand=True)
        layout.prop(self, "align")
        if self.collider_type in {'CYLINDER', 'CAPSULE'}:
            layout.prop(self, "axis", expand=True)
        if self.collider_type in {'SPHERE', 'CYLINDER', 'CAPSULE'}:
            layout.prop(self, "segments")
        layout.prop(self, "use_modifiers")
        layout.prop(self, "parent_to_source")

    def execute(self, context):
        mesh_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not mesh_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        try:
            conv, merge_target = validate_common(self, context)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        depsgraph = context.evaluated_depsgraph_get()
        opts = dict(align=self.align, axis=self.axis, segments=self.segments,
                    use_modifiers=self.use_modifiers)
        active = context.active_object if context.active_object in mesh_objs else mesh_objs[0]

        if self.selection_mode == 'COMBINED':
            jobs = [(active, mesh_objs)]
        else:
            jobs = [(o, [o]) for o in mesh_objs]

        # Geometry may differ from the naming type (Convex As Box: box shape, convex name)
        geometry = 'BOX' if (self.collider_type == 'CONVEX' and self.convex_as_box) else self.collider_type
        material = get_or_create_collider_material(conv)
        created, skipped = [], []
        for src, objs in jobs:
            if merge_target is not None and src.parent != merge_target.parent:
                skipped.append(f"{src.name} (different parent than merge target)")
                continue
            bm, matrix = build_collider_geometry(geometry, objs, src, depsgraph, **opts)
            if bm is None:
                skipped.append(f"{src.name} ({matrix})")
                continue
            name = conv.format(self.collider_type, strip_lod(src.name),
                               strip_lod(merge_target.name) if merge_target else None)
            col = new_collider_object(context, name, bm, matrix, src, material)
            if self.parent_to_source:
                set_parent_keep_transform(col, src)
            created.append(col)

        for msg in skipped:
            self.report({'WARNING'}, f"Skipped {msg}")
        if not created:
            return {'CANCELLED'}
        self.report({'INFO'}, f"Created {len(created)} collider(s)")
        return {'FINISHED'}


class OBJECT_OT_convert_to_collider(Operator):
    """Turn the selected object(s) themselves into colliders (renames them; Convex / Triangle also rebuild their geometry)"""
    bl_idname = "object.convert_to_collider"
    bl_label = "Convert to Collider"
    bl_options = {'REGISTER', 'UNDO'}

    collider_type: EnumProperty(name="Collider Type", items=COLLIDER_TYPES, default='CONVEX')
    use_modifiers: BoolProperty(name="Apply Modifiers",
                                description="Bake modifiers into the mesh before converting (Convex / Triangle)",
                                default=True)
    merge_collider: BoolProperty(name="Merge Collider",
                                 description="Name the collider with the convention's Merge Pattern",
                                 default=False)
    merge_target: StringProperty(name="Merge Target")

    @classmethod
    def poll(cls, context):
        return object_mode_poll(cls, context)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        draw_type_and_merge_ui(self, context, layout)
        layout.separator()
        if self.collider_type in {'CONVEX', 'TRIANGLE'}:
            layout.prop(self, "use_modifiers")
        else:
            layout.label(text="Geometry is kept, only renamed", icon='INFO')

    def _rebuild(self, obj, kind, depsgraph):
        """Rebuild obj's own mesh as convex hull / triangles. Returns error string or None."""
        if obj.library or obj.data.library:
            return "linked data"
        with EvaluatedMesh(obj, depsgraph, self.use_modifiers) as me:
            if kind == 'CONVEX':
                bm = bm_convex_hull([v.co.copy() for v in me.vertices])
                if bm is None:
                    return "too flat / small for a convex hull"
            else:
                bm = bmesh.new()
                bm.from_mesh(me)
                bmesh.ops.triangulate(bm, faces=bm.faces)

        if obj.data.users > 1:           # don't affect other objects sharing this mesh
            obj.data = obj.data.copy()
        if kind == 'CONVEX' and obj.data.shape_keys:
            obj.shape_key_clear()
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        if self.use_modifiers:
            obj.modifiers.clear()
        return None

    def execute(self, context):
        mesh_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not mesh_objs:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        try:
            conv, merge_target = validate_common(self, context)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        depsgraph = context.evaluated_depsgraph_get()
        material = get_or_create_collider_material(conv)
        converted = 0
        for obj in mesh_objs:
            if merge_target is not None and obj.parent != merge_target.parent:
                self.report({'WARNING'}, f"Skipped {obj.name}: different parent than merge target")
                continue
            if self.collider_type in {'CONVEX', 'TRIANGLE'}:
                err = self._rebuild(obj, self.collider_type, depsgraph)
                if err:
                    self.report({'WARNING'}, f"Skipped {obj.name}: {err}")
                    continue
            obj.name = conv.format(self.collider_type, strip_lod(obj.name),
                                   strip_lod(merge_target.name) if merge_target else None)
            if material is not None:
                if obj.data.users > 1:
                    obj.data = obj.data.copy()
                assign_collider_material(obj.data, material)
            converted += 1

        if not converted:
            return {'CANCELLED'}
        self.report({'INFO'}, f"Converted {converted} object(s)")
        return {'FINISHED'}


def _pattern_regex(pattern, type_tokens):
    """Regex matching names produced by `pattern`. None if the pattern can't be parsed back."""
    if "{type}" not in pattern or "{name}" not in pattern or not type_tokens:
        return None
    alternation = "|".join(re.escape(t) for t in sorted(type_tokens, key=len, reverse=True))
    rx = ""
    for part in re.split(r'(\{type\}|\{name\}|\{target\})', pattern):
        if part == "{type}":
            rx += f"(?P<type>{alternation})"
        elif part == "{name}":
            rx += "(?P<name>.+?)"
        elif part == "{target}":
            rx += "(?P<target>.+?)"
        else:
            rx += re.escape(part)
    try:
        return re.compile(f"^{rx}$")
    except re.error:   # e.g. a token used twice
        return None


def _parse_collider_name(name, conv):
    """Returns (ctype, base_name, target) if `name` follows `conv`, else None."""
    token_to_type = {}
    for t in TOKEN_READ_PRIORITY:
        tok = conv.token(t)
        if tok:
            token_to_type.setdefault(tok, t)
    patterns = [conv.merge_pattern] if conv.has_merge() else []
    patterns.append(conv.pattern)
    for pattern in patterns:
        rx = _pattern_regex(pattern, token_to_type.keys())
        m = rx.match(name) if rx else None
        if m:
            return token_to_type[m.group('type')], m.group('name'), m.groupdict().get('target')
    return None


class OBJECT_OT_batch_rename_colliders(Operator):
    """Rename every collider in the file (named with any known convention) to the current naming convention"""
    bl_idname = "object.batch_rename_colliders"
    bl_label = "Update Collider Names"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prefs = get_prefs(context)
        target = active_convention(context)
        if target is None:
            self.report({'ERROR'}, "No naming convention defined")
            return {'CANCELLED'}
        others = [c for c in prefs.conventions if c != target]

        renamed = 0
        for obj in bpy.data.objects:
            if obj.type != 'MESH' or obj.library:
                continue
            if _parse_collider_name(obj.name, target):
                continue    # already follows the current convention
            for conv in others:
                parsed = _parse_collider_name(obj.name, conv)
                if parsed is None:
                    continue
                ctype, base, merge = parsed
                if target.token(ctype):
                    obj.name = target.format(ctype, base, merge)
                    renamed += 1
                break

        self.report({'INFO'}, f"Renamed {renamed} collider(s)")
        return {'FINISHED'}


# ── Convention editing operators ──────────────────────────────────────────────

class COLLIDER_OT_convention_add(Operator):
    """Add a new naming convention"""
    bl_idname = "collider.convention_add"
    bl_label = "Add Naming Convention"
    bl_options = {'INTERNAL'}

    duplicate: BoolProperty(name="Duplicate Active", default=False, options={'SKIP_SAVE'})

    def execute(self, context):
        prefs = get_prefs(context)
        src = None
        if self.duplicate and 0 <= prefs.active_index < len(prefs.conventions):
            src = prefs.conventions[prefs.active_index]
        c = add_convention(prefs, copy_from=src)
        context.scene.collider_convention_uid = c.uid
        _tag_prefs_dirty(context)
        return {'FINISHED'}


class COLLIDER_OT_convention_remove(Operator):
    """Delete the selected naming convention"""
    bl_idname = "collider.convention_remove"
    bl_label = "Delete Naming Convention"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        prefs = get_prefs(context)
        return prefs is not None and 0 <= prefs.active_index < len(prefs.conventions)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        prefs = get_prefs(context)
        prefs.conventions.remove(prefs.active_index)
        prefs.active_index = min(prefs.active_index, len(prefs.conventions) - 1)
        _tag_prefs_dirty(context)
        return {'FINISHED'}


class COLLIDER_OT_convention_move(Operator):
    """Move the selected naming convention up or down"""
    bl_idname = "collider.convention_move"
    bl_label = "Move Naming Convention"
    bl_options = {'INTERNAL'}

    direction: EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])

    def execute(self, context):
        prefs = get_prefs(context)
        i = prefs.active_index
        j = i - 1 if self.direction == 'UP' else i + 1
        if 0 <= i < len(prefs.conventions) and 0 <= j < len(prefs.conventions):
            prefs.conventions.move(i, j)
            prefs.active_index = j
            _tag_prefs_dirty(context)
        return {'FINISHED'}


class COLLIDER_OT_convention_restore_defaults(Operator):
    """Re-add the built-in naming conventions that are missing (Unity / Enfusion, Unreal Engine)"""
    bl_idname = "collider.convention_restore_defaults"
    bl_label = "Restore Built-in Conventions"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        prefs = get_prefs(context)
        ensure_default_conventions(prefs, force=True)
        _tag_prefs_dirty(context)
        return {'FINISHED'}


class COLLIDER_UL_conventions(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        is_current = active_convention(context) == item
        row.prop(item, "name", text="", emboss=False,
                 icon='CHECKMARK' if is_current else 'BLANK1')
        if item.problems():
            row.label(text="", icon='ERROR')


def draw_convention_editor(layout, context):
    prefs = get_prefs(context)
    if prefs is None:
        layout.label(text="Add-on preferences unavailable", icon='ERROR')
        return

    row = layout.row()
    row.template_list("COLLIDER_UL_conventions", "", prefs, "conventions", prefs, "active_index", rows=4)
    col = row.column(align=True)
    col.operator("collider.convention_add", text="", icon='ADD')
    col.operator("collider.convention_remove", text="", icon='REMOVE')
    col.separator()
    col.operator("collider.convention_add", text="", icon='DUPLICATE').duplicate = True
    col.separator()
    col.operator("collider.convention_move", text="", icon='TRIA_UP').direction = 'UP'
    col.operator("collider.convention_move", text="", icon='TRIA_DOWN').direction = 'DOWN'
    col.separator()
    col.operator("collider.convention_restore_defaults", text="", icon='LOOP_BACK')

    if not (0 <= prefs.active_index < len(prefs.conventions)):
        return
    c = prefs.conventions[prefs.active_index]

    box = layout.box()
    box.use_property_split = True
    box.use_property_decorate = False
    box.prop(c, "name")
    box.prop(c, "pattern")
    box.prop(c, "merge_pattern")

    sub = box.column(heading="Type Tokens", align=True)
    for t in TYPE_IDS:
        sub.prop(c, t.lower())

    mat = box.column(align=True)
    mat.prop_search(c, "material_name", bpy.data, "materials", text="Material")
    if c.material_name.strip() and c.material_name.strip() not in bpy.data.materials:
        mat.prop(c, "material_color")

    for p in c.problems():
        box.label(text=p, icon='ERROR')
    if "{name}" in c.pattern:
        example = c.format('BOX', "Rock") if c.token('BOX') else c.format(next(iter(c.tokens()), 'BOX'), "Rock")
        box.label(text=f"Example: {example}", icon='INFO')


# ── Panels / preferences ──────────────────────────────────────────────────────

class COLLIDER_AP_preferences(AddonPreferences):
    bl_idname = ADDON_ID

    conventions: CollectionProperty(type=COLLIDER_PG_convention)
    active_index: IntProperty(name="Active Convention", default=0)

    def draw(self, context):
        self.layout.label(text="Naming Conventions")
        draw_convention_editor(self.layout, context)


class VIEW3D_PT_collider_panel(Panel):
    bl_label = "Collider Tools"
    bl_idname = "VIEW3D_PT_collider_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Colliders"

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Naming Convention:", icon='FILE_TEXT')
        prefs = get_prefs(context)
        if prefs is None or not prefs.conventions:
            box.operator("collider.convention_restore_defaults", icon='LOOP_BACK')
        else:
            box.prop(context.scene, "collider_convention", text="")
        layout.separator()
        col = layout.column(align=True)
        col.operator("object.create_collider", text="Create Collider", icon='MESH_CUBE')
        col.operator("object.convert_to_collider", text="Convert to Collider", icon='MOD_TRIANGULATE')
        layout.separator()
        layout.operator("object.batch_rename_colliders", text="Update All Names", icon='SORTALPHA')


class VIEW3D_PT_collider_conventions(Panel):
    bl_label = "Naming Conventions"
    bl_idname = "VIEW3D_PT_collider_conventions"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Colliders"
    bl_parent_id = "VIEW3D_PT_collider_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_convention_editor(self.layout, context)


classes = (
    COLLIDER_PG_convention,
    COLLIDER_AP_preferences,
    COLLIDER_UL_conventions,
    COLLIDER_OT_convention_add,
    COLLIDER_OT_convention_remove,
    COLLIDER_OT_convention_move,
    COLLIDER_OT_convention_restore_defaults,
    OBJECT_OT_create_collider,
    OBJECT_OT_convert_to_collider,
    OBJECT_OT_batch_rename_colliders,
    VIEW3D_PT_collider_panel,
    VIEW3D_PT_collider_conventions,
)


def _seed_defaults_timer():
    prefs = get_prefs()
    if prefs is not None and not prefs.conventions:
        ensure_default_conventions(prefs)
        _tag_prefs_dirty(bpy.context)
    return None


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.collider_convention_uid = StringProperty(options={'HIDDEN'})
    bpy.types.Scene.collider_convention = EnumProperty(
        name="Naming Convention",
        description="Naming convention used by this scene (edit them in Naming Conventions below)",
        items=_convention_items, get=_convention_get, set=_convention_set,
    )
    # Preferences may not be reachable yet while the add-on is being enabled.
    bpy.app.timers.register(_seed_defaults_timer, first_interval=0.1)


def unregister():
    if bpy.app.timers.is_registered(_seed_defaults_timer):
        bpy.app.timers.unregister(_seed_defaults_timer)
    del bpy.types.Scene.collider_convention
    del bpy.types.Scene.collider_convention_uid
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
