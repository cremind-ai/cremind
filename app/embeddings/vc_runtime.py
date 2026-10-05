"""Windows: make sure PyTorch gets a Visual C++ runtime it can load.

PyTorch's Windows wheels use the Microsoft Visual C++ runtime installed on
the PC, and they need a recent one. torch 2.9+ is built with MSVC 14.42,
whose ``c10.dll`` crashes while it initialises against an ``msvcp140.dll``
older than 14.40 (the constexpr ``std::mutex`` of VS 2022 17.10). Windows
reports that as ``[WinError 1114] A dynamic link library (DLL)
initialization routine failed``. torch 2.13+ also imports
``vcruntime140_threads.dll``, which only ships with 14.38+. Plenty of
Windows 10 PCs, and some Windows 11 ones, carry an older Visual C++
Redistributable, and updating it takes an administrator.

So :func:`prepare` runs right before the embedding providers import
``sentence_transformers`` (and with it torch). When the system runtime is
too old, it loads a private one instead: Microsoft's signed DLLs from the
``msvc-runtime`` wheel, pip-installed into Cremind's own venv the first time
they are needed. Windows binds a DLL import to the already-loaded module of
the same name, so torch then runs on the private copy. No admin, no reboot.
A PC with a current runtime is left alone, and all of this is a no-op off
Windows.

When that can't work (offline, no wheel for this Python, or an older
runtime already loaded into the process), importing torch still fails, and
:func:`explain_load_error` turns its bare WinError into what to do.
"""

from __future__ import annotations

import ctypes
import functools
import os
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass

from app.utils.logger import logger

# What torch needs: 14.40 for c10.dll's mutexes (torch 2.9+). A 14.40 runtime
# also carries vcruntime140_threads.dll (14.38+), which torch 2.13+ imports.
MIN_VERSION = (14, 40)

# Microsoft's latest x64 runtime installer: the fix that needs an admin.
REDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"

# The private runtime: one release, checked by hash, because its DLLs end up
# inside the Cremind process. Each wheel holds Microsoft-signed DLLs
# (14.44.35211) and installs them into the venv's own folder (the install
# scheme's ``data`` path). To bump it, take the new release's sha256 digests
# from https://pypi.org/pypi/msvc-runtime/<version>/json.
PRIVATE_RUNTIME = "msvc-runtime==14.44.35112"
_PRIVATE_RUNTIME_SHA256 = (
    "d4f6cf106aaf235f2a90952f9ec7de49e9946323880d051abf9745a6d4bf60bf",  # cp313 win_amd64
    "359152dc9769559fee4ffdaa19f1fc2b72ab1535631a6105116776b263a235ac",  # cp313 win_arm64
    "af179a6c552070e660493765efd5856283af3fed971ddc3577203a7d3f1ee8e7",  # cp314 win_amd64
    "81eb9346ab2a269ea934bf13492fe5c1cd50af871a2fbb973f0a514031e81b6a",  # cp314 win_arm64
    "1105f8f8117ee210b85bd8c771dbefff8f8d35771ee7ce3291ddd91d2a03eff1",  # cp314t win_amd64
    "a2ab094e35fa04172f6fd5cfc7d2a2017755ea9c3ff39cad5c2a4456e0bff689",  # cp314t win_arm64
)

# Besides msvcp140.dll, the runtime DLLs torch imports.
_COMPANIONS = ("vcruntime140_threads.dll", "msvcp140_atomic_wait.dll")

# What the private runtime contributes, in load order: msvcp140.dll first,
# because the others import it by name. vcruntime140.dll is not here: Python
# loaded its own copy before any of this runs.
_PRIVATE_DLLS = (
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140_threads.dll",
)

# How Windows reports a DLL that won't load: not found (126), a missing
# export (127), or a failed initialisation (1114).
_DLL_LOAD_ERRORS = frozenset({126, 127, 1114})

_WINDOWS = sys.platform == "win32"

_lock = threading.Lock()
# The private DLLs stay referenced for the life of the process.
_private_handles: list = []


