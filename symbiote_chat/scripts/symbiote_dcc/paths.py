"""Path checks every DCC's import tool shares: a path is judged from its text before it is used.

Opening or even stat-ing a ``\\\\server\\share`` path signs in to that server on Windows, so
``check_local_path`` reads only the string, before the artist is asked. ``resolve_checked``
then follows the path's links one component at a time, checking each prefix and each link's
target as text before it ``lstat``s anything, so a link never leads it onto a share. On Windows
a link's target must be a drive path or a relative one. The Windows rules follow CPython's
documented ``lstat``/``readlink`` behaviour and are tested against a fake disk, not a Windows box.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Callable
from types import ModuleType
from typing import Any

NETWORK_PATH = (
    "Symbiote does not import from a network path ({path}): opening it would sign in to "
    "that server. Copy the file to this workstation and import the local copy."
)
DRIVE_LETTER = (
    "Symbiote imports only from a local drive letter on Windows, such as C:\\, "
    "and {path} has none. Give the absolute path to the file."
)
EXPRESSION_PATH = (
    "Symbiote does not expand $variables or `expressions` in a path ({path}): {reason}. "
    "Give the absolute path to the file."
)
HIDDEN_CHARACTER_PATH = (
    "A file path cannot contain control or text-direction characters ({path!r}): they "
    "change how the path reads in the confirmation dialog. Give the absolute path to the file."
)
ILLEGAL_PATH = (
    'A file path cannot contain < > " | ? or * ({path}): Windows allows none of them '
    "in a file name. Give the absolute path to the file."
)
DEVICE_PATH = (
    "Symbiote does not import from an extended-length or device path ({path}). Give the "
    "ordinary path, such as C:\\assets\\hero.abc."
)

LINKED_PATH = "{path} goes through a link to {target}, so nothing was imported: {reason}"
UNREADABLE = "Symbiote could not read {path} ({error}), so nothing was imported."
TOO_MANY_LINKS = "{path} goes through more than {limit} links, so nothing was imported."
NOT_ON_A_DRIVE = (
    "{path} goes through the Windows link {link} to {target}, which is not a folder on a drive or a relative"
    " path. Symbiote follows only those, because any other link can sign in to a server or open a device,"
    " so nothing was imported."
)
ABOVE_THE_DRIVE = "{path} goes through a link that climbs above the drive's root, so nothing was imported."
UNKNOWN_LINK = "{path} is a kind of Windows link Symbiote can't follow, so nothing was imported."

# Windows marks a junction only by these (CPython reports S_IFLNK for symlinks alone); named
# here because the stat module lacks them before 3.8 and on non-Windows builds of some versions.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_LINK_TAGS = (0xA000000C, 0xA0000003)  # IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT (junction)
_NAME_SURROGATE = 0x20000000  # the tag bit for "this reparse point names another path"
# Linux's own limit on links followed in one lookup (MAXSYMLINKS).
MAX_LINKS = 40

_ILLEGAL_CHARACTERS = '<>"|?*'
# controls, format (bidi, zero-width), line/paragraph separators, lone surrogates, private use, unassigned
_HIDDEN_CATEGORIES = frozenset(("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"))
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]+:")  # two or more characters, so C: is a drive
_AUTOFS_PREFIX = "/net/"


def check_local_path(
    path: str,
    *,
    windows: bool,
    display_name: str,
    expression_chars: str,
    expression_reason: str,
) -> None:
    """Refuse a path whose first use would reach another machine, without touching it.

    On Windows, opening ``\\\\server\\share`` (or ``//server/share``) signs in to that
    server with the artist's NTLM credentials, so these are refused from the string
    alone. On Windows a path must also start with a drive letter; a mapped network
    drive still passes, as telling it apart would mean asking the OS about it.

    A DCC may evaluate a file path when it loads it, so the path loaded would not be the
    one the artist confirmed. Each character in ``expression_chars`` is refused for that
    reason, and ``expression_reason`` (which may use ``{display_name}``) says why. There
    is no default: what a DCC expands is its own fact.

    ``< > " | ? *`` are refused on every OS: no Windows file name holds them, and the
    path is shown in the confirmation dialog, which must not get markup. Control
    and text-direction characters are refused for the same reason: a newline or U+202E
    could make the dialog read as another path, so every character in the Unicode
    categories Cc, Cf (bidi and zero-width), Zl, Zp, Cs (lone surrogates), Co (private
    use) and Cn (unassigned) is refused.

    This stays a denylist: an allowlist of ASCII names would refuse ordinary artist
    paths with accented or non-Latin folder names, ``&``, ``#``, ``(`` or ``~``.
    """
    if has_hidden_character(path):
        raise ValueError(HIDDEN_CHARACTER_PATH.format(path=path))
    if any(character in path for character in expression_chars):
        reason = expression_reason.format(display_name=display_name)
        raise ValueError(EXPRESSION_PATH.format(path=path, reason=reason))
    backslashed = path.replace("/", "\\")
    if backslashed.startswith(("\\\\?\\", "\\\\.\\")) and not backslashed[4:].upper().startswith("UNC\\"):
        raise ValueError(DEVICE_PATH.format(path=path))
    if path.startswith(("\\\\", "//")):
        raise ValueError(NETWORK_PATH.format(path=path))
    if any(character in path for character in _ILLEGAL_CHARACTERS):
        raise ValueError(ILLEGAL_PATH.format(path=path))
    if windows and not _has_drive_letter(path):
        raise ValueError(DRIVE_LETTER.format(path=path))


def has_hidden_character(text: str) -> bool:
    """Whether ``text`` holds a character in Unicode category Cc, Cf, Zl, Zp, Cs, Co or Cn.

    Cc is U+0000-U+001F, U+007F and the C1 controls; Cf the bidi and zero-width characters;
    Cs a lone surrogate, Co private use and Cn a code point Unicode has not assigned, none of
    which another program is sure to read the same way.
    """
    return any(unicodedata.category(c) in _HIDDEN_CATEGORIES for c in text)


def is_network_reference(value: str, *, windows: bool) -> bool:
    """Whether ``value`` names another machine: ``//``, ``\\\\``, a URL scheme, or ``/net/``.

    ``/net/`` is the autofs mount point, which mounts the server the moment it is opened
    (not on Windows, where it is an ordinary rooted path), in any letter case, and ``/net``
    itself counts too. ``/Volumes/`` is not network: it
    is already mounted, and the artist can confirm a file there.
    """
    if value.startswith(("//", "\\\\")) or _URL_SCHEME.match(value):
        return True
    folded = value.casefold()  # macOS's default file system reaches /net/ as /NET/ or /Net/ too
    return not windows and (folded == _AUTOFS_PREFIX.rstrip("/") or folded.startswith(_AUTOFS_PREFIX))


def autofs_server(value: str) -> str | None:
    """The server ``value`` names under ``/net/``, in ASCII lower case, or ``None``.

    ``None`` also when the server name is not ASCII: Unicode case folding maps ``ß`` to ``ss``
    and the Kelvin sign to ``k``, so two different hosts could compare equal. Such a name never
    matches, which refuses the link (``is_network_reference`` still calls it network).
    """
    if value[: len(_AUTOFS_PREFIX)].lower() != _AUTOFS_PREFIX:
        return None
    server = value[len(_AUTOFS_PREFIX) :].split("/")[0]
    return server.lower() if server and server.isascii() else None


def _has_drive_letter(path: str) -> bool:
    return len(path) >= 3 and path[0].isascii() and path[0].isalpha() and path[1] == ":" and path[2] in "\\/"


class _Walk:
    """``resolve_checked``'s settings: the DCC's words for ``check_local_path`` and the OS calls."""

    def __init__(
        self,
        windows: bool,
        words: dict[str, str],
        lstat: Callable[[str], Any],
        readlink: Callable[[str], str],
        flavour: ModuleType,
    ) -> None:
        self.windows, self.words, self.lstat, self.readlink, self.flavour = windows, words, lstat, readlink, flavour
        self.confirmed_server: str | None = None  # the autofs server the artist chose, if any

    def check_confirmed(self, text: str) -> None:
        """For text the artist saw: the step-2 rules only, so an autofs ``/net/`` path they chose stays allowed."""
        check_local_path(text, windows=self.windows, **self.words)

    def check(self, text: str, *, prefix: bool = False) -> None:
        """For text a link supplied, which the artist never saw: also no network reference, ``/net/`` included.

        The one exception: a ``/net/<server>/`` path on the server the artist confirmed, which
        contacts no new machine (``latest.abc -> v003.abc`` in a share on NFS).
        """
        self.check_confirmed(text)
        if not is_network_reference(text, windows=self.windows):
            return
        if prefix and self.confirmed_server is not None and text.casefold() == _AUTOFS_PREFIX.rstrip("/"):
            return  # passing through the autofs root on the way to the confirmed server mounts nothing
        server = None if self.windows else autofs_server(text)
        if server is None or server != self.confirmed_server:
            raise ValueError(NETWORK_PATH.format(path=text))

    def is_link(self, candidate: str) -> bool:
        try:
            info = self.lstat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            return False  # nothing there to follow; the caller's isfile names the missing file
        except OSError as exc:
            raise ValueError(UNREADABLE.format(path=candidate, error=exc)) from exc
        if stat.S_ISLNK(info.st_mode):
            return True
        if not self.windows or not getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        # A junction is a directory with a reparse tag; os.readlink reads it like a symlink. Any
        # other tag that names another path is one this walk can't read, so it is refused. Tags
        # that don't name a path (a OneDrive placeholder, a deduplicated file) are the file itself.
        tag = getattr(info, "st_reparse_tag", 0)
        if tag in _LINK_TAGS:
            return True
        if tag & _NAME_SURROGATE:
            raise ValueError(UNKNOWN_LINK.format(path=candidate))
        return False

    def read_link(self, link: str) -> str:
        try:
            target = self.without_local_prefix(self.readlink(link))
        except OSError as exc:
            raise ValueError(UNREADABLE.format(path=link, error=exc)) from exc
        return target

    def on_a_drive_or_relative(self, target: str) -> bool:
        if _has_drive_letter(target):
            return True
        drive, rest = self.flavour.splitdrive(target)
        return not drive and rest[:1] not in ("\\", "/")

    def split(self, text: str) -> tuple[str, list[str]]:
        """``text``'s root (``/``, ``C:\\`` or ``""`` when relative) and its components."""
        flavour = self.flavour
        drive, rest = flavour.splitdrive(text)
        separators = flavour.sep + (flavour.altsep or "")
        root = drive + flavour.sep if rest[:1] in tuple(separators) and rest else drive
        parts = re.split("[" + re.escape(separators) + "]", rest)
        return root, [part for part in parts if part not in ("", ".")]

    def without_local_prefix(self, text: str) -> str:
        # Windows reports a local link target as \\?\C:\...; any other \\?\ or \\.\ form stays and is refused.
        if self.windows and text.startswith("\\\\?\\") and _has_drive_letter(text[4:]):
            return text[4:]
        return text


def resolve_checked(
    path: str,
    *,
    windows: bool,
    display_name: str,
    expression_chars: str,
    expression_reason: str,
    lstat: Callable[[str], Any] | None = None,
    readlink: Callable[[str], str] | None = None,
    flavour: ModuleType | None = None,
) -> str:
    """The path ``path`` really names, its links followed as the OS would; ``ValueError`` if any is refused.

    It walks the absolute path one component at a time, as the kernel does, checking each prefix
    as text before it is ``lstat``ed. A prefix that is still the text the artist confirmed only
    has to pass ``check_local_path``, so a confirmed autofs path under ``/net/`` is allowed, as
    it always was. A link's target is read with ``readlink`` and must also not be a network
    reference (``/net/`` included), as must every prefix after it: the artist never saw them.
    The exception is a target on the same ``/net/<server>/`` as the confirmed path (an ASCII
    server name, compared case-insensitively): the artist already chose that server, so no new machine is contacted.
    The target is spliced in (an absolute
    target replaces what was resolved, a relative one continues from the link's folder), so a
    later ``..`` steps back from the real folder, not the link's name. At most ``MAX_LINKS``
    links are followed. An ``lstat`` or ``readlink`` that fails for another reason than a
    missing file refuses the path. On Windows the confirmed path is first normalised with ``normpath``,
    because Win32 removes ``.`` and ``..`` as text before it follows a reparse point; elsewhere
    ``..`` is resolved after the links before it, as the kernel does. ``lstat``, ``readlink``
    and ``flavour`` (``posixpath`` or ``ntpath``) are for tests; they default to this OS's.
    """
    words = {"display_name": display_name, "expression_chars": expression_chars, "expression_reason": expression_reason}
    walk = _Walk(windows, words, lstat or os.lstat, readlink or os.readlink, flavour or os.path)
    start = walk.without_local_prefix(path)
    if windows:  # Win32 collapses . and .. as text before it follows any junction or link
        start = walk.flavour.normpath(start)
    walk.check_confirmed(start)
    walk.confirmed_server = None if windows else autofs_server(start)
    absolute = start if walk.split(start)[0] else walk.flavour.join(os.getcwd(), start)
    root, pending = walk.split(absolute)
    return _walk(walk, path, root, pending)


def _walk(walk: _Walk, path: str, resolved: str, pending: list[str]) -> str:
    links = 0
    while pending:
        name, pending = pending[0], pending[1:]
        if name == "..":
            parent = walk.flavour.dirname(resolved)
            if walk.windows and links and parent == resolved:
                # Win32 would stop at the root; what the OS does inside a link's target is unmeasured.
                raise ValueError(ABOVE_THE_DRIVE.format(path=path))
            resolved = parent
            continue
        candidate = walk.flavour.join(resolved, name)
        if links:
            walk.check(candidate, prefix=True)
        else:
            walk.check_confirmed(candidate)
        if not walk.is_link(candidate):
            resolved = candidate
            continue
        links += 1
        if links > MAX_LINKS:
            raise ValueError(TOO_MANY_LINKS.format(path=path, limit=MAX_LINKS))
        target = walk.read_link(candidate)
        if walk.windows and not walk.on_a_drive_or_relative(target):
            # os.readlink rewrites only a leading \??\: an NT path such as \Device\Mup\h\s comes
            # back rooted with no drive, and joining it onto C: would hide where the OS goes.
            raise ValueError(NOT_ON_A_DRIVE.format(path=path, link=candidate, target=target))
        joined = walk.flavour.join(resolved, target)
        try:
            walk.check(joined)
        except ValueError as refused:
            raise ValueError(LINKED_PATH.format(path=path, target=target, reason=refused)) from refused
        if walk.split(target)[0]:  # absolute: start again from its root
            resolved, parts = walk.split(joined)
        else:  # relative: continue from the link's folder
            parts = walk.split(target)[1]
        pending = parts + pending
    return resolved
