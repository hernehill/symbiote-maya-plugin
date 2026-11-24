"""Install for REZ"""

# Standard
import sys

print("Appending rez_build_api.py to sys.path")
if sys.platform.startswith("linux"):
    sys.path.append("/mnt/tools/rez_pipe/hh_rez_pckSetup")
else:
    sys.path.append("T:/rez_pipe/hh_rez_pckSetup")
import rez_build_api


# -----------------------------------------------
# The lists below will be 'copied' or 'symlinked' to destination.
# If building locally, user has the option to build with symlink.
# If releasing, symlink is not allowed.

DIRECTORY_LIST = ["symbiote_chat"]  # destination will preserve full sub-paths
LEAF_DIRS = []  # only the leaf subdir will be copied/symlinked
FILE_LIST = ["symbiote_chat.mod"]
DO_NOT_CLEAR_LIST = []  # during local build, do not remove these files/sudbirs


if __name__ == "__main__":

    rez_build_api.build(
        DIRECTORY_LIST,
        FILE_LIST,
        has_otls=False,
        leafs=LEAF_DIRS,
        do_not_clear=DO_NOT_CLEAR_LIST
    )