@dataclass(frozen=True)
class Runtime:
    """One copy of the C++ runtime, named by its ``msvcp140.dll``."""

    path: str
    # ``None`` when the DLL is missing or its version can't be read.
    version: tuple[int, int, int, int] | None
    # Companion DLLs torch imports that are not beside it.
    missing: tuple[str, ...] = ()

    @property
    def new_enough(self) -> bool:
        return self.version is not None and self.version[:2] >= MIN_VERSION

    @property
    def current(self) -> bool:
        return self.new_enough and not self.missing

    @property
    def label(self) -> str:
        return ".".join(map(str, self.version)) if self.version else "missing"

    def describe(self) -> str:
        """What this runtime is, completing "this PC has …"."""
        if self.version is None:
            return "none installed"
        if self.new_enough and self.missing:
            return f"version {self.label} without {', '.join(self.missing)}"
        return f"version {self.label}"


def prepare(*, install: bool = True) -> str:
    """Line up a Visual C++ runtime torch can load. Call before importing it.

    Returns what it found or did, for :func:`explain_load_error` and logs:

    - ``"n/a"``: not Windows.
    - ``"already-loaded"``: an ``msvcp140.dll`` is already in the process.
      Whatever it is, torch will bind to it; nothing can change that now.
    - ``"system"``: the system runtime is current; torch will load it.
    - ``"private"``: the system runtime is too old, and the private one is
      now loaded in its place (pip-installed first when missing, if
      ``install``).
    - ``"outdated"``: the system runtime is too old and no private one could
      be loaded, so importing torch will most likely fail.
    - ``"unknown"``: the check itself failed. Never raises: a broken check
      must not stop a PC whose runtime is fine.
    """
    if not _WINDOWS:
        return "n/a"
    with _lock:
        try:
            return _prepare_locked(install)
        except Exception:  # noqa: BLE001 — see the docstring
            logger.exception("[vc-runtime] could not check the Visual C++ runtime")
            return "unknown"


def _prepare_locked(install: bool) -> str:
    if _loaded_runtime() is not None:
        return "already-loaded"
    system = _system_runtime()
    if system.current:
        return "system"
    private = _private_runtime()
    if not private.current and install and _install_private_runtime():
        private = _private_runtime()
    if private.current and _load_private(private):
        logger.info(
            f"[vc-runtime] the system Visual C++ runtime ({system.describe()}, "
            f"{system.path}) is older than PyTorch needs; loaded Cremind's private "
            f"runtime {private.label} from {os.path.dirname(private.path)}"
        )
        return "private"
    logger.warning(
        f"[vc-runtime] the system Visual C++ runtime ({system.describe()}, "
        f"{system.path}) is older than PyTorch needs ({_min_label()}+), and no "
        "private runtime could be loaded; PyTorch will most likely fail to load"
    )
    return "outdated"


def explain_load_error(exc: BaseException, status: str) -> str | None:
    """What to do about a torch import that failed on the C++ runtime.

    ``status`` is what :func:`prepare` returned before the import. Returns
    ``None`` unless ``exc`` is a Windows DLL load failure and the runtime
    torch got is too old: otherwise the failure has another cause, and
    torch's own error says more than a guess would. Never raises.
    """
    if not _WINDOWS or getattr(exc, "winerror", None) not in _DLL_LOAD_ERRORS:
        return None
    try:
        if status == "outdated":
            return (
                f"PyTorch needs the Microsoft Visual C++ runtime {_min_label()} or "
                f"newer, but this PC has {_system_runtime().describe()}. Install the "
                f"latest Microsoft Visual C++ Redistributable from {REDIST_URL}, "
                "then restart Cremind."
            )
        if status == "already-loaded":
            loaded = _loaded_runtime()
            # An unreadable version proves nothing either way.
            if loaded is None or loaded.version is None or loaded.current:
                return None
            return (
                f"PyTorch needs the Microsoft Visual C++ runtime {_min_label()} or "
                f"newer, but this Cremind process had already loaded "
                f"{loaded.describe()} ({loaded.path}). Restart Cremind so it can "
                "load a newer one first. If this error comes back, install the "
                f"latest Microsoft Visual C++ Redistributable from {REDIST_URL}, "
                "then restart Cremind."
            )
    except Exception:  # noqa: BLE001 — torch's own error still stands
        logger.exception("[vc-runtime] could not explain a PyTorch load failure")
    return None


def _min_label() -> str:
    return ".".join(map(str, MIN_VERSION))


