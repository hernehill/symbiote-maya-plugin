"""Node types and attributes Symbiote's tools never let a file or the model set, reviewed by hand against Maya 2027.

Pure: no ``maya``. ``DENIED_ATTRIBUTES`` carry code Maya runs, or a path Maya reads or writes. ``HEAVY_ATTRIBUTES``
make Maya do work that grows with the value (smoothing, tessellation, sampling), so an import may bring them only at
their defaults. ``VALUE_CAPS`` bound what an import may bring and what a write tool may set.

The write tools set only ``SETTABLE_TYPES``, and on them only the attributes the census measured
(``maya_allowlist_data``, derived by ``tools/derive_allowlist.py``). ``DENIED_TYPES`` names the types a reviewer
ruled out on purpose; every other type outside ``SETTABLE_TYPES`` is refused too.
"""

from __future__ import annotations

import re

# Each entry says why. measured: docs/research/2026-11-12-maya-measurements.md.
DENIED_ATTRIBUTES = {
    # Code.
    "homeCommand": "it runs as MEL or Python when the artist chooses View > Default Home",
    "rmbCommand": "it names a procedure Maya runs from a right-click",
    "journalCommand": "it journals commands (a bool, denied with the command attributes)",
    "before": "a script node runs it",
    "after": "a script node runs it",
    "expression": "an expression node runs it",
    "pyScript": "MASH runs it as Python",
    # MEL hooks on the render globals.
    "preMel": "Maya runs it as MEL before a render",
    "postMel": "Maya runs it as MEL after a render",
    "preRenderMel": "Maya runs it as MEL before each frame",
    "postRenderMel": "Maya runs it as MEL after each frame",
    "preRenderLayerMel": "Maya runs it as MEL before each render layer",
    "postRenderLayerMel": "Maya runs it as MEL after each render layer",
    # Paths Maya or Arnold reads.
    "plugins_path": "Arnold loads plugins from it",
    "procedural_searchpath": "Arnold loads procedurals from it",
    "fileTextureName": "Maya reads the image it names",
    "fileNames": "Maya loads the scene it names",
    "imageName": "Maya reads the image it names",
    "filename": "Maya reads the file it names",
    "cachePath": "Maya reads the cache it names",
    # Depth-map files a light writes or reads at render time.
    "writeDmap": "the light writes depth-map files when it renders",
    "reuseDmap": "the light reads depth-map files when it renders",
    "dmapLightName": "it names the depth-map files the light writes",
    "dmapSceneName": "it names the depth-map files the light writes",
    "dmapFrameExt": "it names the depth-map files the light writes",
}
# Any attribute named like a hook: Maya's own (preMel, rmbCommand) and a plugin's alike.
DENIED_SUFFIXES = ("Mel", "Command", "Script", "Callback")

# Denied to the write tools only (fix round 1, R2): evaluation and display controls that hide or freeze geometry
# from the artist. An import may carry them: every Maya node has nodeState and frozen, so denying them to the
# import audit refuses every clean file (measured: "|landed.frozen is an attribute Symbiote has no pinned default").
_CONTROL = "it is an evaluation or display control that can hide or freeze geometry from the artist"
WRITE_DENIED_ATTRIBUTES = {
    "nodeState": _CONTROL,
    "frozen": _CONTROL,
    "caching": _CONTROL,
    "blackBox": _CONTROL,
    "intermediateObject": _CONTROL,
    "hiddenInOutliner": _CONTROL,
    "uiTreatment": _CONTROL,
    "identification": _CONTROL,
    "templateVersion": _CONTROL,
    # Security re-review of 5c-2: each hides geometry somewhere the artist may not look (the LOD switch, the
    # display-override group, playback), where visibility shows greyed in the Outliner.
    "lodVisibility": _CONTROL,
    "overrideVisibility": _CONTROL,
    "overridePlayback": _CONTROL,
    "hideOnPlayback": _CONTROL,
    "overrideLevelOfDetail": _CONTROL,
}

