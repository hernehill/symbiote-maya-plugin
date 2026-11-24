# Symbiote Chat for Maya

A docked Maya pane that opens Symbiote chat inside Maya, with local scene tools.

This is a developer preview: only Maya 2027.2 or later on macOS is measured.

## Install

For install, upgrade and uninstall, follow <https://github.com/symbiote-labs/symbiote-maya-plugin#readme>.
In short: quit Maya, then unpack the release zip into Maya's modules folder. This replaces an earlier copy of the
plugin there:

    MODULES="$HOME/Library/Preferences/Autodesk/maya/modules"   # or a studio folder on MAYA_MODULE_PATH
    mkdir -p "$MODULES" && rm -rf "$MODULES/symbiote_chat" "$MODULES/symbiote_chat.mod" && unzip -o symbiote-maya-plugin-X.Y.Z.zip -d "$MODULES"

Replace `X.Y.Z` with the version in the zip's name. Start Maya and choose **Symbiote > Symbiote Chat**. To
remove the plugin, quit Maya and delete `symbiote_chat` and `symbiote_chat.mod` from that folder; if the pane was
docked, choose **Symbiote > Remove Symbiote Chat from the layout** first.

## Requirements

- Maya 2027.2 or later on macOS.
- The `symbiote` CLI, new enough for the panel: `symbiote version` must print a `features:` line listing
  `tool-server-token`, `tool-tiers` and `busy-refusal`. Without them the panel turns the tools off, names what
  is missing and says to upgrade the CLI. Sign in with `symbiote login` first.
- If Maya cannot find `symbiote`, set the `SYMBIOTE_CLI` environment variable to its full path before Maya
  starts. A Maya launched from the Dock does not read your shell profile, so set it in
  `~/Library/Preferences/Autodesk/maya/2027/Maya.env` (one line, `SYMBIOTE_CLI=/full/path/to/symbiote`).

## Use

Choose **Symbiote > Symbiote Chat**. The pane docks in Maya's layout, and comes back docked, signed in, when
Maya restarts. **Symbiote > Remove Symbiote Chat from the layout** forgets the saved pane.

A batch or render-farm Maya (`mayapy`, `maya -batch`) adds no menu and loads nothing of the plugin.

## Tools

| Tool | Tier | What it does |
|---|---|---|
| `maya_scene_info` | read | Describes the open scene: file, frame range, selection and top-level nodes. |
| `maya_list_objects` | read | Lists the cameras, lights, meshes or materials in the scene. |
| `maya_object_info` | read | Describes one node: type, parent, children, transform, numeric attributes, materials. String values are never returned. |
| `maya_capture_viewport` | read | Captures the active 3D viewport. Sending the picture asks you first. |
| `maya_frame_object` | read | Frames one object by moving a startup camera (persp, top, front, side) only. |
| `maya_import_asset` | import | Imports an Alembic (`.abc`) or OBJ (`.obj`) file under a new group, once you confirm. |
| `maya_create_object` | write | Creates a primitive (polyCube, polySphere, polyCylinder, polyCone, polyPlane, polyTorus), a camera or a light, with its starting values. |
| `maya_set_attribute` | write | Sets up to 50 attributes on one node; all are checked before any is set. |
| `maya_set_transform` | write | Moves, rotates or scales one transform, to the values given or by them (`relative`), in object or world space. |
| `maya_select` | write | Replaces, adds to or removes from your selection. |
| `maya_assign_material` | write | Assigns an existing material, or a new standardSurface, openPBRSurface or lambert, to meshes. |
| `maya_group` | write | Puts transforms in a new group, or under an existing transform. |
| `maya_delete_object` | write | Deletes one object and everything below it, once you confirm; the dialog lists what goes. |
| `maya_save_scene` | write | Saves the scene over its own file, once you confirm. |

## The panel's switches: Imports and Scene changes

Two checkboxes next to the panel's status bar decide what Symbiote may do in your scene. Read tools are always on.

- **Imports** lets Symbiote import a file. You confirm each import in Maya.
- **Scene changes** turns on the write tools above. Off by default.

Your studio decides first: when it has imports or scene changes off, that switch is greyed out and you can't turn
it on. A switch you turn off refuses its tools at once, including a call already waiting for Maya.

## Scene changes: what the write tools may do

The write tools run no code. Each one checks the whole call before it changes anything, and makes its change as
one undo step, so one Ctrl-Z takes it back (a save can't be undone). If anything in the call is refused, nothing
changes, and the refusal says why. They refuse:

- a Maya version whose attributes Symbiote hasn't measured (only Maya 2027 so far), and any change while undo
  is switched off;
- nodes from a referenced file, locked nodes, Maya's own default nodes (lambert1, initialShadingGroup) and the
  startup cameras, and any type outside the primitives, their meshes and transforms, cameras, lights and the
  three materials;
- an attribute Maya didn't define on that type (aliases, short names, added and Arnold `ai*` attributes), one
  that can carry code or a path, or one that hides or freezes a node;
- a value of the wrong kind, outside Maya's own limits, or over Symbiote's caps: subdivisions at most 200, a
  create of at most 1,000,000 faces, and a smoothed mesh of at most 4,000,000 faces.

Selecting changes no node, so any node can be selected by its exact name. Delete and save ask you first, every
time, and check again after you confirm that the scene is still what the dialog showed.

## Importing

Symbiote imports Alembic and OBJ files only, and only after you press **Import** in Maya's confirmation. Then it
checks everything the file brought. If any of it can carry code (a camera's `homeCommand`, a right-click
command), a path, a string the file added, or heavy work (a high smoothing level, an animated property), Symbiote
deletes all of it, takes back anything it added to nodes already in your scene, and says why.

- **Ctrl-Z can't remove an import.** Delete the group to remove it. A refused import never reaches the undo
  queue, so Ctrl-Z can't bring it back, and your undo history is kept.
- **An OBJ needs a saved scene.** Importing an OBJ clears Maya's undo history, so Symbiote asks you to save first.
  An OBJ's material files must sit in its own folder.
- **Maya scenes (`.ma`, `.mb`) are never imported:** a Maya scene can carry script nodes.
- The defaults Symbiote compares imports against are measured per Maya year and platform, and imports stay off
  in a Maya that hasn't been measured. mtoa's defaults are measured per mtoa version: with another mtoa, or a
  renderer that hasn't been measured (Redshift, RenderMan), the strings it adds make Symbiote refuse the import.
- An OBJ whose material names a texture is refused for now, and so are Alembic node types Symbiote hasn't
  measured yet; the refusal names them.
- **An Alembic whose face sets are named like your shading groups is refused.** Alembic puts a face set into an
  existing shading group of the same name and adds an `AbcFacesetName` attribute to it; Symbiote never changes
  your shading groups, so it takes that back and refuses. Faces left on Maya's default material
  (`initialShadingGroup`) import normally, and the attribute Alembic added to that group is removed.
- The check reads the new nodes and every connection into or out of them. A connection an import makes between
  two nodes that were already in the scene is not checked. Maya makes exactly one kind (measured on every
  fixture): when an import adds a shading group, it links Maya's default light set to it in the light linker
  (`defaultLightSet.message` into `lightLinker1.link[]` and `shadowLink[]`). That is bookkeeping that carries no
  value and no path, so it is accepted. Checking for others would mean reading every connection in the scene
  before each import. Existing nodes can still be changed another way: an Alembic face set adds an attribute to
  an existing shading group of its name, which Symbiote checks and takes back (see above).
- FBX and USD import aren't supported yet.
