"""Rewriting a viewport capture as an opaque RGB PNG, for a DCC whose render keeps an alpha channel.

Qt is imported only when a picture is rewritten, so the module imports without PySide6.
"""

from __future__ import annotations

from pathlib import Path


def drop_alpha(path: Path) -> None:
    """Rewrite the PNG at ``path`` as RGB, dropping alpha without compositing over a colour.

    A viewport render can leave alpha at 0 over the background, which a viewer then shows as
    transparent; the colour channels already hold what the artist sees. Raises ``ValueError``
    when the file cannot be read or written.
    """
    from PySide6 import QtGui

    image = QtGui.QImage(str(path))
    if image.isNull():
        raise ValueError(f"Qt could not read the capture at {path}")
    flat = image.convertToFormat(QtGui.QImage.Format.Format_RGB888)
    if not flat.save(str(path), "PNG"):
        raise ValueError(f"Qt could not write the capture at {path}")
