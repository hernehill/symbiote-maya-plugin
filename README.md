# Symbiote Chat for Maya

A docked Maya pane that opens Symbiote chat inside Maya, with local scene tools.

Download the latest release: <https://github.com/symbiote-labs/symbiote-maya-plugin/releases/latest>

Each release has two files: `symbiote-maya-plugin-X.Y.Z.zip` and its `.zip.sha256` checksum.

## Requirements

- Maya 2027 on macOS. Later releases add Maya 2025, 2026 and Linux.
- The `symbiote` CLI, new enough for the panel: `symbiote version` must print a `features:`
  line listing `tool-server-token`, `tool-tiers` and `busy-refusal`. Without them the panel turns the tools
  off, names what is missing and says to upgrade the CLI ([Update the CLI](#update-the-cli)). Install it the usual way:
  <https://github.com/symbiote-labs/symbiote-cli-dist>. Sign in with `symbiote login` first.
- The panel finds `symbiote` on your PATH, plus `/opt/homebrew/bin`, `/usr/local/bin` and
  `~/.local/bin`. A Maya started from the Dock does not inherit your shell's PATH or variables.
  If the CLI lives somewhere else, set `SYMBIOTE_CLI` to the full path of the executable in
  `~/Library/Preferences/Autodesk/maya/2027/Maya.env`, one line:
  `SYMBIOTE_CLI=/full/path/to/symbiote`. Maya reads that file at start. A shell `export` reaches
  only a Maya you start from that terminal.

## Update the CLI

A CLI installed a while ago is often too old for the panel. Check it in Terminal:

```sh
symbiote version
```

If the `features:` line is missing any of `tool-server-token`, `tool-tiers` or `busy-refusal`,
update it the same way you installed it. If `brew list symbiote` succeeds, it came from Homebrew;
otherwise it came from the install script:

- **Homebrew:**

  ```sh
  brew update && brew upgrade symbiote
  ```

- **The install script:** run it again; it installs the latest release over the old one:

  ```sh
  curl -fsSL https://raw.githubusercontent.com/symbiote-labs/symbiote-cli-dist/main/install.sh | sh
  ```

Then quit and restart Maya, so the panel picks up the new CLI.

## Install or upgrade

1. Download `symbiote-maya-plugin-X.Y.Z.zip` and `symbiote-maya-plugin-X.Y.Z.zip.sha256`
   from the latest release.
2. Check the download, in the folder that holds both files:
   `shasum -a 256 -c symbiote-maya-plugin-X.Y.Z.zip.sha256`
3. Quit Maya. A running Maya keeps the old code loaded.
4. Install. The same command upgrades an existing install:

   ```sh
   MODULES="$HOME/Library/Preferences/Autodesk/maya/modules"   # or a studio folder on MAYA_MODULE_PATH
   mkdir -p "$MODULES" && rm -rf "$MODULES/symbiote_chat" "$MODULES/symbiote_chat.mod" && unzip -o symbiote-maya-plugin-X.Y.Z.zip -d "$MODULES"
   ```

5. Start Maya and choose **Symbiote > Symbiote Chat**. The panel's status bar shows "Symbiote Chat X.Y.Z".

Do not double-click the zip. `symbiote_chat.mod` must sit directly in the modules folder: Maya ignores
a `.mod` file in a subfolder.

Install one copy only. If you installed from a source checkout, remove that module first. When two
copies are on `MAYA_MODULE_PATH`, the first folder wins, which may not be the one you meant. If that
developer install set the CLI's path for you, set `SYMBIOTE_CLI` in `Maya.env` again (see
Requirements), because removing the module takes that setting with it.

## Studio install (one shared copy)

A studio can install one copy for every artist instead of one per person.

1. Pick a folder every artist can read, for example `/studio/maya/modules`. Any name works.
   If the studio already sets `MAYA_MODULE_PATH`, use that folder.
2. Extract the zip into it once, with the install command above, setting `MODULES` to that
   folder. Upgrade and uninstall the same way, against the same folder.
3. Put that folder on `MAYA_MODULE_PATH` for every artist before Maya starts: in each artist's
   `~/Library/Preferences/Autodesk/maya/2027/Maya.env`, or in the studio's launcher. In `Maya.env`,
   keep what is already there by writing the line with the existing value after the new folder:

   ```
   MAYA_MODULE_PATH=/studio/maya/modules:$MAYA_MODULE_PATH
   ```

   Do not write `MAYA_MODULE_PATH=/studio/maya/modules` alone if the studio sets the variable
   elsewhere (a launcher or shell profile) and Maya.env is the only place you edit: check in
   Maya's Script Editor that `os.environ["MAYA_MODULE_PATH"]` still lists the studio's other folders.

Artists who get the shared copy must not also install a personal one.

## Use

Choose **Symbiote > Symbiote Chat**. The pane docks in Maya's layout, and comes back docked, signed in,
when Maya restarts. **Symbiote > Remove Symbiote Chat from the layout** forgets the saved pane.

A batch or render-farm Maya (`mayapy`, `maya -batch`) adds no menu and loads nothing of the plugin.

Symbiote's tools come in tiers: read tools are always on, import tools need the **Imports** switch,
and write tools need the **Scene changes** switch, off by default. Both switches are checkboxes by the
panel's status bar. Your studio decides first: when its server has writes turned off
(`WORKSTATION_WRITES_ENABLED` on the server), the switch is greyed out and you can't turn it on. A
switch you turn off refuses its tools at once.

| Tool | Tier | What it does |
|---|---|---|
| `maya_scene_info` | read | Describes the open scene: file, frame range, selection and top-level nodes. |
| `maya_list_objects` | read | Lists the cameras, lights, meshes or materials in the scene. |
| `maya_object_info` | read | Describes one node: type, parent, children, transform, numeric attributes, materials. |
| `maya_capture_viewport` | read | Captures the active 3D viewport. Sending the picture asks you first. |
| `maya_frame_object` | read | Frames one object by moving a startup camera (persp, top, front, side) only. |
| `maya_import_asset` | import | Imports an Alembic (`.abc`) or OBJ (`.obj`) file under a new group, once you confirm. Only these two formats. |
| `maya_create_object` | write | Creates a primitive, a camera or a light, with its starting values. |
| `maya_set_attribute` | write | Sets up to 50 attributes on one node; all are checked before any is set. |
| `maya_set_transform` | write | Moves, rotates or scales one transform, in object or world space. |
| `maya_select` | write | Replaces, adds to or removes from your selection. |
| `maya_assign_material` | write | Assigns an existing material, or a new standardSurface, openPBRSurface or lambert, to meshes. |
| `maya_group` | write | Puts transforms in a new group, or under an existing transform. |
| `maya_delete_object` | write | Deletes one object and everything below it, once you confirm. |
| `maya_save_scene` | write | Saves the scene over its own file, once you confirm. |

The write tools run no code. Each checks the whole call before it changes anything, and one Ctrl-Z
undoes it (a save and an import can't be undone: delete the imported group instead). They work only
on what this plugin measured in Maya 2027, and a refusal says why.

## Uninstall

1. In Maya, if the pane is docked, choose **Symbiote > Remove Symbiote Chat from the layout**.
   Otherwise Maya's saved layout keeps a reference to files that are gone.
2. Quit Maya.
3. Remove the files, with `MODULES` set as in the install command:

   ```sh
   rm -rf "$MODULES/symbiote_chat" "$MODULES/symbiote_chat.mod"
   ```