# Numbers that make Maya do work that grows with the value. An import refuses any that differs from its default.
HEAVY_ATTRIBUTES = frozenset(
    {
        "smoothLevel",
        "displaySmoothMesh",
        "renderSmoothLevel",
        "smoothTessLevel",
        "useSmoothPreviewForRender",
        "maxSubd",
        "maxTriangles",
        "useMaxSubdivisions",
        "dispResolution",
        "volumeSamples",
        "extraSampleRate",
        "initialSampleRate",
        "maxShadingSamples",
        "maxVisibilitySamples",
        "shadingSamples",
        "rayDepth",
        "ghosting",
        "ghostPreFrames",
        "ghostPostFrames",
        "ghostsStep",
    }
)

# The largest value any tool lets an attribute hold (security M5, re-review N3, plan Task 13); each has a test.
# The import audit reads this table too (import_audit._number_reasons), so a cap added for the write tools also
# refuses an import over it: deliberate, and pinned by test_import_audit's test_the_write_tools_caps_hold_imports_too.
# No measured Maya default is over its cap (test_census), so a clean file is unaffected.
# smoothTessLevel is 7, not the plan's 4: a fresh mesh holds 7 (measured again in the Task 13 census), so a lower
# cap would refuse every imported mesh and stop a write tool putting the default back.
_SUBDIVISIONS = 200
VALUE_CAPS = {
    # Primitive creators. With every subdivision at 200 the largest primitive is a 240,000-face cube.
    "subdivisionsAxis": _SUBDIVISIONS,
    "subdivisionsCap": _SUBDIVISIONS,  # polyCone's, singular
    "subdivisionsCaps": _SUBDIVISIONS,  # polyCylinder's
    "subdivisionsDepth": _SUBDIVISIONS,
    "subdivisionsHeight": _SUBDIVISIONS,
    "subdivisionsWidth": _SUBDIVISIONS,
    # Mesh smoothing and tessellation.
    "smoothLevel": 3,
    "renderSmoothLevel": 3,
    "smoothTessLevel": 7,
    "maxSubd": 256,
    "maxTriangles": 1_000_000,
    "numTriangles": 1_000_000,  # a mesh's triangle target when useNumTriangles is on, like maxTriangles
    # Render sampling, per object or light.
    "volumeSamples": 64,
    "volumeShadowSamples": 64,
    "psIllumSamples": 64,
    "initialSampleRate": 64,
    "extraSampleRate": 64,
    "dmapResolution": 4096,
    "shadowRays": 64,
    "rayDepthLimit": 16,
    # Ghosting draws the object again at each of these frames, evaluating the scene there. Held to the census
    # default (3): ghost drawing is viewport work, which mayapy can't time, so no higher cap is measured. Ghosting is
    # also checked on the state it leaves, with each ghost counted against SMOOTHED_FACE_BUDGET (draw_change_refusal, reparent_refusal).
    "ghostPreFrames": 3,
    "ghostPostFrames": 3,
}
# Every cap is two-sided: a value below CAP_FLOOR (or below Maya's own minimum, when the census has one) is refused.
# Measured in mayapy 2027 and 2025 (tools/measure_claims.py): maxSubd, numTriangles, volumeSamples and the ghost frame
# counts take any negative, down to -2147483648, without complaint, and spotLight's psIllumSamples (a short) takes
# negatives too; every other capped attribute has a Maya minimum and raises below it.
CAP_FLOOR = 0
# Caps whose rule is "no more than a fresh node holds": each equals its census default on every type (test_write_limits).
CAPS_AT_DEFAULT = frozenset({"smoothTessLevel", "ghostPreFrames", "ghostPostFrames"})
# A smoothed mesh draws faces × 4^smoothLevel faces; past this Maya stalls.
SMOOTHED_FACE_BUDGET = 4_000_000
# The most faces one create may make, counted from the primitive's subdivisions.
CREATE_FACE_BUDGET = 1_000_000