# ── finding runtimes ────────────────────────────────────────────────────────

def _loaded_runtime() -> Runtime | None:
    """The ``msvcp140.dll`` already loaded in this process, or ``None``."""
    kernel32 = _kernel32()
    handle = kernel32.GetModuleHandleW("msvcp140.dll")
    if not handle:
        return None
    buf = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetModuleFileNameW(handle, buf, len(buf)):
        return Runtime("msvcp140.dll", None)
    return Runtime(buf.value, _file_version(buf.value))


def _system_runtime() -> Runtime:
    buf = ctypes.create_unicode_buffer(260)
    if _kernel32().GetSystemDirectoryW(buf, len(buf)):
        system_dir = buf.value
    else:
        system_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    return _runtime_in(system_dir)


def _private_dir() -> str:
    # Where pip puts a wheel's ``.data/data`` files: this venv's own folder.
    return sysconfig.get_path("data")


def _private_runtime() -> Runtime:
    return _runtime_in(_private_dir())


def _runtime_in(directory: str) -> Runtime:
    path = os.path.join(directory, "msvcp140.dll")
    version = _file_version(path) if os.path.isfile(path) else None
    missing = tuple(n for n in _COMPANIONS if not os.path.isfile(os.path.join(directory, n)))
    return Runtime(path, version, missing)


# ── installing and loading the private runtime ──────────────────────────────

def _install_private_runtime() -> bool:
    """pip-install the pinned private runtime into this venv. Never raises."""
    # Lazy: the upgrade runner pulls in config and settings, and this path
    # only runs on a PC whose runtime is too old.
    from app.upgrade.runner import _pip_install

    logger.info(f"[vc-runtime] installing {PRIVATE_RUNTIME} into {_private_dir()}")
    try:
        with tempfile.TemporaryDirectory(prefix="cremind-vc-") as tmp:
            requirements = os.path.join(tmp, "requirements.txt")
            with open(requirements, "w", encoding="utf-8") as f:
                f.write(_requirements_line() + "\n")
            # Always PyPI: the test channel adds Test PyPI, where anyone can
            # publish under this name. The hashes would refuse a stranger's
            # file anyway, so the index there is only ever noise.
            _pip_install(["-r", requirements], None, channel="production", upgrade=False)
    except Exception as exc:  # noqa: BLE001 — pip failures surface as RuntimeError
        logger.warning(f"[vc-runtime] could not install {PRIVATE_RUNTIME}: {exc}")
        return False
    return True


def _requirements_line() -> str:
    """A requirements-file line that puts pip in hash-checking mode."""
    hashes = " ".join(f"--hash=sha256:{digest}" for digest in _PRIVATE_RUNTIME_SHA256)
    return f"{PRIVATE_RUNTIME} {hashes}"


def _load_private(private: Runtime) -> bool:
    """Load the private DLLs, so later imports by name bind to them.

    True once its ``msvcp140.dll`` is loaded; a companion that fails after
    that is only logged, since torch may not need it.
    """
    directory = os.path.dirname(private.path)
    for name in _PRIVATE_DLLS:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            _private_handles.append(ctypes.WinDLL(path))
        except OSError as exc:
            logger.warning(f"[vc-runtime] could not load {path}: {exc}")
            if name == "msvcp140.dll":
                return False
    return True


# ── Win32 ───────────────────────────────────────────────────────────────────

# Private WinDLL instances, so the argtypes set here can't change anyone
# else's view of these functions.

@functools.cache
def _kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetModuleFileNameW.restype = wintypes.DWORD
    kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    return kernel32


@functools.cache
def _version_dll():
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL
    return version


def _file_version(path: str) -> tuple[int, int, int, int] | None:
    """The file version Windows reports for ``path``, or ``None``."""
    from ctypes import wintypes

    version = _version_dll()
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    fixed = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data, "\\", ctypes.byref(fixed), ctypes.byref(length)):
        return None
    if not fixed.value or length.value < 16:
        return None
    # VS_FIXEDFILEINFO opens with dwSignature, dwStrucVersion,
    # dwFileVersionMS, dwFileVersionLS.
    _signature, _struct_version, ms, ls = (wintypes.DWORD * 4).from_address(fixed.value)
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
