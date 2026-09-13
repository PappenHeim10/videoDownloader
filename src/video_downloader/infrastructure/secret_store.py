"""Encrypting a few bytes at rest, bound to this user on this machine.

Windows DPAPI, through `ctypes` rather than a package: `CryptProtectData` is
two calls in `crypt32`, it is present on every supported Windows, and the
alternative was a dependency for something the platform already does.

What it buys, precisely, and what it does not:

* A blob protected here cannot be read by another Windows account on the same
  machine, nor by the same account on another machine. Copying `sessions.dat`
  to a colleague's PC yields nothing.
* It is *not* protection from code running as this user. Anything that can read
  the file can also call `CryptUnprotectData`. The threat this addresses is a
  file that travels - a backup, a synced profile directory, a support archive -
  which is exactly how a plaintext session token gets away from its owner.

`CRYPTPROTECT_UI_FORBIDDEN` is set so neither call can ever try to show a
window: these run on whatever thread asked, including one with no event loop,
and a blocking prompt there would hang the application rather than fail.

Off Windows there is no equivalent that is honest to write, so `available()`
answers False and `protect` refuses. The caller's job is to keep the secret in
memory for the run instead of writing a plaintext file that looks protected.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

#: Mixed into the protection so a blob can only be read back by this
#: application's own call. Not a secret - it ships in the binary - and not
#: meant to be one: it binds the blob to a purpose, while the user and machine
#: binding is what DPAPI itself provides.
_ENTROPY = b"VideoDownloader/site-session/v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretStoreError(RuntimeError):
    """Protection or unprotection failed, with the platform's own error code."""


class SecretStoreUnavailable(SecretStoreError):
    """This platform has no user-bound protection this module can use.

    Its own type because the answer differs: a failed call is a problem to
    report, and a platform without DPAPI is a decision to make - keep the
    secret in memory, or ask again next time.
    """


class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    )


def available() -> bool:
    """Whether bytes can be protected at rest on this platform."""
    return sys.platform == "win32"


def _blob_of(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    # The buffer is returned alongside the struct because the struct only points
    # at it: letting it be collected would hand the API a dangling pointer.
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt32() -> ctypes.WinDLL:
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    for name in ("CryptProtectData", "CryptUnprotectData"):
        function = getattr(library, name)
        function.restype = wintypes.BOOL
        function.argtypes = (
            ctypes.POINTER(_DataBlob),   # pDataIn / pDataOut source
            wintypes.LPCWSTR,            # szDataDescr
            ctypes.POINTER(_DataBlob),   # pOptionalEntropy
            ctypes.c_void_p,             # pvReserved
            ctypes.c_void_p,             # pPromptStruct
            wintypes.DWORD,              # dwFlags
            ctypes.POINTER(_DataBlob),   # pDataOut
        )
    return library


def _call(function_name: str, data: bytes) -> bytes:
    if not available():
        raise SecretStoreUnavailable(
            f"{sys.platform} has no user-bound secret store this application uses"
        )

    library = _crypt32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)

    source, _source_buffer = _blob_of(data)
    entropy, _entropy_buffer = _blob_of(_ENTROPY)
    result = _DataBlob()

    ok = getattr(library, function_name)(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result),
    )
    if not ok:
        code = ctypes.get_last_error()
        raise SecretStoreError(f"{function_name} failed with Windows error {code}")

    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        # The API allocated it; nothing else will hand it back.
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    """`data`, readable again only by this user on this machine."""
    return _call("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    """The bytes `protect` was given, or an error - never a guess."""
    return _call("CryptUnprotectData", blob)