# What the write tools create: each primitive with its cmds.poly* command, lights with shadingNode(asLight=True),
# materials with shadingNode(asShader=True), and a camera with shadingNode("camera", asUtility=True), as the
# Cameras menu does (cmds.camera stays query-only in the source guard).
PRIMITIVE_TYPES = frozenset({"polyCube", "polySphere", "polyCylinder", "polyCone", "polyPlane", "polyTorus"})
LIGHT_TYPES = frozenset({"directionalLight", "pointLight", "spotLight", "areaLight", "ambientLight"})
MATERIAL_TYPES = frozenset({"standardSurface", "openPBRSurface", "lambert"})
CREATABLE_TYPES = PRIMITIVE_TYPES | LIGHT_TYPES | {"camera"}
# The creatable types and their shapes (a primitive's is a mesh; a light and a camera are their own shapes), the
# transforms above them, and the materials. Checked by exact nodeType: a joint is not a transform (re-review N11).
SETTABLE_TYPES = CREATABLE_TYPES | MATERIAL_TYPES | {"transform", "mesh"}
# The nodes a write tool may change without setting their attributes (fix round 1, R1): an assignment adds members
# to a shading engine. edit_refusal still refuses a default (initialShadingGroup), referenced or locked one.
EDITABLE_TYPES = SETTABLE_TYPES | {"shadingEngine"}
# Each primitive's subdivision attributes, which its face count is computed from.
PRIMITIVE_SUBDIVISIONS = {
    "polyCube": ("subdivisionsWidth", "subdivisionsHeight", "subdivisionsDepth"),
    "polyPlane": ("subdivisionsWidth", "subdivisionsHeight"),
    "polySphere": ("subdivisionsAxis", "subdivisionsHeight"),
    "polyTorus": ("subdivisionsAxis", "subdivisionsHeight"),
    "polyCylinder": ("subdivisionsAxis", "subdivisionsHeight", "subdivisionsCaps"),
    "polyCone": ("subdivisionsAxis", "subdivisionsHeight", "subdivisionsCap"),
}
# Enums a write tool may set to only some fields, by Maya's index (measured in 2027), each with why.
ENUM_LIMITS = {
    # 0-2 draw ghostPreFrames/ghostPostFrames frames (capped). 3 (Custom Frames) draws a frame list, 4 and 5 a
    # ghost per keyframe: no cap bounds those.
    "ghostingMode": (
        frozenset({0, 1, 2}),
        "only the frame-count modes (Pre And Post Frames, Pre Frames, Post Frames), whose frame counts are capped",
    ),
}
# The attribute kinds a write tool sets: float3 and double3 only with three numeric children.
SETTABLE_KINDS = (
    "bool",
    "long",
    "short",
    "byte",
    "enum",
    "float",
    "double",
    "doubleLinear",
    "doubleAngle",
    "time",
    "float3",
    "double3",
)

DENIED_TYPES = {
    "script": "a script node runs MEL or Python when the scene opens or closes",
    "expression": "an expression node runs its expression whenever the scene evaluates",
    "reference": "a reference node loads another scene file",
    "file": "a file texture node reads the image it names",
    "renderGlobals": "the render globals hold the MEL hooks Maya runs around a render",
    "aiOptions": "Arnold's options name the plug-in and procedural paths Arnold loads",
    "MASH_Python": "MASH runs its Python",
    "imagePlane": "an image plane reads the image or movie it names",
    "audio": "an audio node reads the sound file it names",
    "cacheFile": "a cache file node reads the cache it names",
    "AlembicNode": "an Alembic node reads the file it names",
}

