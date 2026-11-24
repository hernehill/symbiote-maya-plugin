import maya.cmds as cmds
import maya.utils

if not cmds.about(batch=True):
    def _menu():
        from symbiote_maya import host_check
        reason = host_check.unsupported_reason()
        if reason:
            host_check.install_refusal_menu(reason)
        else:
            from symbiote_maya import dock
            dock.install_menu()
    maya.utils.executeDeferred(_menu)