# Review lists (security M1, re-review N8): census attributes named like a file writer or like heavy work. The
# census test pins each list, so a new Maya that adds one fails until someone rules on it here or caps it.
FILE_WORDS = re.compile(r"dmap|write|reuse|disk|file|cache|load|save|export|ghost|sculpt", re.IGNORECASE)
HEAVY_WORDS = re.compile(
    r"subdiv|level|sample|rays|depth|resolution|iteration|bounce|tess|maxSubd|triangles", re.IGNORECASE
)
# Ruled harmless: none of these reads or writes a file. The ones that do (writeDmap, reuseDmap and the dmap names)
# are in DENIED_ATTRIBUTES.
_DMAP_SHAPE = "it shapes the depth map the light renders in memory; only writeDmap writes it to disk"
_GHOST = "it changes how ghosting draws, in memory; frame counts are capped and ghostingMode is held to them"
FILE_WORD_RULINGS = {
    "computeFromSculptCache": "it makes the mesh use its in-memory sculpt cache; no file",
    "useMeshSculptCache": "it switches the mesh's in-memory sculpt cache; no file",
    "useMeshTexSculptCache": "it switches the mesh's in-memory sculpt cache; no file",
    "dmapBias": _DMAP_SHAPE,
    "dmapFarClipPlane": _DMAP_SHAPE,
    "dmapFilterSize": _DMAP_SHAPE,
    "dmapFocus": _DMAP_SHAPE,
    "dmapNearClipPlane": _DMAP_SHAPE,
    "dmapResolution": _DMAP_SHAPE + " (capped)",
    "dmapWidthFocus": _DMAP_SHAPE,
    "useDmapAutoClipping": _DMAP_SHAPE,
    "useDmapAutoFocus": _DMAP_SHAPE,
    "useMidDistDmap": _DMAP_SHAPE,
    "useOnlySingleDmap": _DMAP_SHAPE,
    "useXMinusDmap": _DMAP_SHAPE,
    "useXPlusDmap": _DMAP_SHAPE,
    "useYMinusDmap": _DMAP_SHAPE,
    "useYPlusDmap": _DMAP_SHAPE,
    "useZMinusDmap": _DMAP_SHAPE,
    "useZPlusDmap": _DMAP_SHAPE,
    "ghosting": _GHOST,
    "ghostingMode": "it picks which frames ghosting draws; held to the frame-count modes (ENUM_LIMITS)",
    "ghostUseDriver": _GHOST,
    "ghostsStep": _GHOST,
    "ghostPreFrames": "how many frames ghosting draws before the current one (capped)",
    "ghostPostFrames": "how many frames ghosting draws after the current one (capped)",
    "ghostColorPre": _GHOST,
    "ghostColorPreR": _GHOST,
    "ghostColorPreG": _GHOST,
    "ghostColorPreB": _GHOST,
    "ghostColorPost": _GHOST,
    "ghostColorPostR": _GHOST,
    "ghostColorPostG": _GHOST,
    "ghostColorPostB": _GHOST,
    "ghostFarOpacity": _GHOST,
    "ghostNearOpacity": _GHOST,
    "loadTiledTextures": "it loads the UV tiles of textures the artist already chose; it names no file",
    "reuseTriangles": "a display cache of the mesh's triangles, in memory",
    "vrOverwriteDefaults": "a lambert's renderer-defaults switch; it names no file",
}
# Heavy-worded attributes that aren't capped, each ruled harmless: a switch, a distance, or a choice whose cost
# doesn't grow with the value. The ones whose cost grows are in VALUE_CAPS.
HEAVY_RULINGS = {
    "depth": "a bool (a camera's depth channel, a cube's depth is a length): no work grows with it",
    "depthJitter": "a bool for the mesh's depth sorting",
    "depthOfField": "a bool: depth of field costs a bounded amount per render",
    "depthType": "an enum of two depth choices",
    "displayResolution": "a bool that draws the resolution gate",
    "displayTriangles": "a bool that draws the mesh's triangle edges",
    "maxVisibilitySamplesOverride": "a bool; the sample count it unlocks is capped",
    "osdSmoothTriangles": "a bool choosing OpenSubdiv's triangle smoothing rule, not a level",
    "shadingSamplesOverride": "a bool; the sample count it unlocks is capped",
    "translucenceDepth": "a distance light travels in the material, not an iteration count",
    "transmissionDepth": "a distance light travels in the material, not an iteration count",
    "transparencyBasedDepth": "a bool for the camera's depth channel",
    "useDepthMapShadows": "a bool: the depth map it renders is capped by dmapResolution and never written to disk",
    "useMaxSubdivisions": "a bool that turns the maxSubd cap on",
    "useNumTriangles": "a bool; the triangle count it unlocks is capped",
    "volumeSamplesOverride": "a bool; the sample count it unlocks is capped",
    "reuseTriangles": "a bool display cache of triangles, in memory",
    # Maya's own hard maximum bounds these (setAttr refuses above it; measured in the census as "max").
    "antialiasingLevel": "Maya holds it to at most 5",
    "shadingSamples": "Maya holds it to at most 32",
    "maxShadingSamples": "Maya holds it to at most 32",
    "maxVisibilitySamples": "Maya holds it to at most 32",
}

# Settable attributes Maya flags ``internal`` (attributeQuery), ruled one by one (plan Task 2, review S-7). Internal
# means the node stores or computes the value in its own code rather than in the data block; it doesn't make a
# write read a file or run code. The census measured no settable attribute flagged ``usedAsFilename``; such a hit
# isn't ruled here: it fails test_census until someone adds it by hand to DENIED_ATTRIBUTES or
# WRITE_DENIED_ATTRIBUTES. Measured in Maya 2025 (Rocky 8 and 9) and 2027 (macOS): the same 76 names.
_DRAW = "it changes only how the viewport draws the node; no file, no code, and no work that grows with it"
_OBJECT_COLOR = "it picks the node's wireframe colour in the viewport; no file and no code"
_SHADOWS = "a render switch for the shape's shadows; the same choice the Attribute Editor offers"
_TANGENTS = "it sets how the mesh's tangents are computed for shading; bounded, no file and no code"
_SELECT_HANDLE = "it moves the transform's selection handle, which only the viewport draws"
_QUATERNION = "a component of the transform's rotation as a quaternion; Maya keeps it in step with rotate"
FLAG_RULINGS = {
    "allowTopologyMod": "it lets history change the mesh's topology; the write tools' face budgets still hold",
    "backfaceCulling": _DRAW,
    "borderWidth": _DRAW,
    "castsShadows": _SHADOWS,
    "computeFromSculptCache": "it makes the mesh use its in-memory sculpt cache; no file (FILE_WORD_RULINGS)",
    "displayAlphaAsGreyScale": _DRAW,
    "displayBlueColorChannel": _DRAW,
    "displayBorders": _DRAW,
    "displayCenter": _DRAW,
    "displayColorAsGreyScale": _DRAW,
    "displayColors": _DRAW,
    "displayEdges": _DRAW,
    "displayFacesWithGroupId": _DRAW,
    "displayGreenColorChannel": _DRAW,
    "displayHandle": _DRAW,
    "displayImmediate": _DRAW,
    "displayInvisibleFaces": _DRAW,
    "displayItemNumbers": _DRAW,
    "displayLocalAxis": _DRAW,
    "displayMapBorders": _DRAW,
    "displayNonPlanar": _DRAW,
    "displayNormal": _DRAW,
    "displayRedColorChannel": _DRAW,
    "displayRotatePivot": _DRAW,
    "displayScalePivot": _DRAW,
    "displaySubdComps": _DRAW,
    "displayTangent": _DRAW,
    "displayTriangles": _DRAW,
    "displayUVs": _DRAW,
    "displayVertices": _DRAW,
    "doubleSided": "a render switch for whether both sides of each face render",
    "dynamics": "a legacy transform switch, not stored in the scene; no file and no code",
    "ghosting": "it turns ghosting on; frame counts are capped and ghostingMode is held to them (FILE_WORD_RULINGS)",
    "holdOut": "a render switch that makes the shape a matte",
    "ignoreHwShader": _DRAW,
    "inheritsTransform": "whether the transform follows its parent: a plain switch the artist sees in the viewport",
    "materialBlend": _DRAW,
    "mikktspaceTangentGen": _TANGENTS,
    "normalSize": _DRAW,
    "normalType": _DRAW,
    "objectColor": _OBJECT_COLOR,
    "objectColorB": _OBJECT_COLOR,
    "objectColorG": _OBJECT_COLOR,
    "objectColorR": _OBJECT_COLOR,
    "objectColorRGB": _OBJECT_COLOR,
    "opposite": "a render switch that flips which side of each face is the front",
    "receiveShadows": _SHADOWS,
    "relativeTweak": "whether the mesh's component tweaks are relative; it moves no data in or out of the scene",
    "rotateOrder": "the order the transform applies its rotations: an enum of six orders",
    "rotateQuaternionW": _QUATERNION,
    "rotateQuaternionX": _QUATERNION,
    "rotateQuaternionY": _QUATERNION,
    "rotateQuaternionZ": _QUATERNION,
    "selectHandle": _SELECT_HANDLE,
    "selectHandleX": _SELECT_HANDLE,
    "selectHandleY": _SELECT_HANDLE,
    "selectHandleZ": _SELECT_HANDLE,
    "selectionChildHighlighting": _DRAW,
    "smoothDrawType": _DRAW,
    "tangentNormalThreshold": _TANGENTS,
    "tangentSmoothingAngle": _TANGENTS,
    "tangentSpace": _TANGENTS,
    "template": "it draws the node greyed and unselectable, as Display > Object Display > Template does",
    "tweak": "whether the mesh applies its component tweaks; it moves no data in or out of the scene",
    "useMeshSculptCache": "it switches the mesh's in-memory sculpt cache; no file (FILE_WORD_RULINGS)",
    "useMeshTexSculptCache": "it switches the mesh's in-memory sculpt cache; no file (FILE_WORD_RULINGS)",
    "useObjectColor": _OBJECT_COLOR,
    "uvSize": _DRAW,
    "vertexBackfaceCulling": _DRAW,
    "vertexColorSource": _DRAW,
    "vertexSize": _DRAW,
    "visibility": "it shows or hides the node, which the Outliner shows; the plain visibility switch",
    "wireColorB": _OBJECT_COLOR,
    "wireColorG": _OBJECT_COLOR,
    "wireColorR": _OBJECT_COLOR,
    "wireColorRGB": _OBJECT_COLOR,
}


def type_denial(node_type: str) -> str | None:
    """Why the write tools never set or edit a node of ``node_type``, or ``None`` for a settable type."""
    if node_type in SETTABLE_TYPES:
        return None
    if node_type in DENIED_TYPES:
        return f"a {node_type} is never changed by Symbiote: {DENIED_TYPES[node_type]}"
    return f"a {node_type} isn't a node type Symbiote's write tools change"


def edit_type_denial(node_type: str) -> str | None:
    """Why the write tools never change a node of ``node_type`` (its membership included), or ``None``."""
    return None if node_type in EDITABLE_TYPES else type_denial(node_type)


def write_denial(attribute: str) -> str | None:
    """Why the write tools never set ``attribute``: anything ``denial`` refuses, and the write-only list."""
    return denial(attribute) or WRITE_DENIED_ATTRIBUTES.get(attribute)


def denial(attribute: str) -> str | None:
    """Why ``attribute`` is denied, or ``None``. ``attribute`` is a long name, without node or index."""
    if attribute in DENIED_ATTRIBUTES:
        return DENIED_ATTRIBUTES[attribute]
    if attribute.endswith(DENIED_SUFFIXES):
        return "its name says it holds a command or script"
    return None
