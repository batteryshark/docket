"""Small native foundations; no service management or host discovery.

Private Docket storage enforces owner-only access, reparse/symlink refusal,
single-link regular file checks, and handle-verified operations on both POSIX
and native Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import ntpath
import os
import secrets
import stat
import subprocess
from pathlib import Path


class PrivateStorageError(OSError):
    """Private storage ownership, ACL, link-count, type, or size refusal."""


def is_windows() -> bool:
    return os.name == "nt"


def state_root() -> Path:
    override = os.environ.get("DOCKET_HOME")
    if override:
        return Path(override).expanduser()
    if is_windows():
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not fully_qualified_path(local_app_data, windows=True):
            raise PrivateStorageError(
                "Windows Docket state requires LOCALAPPDATA or DOCKET_HOME to be a fully qualified path"
            )
        return Path(local_app_data) / "Docket"
    return Path("~/.docket").expanduser()


def fully_qualified_path(value: str, *, windows: bool | None = None) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if not (is_windows() if windows is None else windows):
        return value.startswith("/")
    # Device namespaces are not ordinary filesystem watch paths. A drive is
    # required even for rooted paths; C:foo and \foo depend on ambient state.
    path = value.replace("/", "\\")
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        return False
    drive, tail = ntpath.splitdrive(path)
    if drive.startswith("\\\\"):
        parts = drive[2:].split("\\")
        return len(parts) == 2 and all(p and p not in (".", "..") for p in parts) and (not tail or tail.startswith("\\"))
    return len(drive) == 2 and drive[0].isascii() and drive[0].isalpha() and drive[1] == ":" and tail.startswith("\\")


def same_directory(expected: str, actual: str) -> bool:
    if not fully_qualified_path(expected) or not fully_qualified_path(actual):
        return False
    try:
        # File identity respects per-directory case sensitivity on Windows;
        # normcase would incorrectly equate different case-sensitive objects.
        return os.path.samefile(expected, actual)
    except FileNotFoundError:
        missing = 0
        for path in (expected, actual):
            try:
                os.stat(path)
            except FileNotFoundError:
                missing += 1
        if missing != 2:
            return False
        return os.path.realpath(expected) == os.path.realpath(actual)


def child_options() -> dict:
    """Fixed-command children only: no inherited input, console, or handles."""
    options = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if is_windows():
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


_SYSTEM_SID = "S-1-5-18"
_TOKEN_QUERY = 0x0008
_TOKEN_ADJUST_DEFAULT = 0x0080
_TOKEN_USER_CLASS = 1
_TOKEN_OWNER_CLASS = 4
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PRESENT = 0x0004
_SE_DACL_PROTECTED = 0x1000
_SDDL_REVISION_1 = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERIT_ONLY_ACE = 0x08
_INHERITED_ACE = 0x10
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_GENERIC_ALL = 0x10000000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_SYNCHRONIZE = 0x00100000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_SHARE_ALL = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_TYPE_DISK = 0x0001
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _TOKEN_OWNER(ctypes.Structure):
    _fields_ = [("Owner", ctypes.c_void_p)]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_ulong),
        ("AclBytesInUse", ctypes.c_ulong),
        ("AclBytesFree", ctypes.c_ulong),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", _ACE_HEADER),
        ("Mask", ctypes.c_ulong),
        ("SidStart", ctypes.c_ulong),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_ulong),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_ulong),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_ulong),
        ("nFileSizeHigh", ctypes.c_ulong),
        ("nFileSizeLow", ctypes.c_ulong),
        ("nNumberOfLinks", ctypes.c_ulong),
        ("nFileIndexHigh", ctypes.c_ulong),
        ("nFileIndexLow", ctypes.c_ulong),
    ]


_WIN_APIS = None


def _windows_apis():
    global _WIN_APIS
    if _WIN_APIS is not None:
        return _WIN_APIS
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise PrivateStorageError(
            "Windows private Docket storage unavailable: native user SID, ACL and reparse/handle verification requires a native Windows runtime"
        )
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SECURITY_ATTRIBUTES)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetLongPathNameW.restype = wintypes.DWORD
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel.FlushFileBuffers.restype = wintypes.BOOL

    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.SetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    advapi.SetTokenInformation.restype = wintypes.BOOL
    advapi.IsValidSid.argtypes = [wintypes.LPVOID]
    advapi.IsValidSid.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    advapi.SetSecurityInfo.restype = wintypes.DWORD

    _WIN_APIS = (kernel, advapi)
    return _WIN_APIS


@contextlib.contextmanager
def _win_handle(handle: int):
    kernel, _ = _windows_apis()
    try:
        yield handle
    finally:
        if handle and handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)


def _sid_to_string(sid_ptr: int) -> str:
    from ctypes import wintypes

    kernel, advapi = _windows_apis()
    if not sid_ptr or not advapi.IsValidSid(sid_ptr):
        raise PrivateStorageError("Windows SID is invalid")
    out = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
        raise PrivateStorageError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
    try:
        value = out.value
    finally:
        kernel.LocalFree(out)
    if not value:
        raise PrivateStorageError("Windows SID string is empty")
    return value


def _windows_user_sid() -> tuple[str, ctypes.Array, int]:
    from ctypes import wintypes

    kernel, advapi = _windows_apis()
    tok = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(), _TOKEN_QUERY | _TOKEN_ADJUST_DEFAULT, ctypes.byref(tok)
    ):
        raise PrivateStorageError(
            ctypes.get_last_error(),
            "Windows private Docket storage unavailable: unable to query current user SID, ACL token",
        )
    with _win_handle(tok.value):
        needed = wintypes.DWORD(0)
        advapi.GetTokenInformation(tok, _TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise PrivateStorageError("Windows private Docket storage unavailable: TokenUser size unavailable")
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
            tok, _TOKEN_USER_CLASS, buf, needed.value, ctypes.byref(needed)
        ):
            raise PrivateStorageError(
                ctypes.get_last_error(),
                "Windows private Docket storage unavailable: TokenUser query failed",
            )
        sid_ptr = _SID_AND_ATTRIBUTES.from_buffer(buf).Sid
        sid_str = _sid_to_string(sid_ptr)
        if sid_str.startswith("S-1-5-32-") or sid_str in ("S-1-1-0", _SYSTEM_SID):
            raise PrivateStorageError(
                f"Windows private Docket storage unavailable: refuses non-user SID {sid_str}"
            )
        # Ensure new objects created in-process (including SQLite sidecars) default to TokenUser
        # rather than BUILTIN\Administrators when the user belongs to the Administrators group.
        owner = _TOKEN_OWNER(Owner=sid_ptr)
        if not advapi.SetTokenInformation(
            tok, _TOKEN_OWNER_CLASS, ctypes.byref(owner), ctypes.sizeof(owner)
        ):
            raise PrivateStorageError(
                ctypes.get_last_error(),
                "Windows private Docket storage unavailable: unable to pin process TokenOwner to user SID",
            )
        return sid_str, buf, sid_ptr


@contextlib.contextmanager
def _windows_private_sd(user_sid_str: str, *, is_directory: bool):
    from ctypes import wintypes

    kernel, advapi = _windows_apis()
    if is_directory:
        sddl = f"O:{user_sid_str}D:P(A;OICI;FA;;;{user_sid_str})(A;OICI;FA;;;SY)"
    else:
        sddl = f"O:{user_sid_str}D:P(A;;FA;;;{user_sid_str})(A;;FA;;;SY)"
    sd_ptr = wintypes.LPVOID()
    sd_size = wintypes.ULONG(0)
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(sd_ptr), ctypes.byref(sd_size)
    ):
        raise PrivateStorageError(
            ctypes.get_last_error(), "unable to construct Windows private security descriptor"
        )
    try:
        yield sd_ptr.value
    finally:
        if sd_ptr.value:
            kernel.LocalFree(sd_ptr.value)


def _windows_apply_private_security(handle: int, user_sid_ptr: int, sd_ptr: int) -> None:
    from ctypes import wintypes

    _, advapi = _windows_apis()
    present = wintypes.BOOL(0)
    dacl_ptr = wintypes.LPVOID()
    defaulted = wintypes.BOOL(0)
    if not advapi.GetSecurityDescriptorDacl(
        sd_ptr, ctypes.byref(present), ctypes.byref(dacl_ptr), ctypes.byref(defaulted)
    ) or not present.value or not dacl_ptr.value:
        raise PrivateStorageError("Windows private security descriptor is missing its DACL")
    rc = advapi.SetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
        user_sid_ptr,
        None,
        dacl_ptr.value,
        None,
    )
    if rc != 0:
        raise PrivateStorageError(rc, "unable to apply Windows private owner and protected DACL")


def _windows_verify_security(
    handle: int,
    *,
    user_sid_str: str,
    user_sid_ptr: int,
    is_directory: bool,
    allow_inherited_from_private_parent: bool = False,
) -> bool:
    """Verify owner SID and DACL on an open handle. Returns True if DACL is already protected."""
    from ctypes import wintypes

    kernel, advapi = _windows_apis()
    owner_sid = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    sd = wintypes.LPVOID()
    rc = advapi.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner_sid),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(sd),
    )
    if rc != 0:
        raise PrivateStorageError(rc, "Windows security descriptor unavailable")
    try:
        owner_str = _sid_to_string(owner_sid.value)
        if owner_str != user_sid_str or not advapi.EqualSid(owner_sid.value, user_sid_ptr):
            raise PrivateStorageError(
                f"Docket private path owner {owner_str} does not match current user SID"
            )
        control = wintypes.WORD(0)
        revision = wintypes.DWORD(0)
        if not advapi.GetSecurityDescriptorControl(
            sd.value, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise PrivateStorageError(ctypes.get_last_error(), "GetSecurityDescriptorControl failed")
        if not (control.value & _SE_DACL_PRESENT) or not dacl.value:
            raise PrivateStorageError("Docket private path is missing a DACL")
        is_protected = bool(control.value & _SE_DACL_PROTECTED)
        if not is_protected and not allow_inherited_from_private_parent:
            raise PrivateStorageError("Docket private path DACL must be protected against inheritance")

        size_info = _ACL_SIZE_INFORMATION()
        if not advapi.GetAclInformation(
            dacl.value,
            ctypes.byref(size_info),
            ctypes.sizeof(size_info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise PrivateStorageError(ctypes.get_last_error(), "GetAclInformation failed")
        if size_info.AceCount == 0:
            raise PrivateStorageError("Docket private path DACL has no ACEs")

        allowed_flags = (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE) if is_directory else 0
        if allow_inherited_from_private_parent:
            allowed_flags |= _INHERITED_ACE

        user_full_access = False
        has_inherited_ace = False
        all_aces_inherited = True
        for idx in range(size_info.AceCount):
            ace_ptr = wintypes.LPVOID()
            if not advapi.GetAce(dacl.value, idx, ctypes.byref(ace_ptr)) or not ace_ptr.value:
                raise PrivateStorageError(ctypes.get_last_error(), "GetAce failed")
            header = _ACE_HEADER.from_address(ace_ptr.value)
            if header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                raise PrivateStorageError("Docket private path DACL contains a non-allow ACE")
            if header.AceFlags & _INHERITED_ACE:
                has_inherited_ace = True
                if not allow_inherited_from_private_parent:
                    raise PrivateStorageError("Docket private path DACL contains an inherited ACE")
            else:
                all_aces_inherited = False
            if header.AceFlags & _INHERIT_ONLY_ACE:
                raise PrivateStorageError("Docket private path DACL contains an inherit-only ACE")
            if header.AceFlags & ~allowed_flags:
                raise PrivateStorageError("Docket private path DACL contains unexpected ACE flags")
            ace = _ACCESS_ALLOWED_ACE.from_address(ace_ptr.value)
            sid_addr = ace_ptr.value + _ACCESS_ALLOWED_ACE.SidStart.offset
            trustee = _sid_to_string(sid_addr)
            if trustee not in (user_sid_str, _SYSTEM_SID):
                raise PrivateStorageError(
                    f"Docket private path DACL grants access to unexpected trustee {trustee}"
                )
            if trustee == user_sid_str:
                if (ace.Mask & _FILE_ALL_ACCESS) == _FILE_ALL_ACCESS or (
                    ace.Mask & _GENERIC_ALL
                ) == _GENERIC_ALL:
                    user_full_access = True
                else:
                    raise PrivateStorageError(
                        "Docket private path DACL gives partial access to current user"
                    )
        if not user_full_access:
            raise PrivateStorageError(
                "Docket private path DACL does not grant full access to current user"
            )
        if allow_inherited_from_private_parent and (not is_protected or has_inherited_ace):
            # Only SQLite's private inheritance case is recoverable, not arbitrary
            # explicit/unprotected ACLs. The caller also verifies the parent,
            # owner, regular-file type and single-link identity before sealing.
            if is_protected or not all_aces_inherited:
                raise PrivateStorageError("Docket sidecar must inherit only from its private parent")
        return is_protected and not has_inherited_ace
    finally:
        if sd.value:
            kernel.LocalFree(sd.value)


def _strip_extended_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _windows_final_path(handle: int) -> str:
    kernel, _ = _windows_apis()
    needed = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not needed:
        raise PrivateStorageError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
    buf = ctypes.create_unicode_buffer(needed + 1)
    written = kernel.GetFinalPathNameByHandleW(handle, buf, needed + 1, 0)
    if not written or written > needed:
        raise PrivateStorageError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
    return _strip_extended_prefix(buf.value)


def _windows_long_path(path: str) -> str:
    kernel, _ = _windows_apis()
    abspath = ntpath.abspath(path)
    needed = kernel.GetLongPathNameW(abspath, None, 0)
    if not needed:
        return abspath
    buf = ctypes.create_unicode_buffer(needed + 1)
    written = kernel.GetLongPathNameW(abspath, buf, needed + 1)
    if not written or written > needed:
        return abspath
    return buf.value


def _windows_raw_open(
    path: str,
    access: int,
    creation: int,
    flags: int,
    sa_ptr=None,
) -> int:
    kernel, _ = _windows_apis()
    invalid = ctypes.c_void_p(-1).value
    handle = kernel.CreateFileW(path, access, _FILE_SHARE_ALL, sa_ptr, creation, flags, None)
    if not handle or handle == invalid:
        err = ctypes.get_last_error()
        if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            raise FileNotFoundError(errno.ENOENT, "path does not exist", path)
        if err in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
            raise FileExistsError(errno.EEXIST, "path already exists", path)
        if err == _ERROR_ACCESS_DENIED and creation == _CREATE_NEW and os.path.lexists(path):
            raise FileExistsError(errno.EEXIST, "path already exists", path)
        raise PrivateStorageError(err, f"Windows CreateFileW failed ({err})", path)
    return handle


def _windows_handle_identity(handle: int) -> tuple[_BY_HANDLE_FILE_INFORMATION, tuple[int, int, int]]:
    kernel, _ = _windows_apis()
    if kernel.GetFileType(handle) != _FILE_TYPE_DISK:
        raise PrivateStorageError("Docket path must be a disk file or directory")
    info = _BY_HANDLE_FILE_INFORMATION()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise PrivateStorageError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
    if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(errno.ELOOP, "Docket path must not be a symlink or reparse point")
    return info, (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow)


def _windows_verify_handle(
    handle: int,
    *,
    user_sid_str: str,
    user_sid_ptr: int,
    is_directory: bool,
    parent_identity: tuple[int, int, int] | None = None,
    max_bytes: int | None = None,
    allow_inherited_from_private_parent: bool = False,
) -> tuple[tuple[int, int, int], str, int, bool]:
    info, identity = _windows_handle_identity(handle)
    if is_directory:
        if not (info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY):
            raise PrivateStorageError(errno.ENOTDIR, "Docket directory path is not a directory")
        size = 0
    else:
        if info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise PrivateStorageError(errno.EISDIR, "Docket file must be a regular file, not a directory")
        if info.nNumberOfLinks != 1:
            raise PrivateStorageError("Docket file must be a single-link regular file")
        size = (info.nFileSizeHigh << 32) | info.nFileSizeLow
        if max_bytes is not None and size > max_bytes:
            raise PrivateStorageError("Docket private file exceeds maximum size")
    final_path = _windows_final_path(handle)
    if parent_identity is not None:
        parent_path = ntpath.dirname(final_path)
        parent_h = _windows_raw_open(
            parent_path,
            _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        with _win_handle(parent_h):
            _, actual_parent_id = _windows_handle_identity(parent_h)
            if actual_parent_id != parent_identity:
                raise PrivateStorageError("Docket file parent directory identity mismatch")
    is_protected = _windows_verify_security(
        handle,
        user_sid_str=user_sid_str,
        user_sid_ptr=user_sid_ptr,
        is_directory=is_directory,
        allow_inherited_from_private_parent=allow_inherited_from_private_parent,
    )
    return identity, final_path, size, is_protected


def _check_dedicated_directory_target(path: Path) -> None:
    raw = str(path)
    if not fully_qualified_path(raw):
        raise PrivateStorageError("Docket state directory must be a fully qualified path")
    if path.parent == path:
        raise PrivateStorageError("Docket state must be a dedicated directory, not a root directory")
    forbidden = [Path.home()]
    if is_windows():
        for env_key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
            val = os.environ.get(env_key)
            if val:
                forbidden.append(Path(val))
    norm_target = os.path.normcase(os.path.abspath(raw))
    for item in forbidden:
        try:
            if norm_target == os.path.normcase(os.path.abspath(str(item))):
                raise PrivateStorageError(
                    "Docket state must be a dedicated directory, not the user home or profile root"
                )
            if path.exists() and item.exists() and os.path.samefile(path, item):
                raise PrivateStorageError(
                    "Docket state must be a dedicated directory, not the user home or profile root"
                )
        except PrivateStorageError:
            raise
        except OSError:
            continue


def _check_no_windows_reparse_ancestors(path: Path) -> None:
    cur = Path(ntpath.abspath(str(path)))
    while True:
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            pass
        else:
            attrs = getattr(st, "st_file_attributes", 0)
            if stat.S_ISLNK(st.st_mode) or (attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
                raise OSError(errno.ELOOP, "Docket path must not be or traverse a symlink or reparse point")
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


class _VerifiedWindowsDirectory:
    def __init__(
        self,
        handle: int,
        identity: tuple[int, int, int],
        final_path: str,
        user_sid_str: str,
        user_sid_buf: ctypes.Array,
        user_sid_ptr: int,
    ):
        self.handle = handle
        self.identity = identity
        self.final_path = final_path
        self.user_sid_str = user_sid_str
        self.user_sid_buf = user_sid_buf
        self.user_sid_ptr = user_sid_ptr

    def child_path(self, name: str) -> str:
        if not name or "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
            raise PrivateStorageError("invalid Docket private file name")
        return ntpath.join(self.final_path, name)


@contextlib.contextmanager
def _windows_verified_directory(path: Path):
    user_sid_str, user_sid_buf, user_sid_ptr = _windows_user_sid()
    _check_dedicated_directory_target(path)
    _check_no_windows_reparse_ancestors(path)
    kernel, _ = _windows_apis()

    missing: list[Path] = []
    cur = Path(ntpath.abspath(str(path)))
    while not os.path.lexists(cur):
        missing.append(cur)
        parent = cur.parent
        if parent == cur:
            raise PrivateStorageError("Docket private directory root does not exist")
        cur = parent

    if missing:
        with _windows_private_sd(user_sid_str, is_directory=True) as sd_ptr:
            sa = _SECURITY_ATTRIBUTES(
                nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
                lpSecurityDescriptor=sd_ptr,
                bInheritHandle=0,
            )
            for target in reversed(missing):
                created = bool(kernel.CreateDirectoryW(str(target), ctypes.byref(sa)))
                if not created:
                    err = ctypes.get_last_error()
                    if err != _ERROR_ALREADY_EXISTS:
                        raise PrivateStorageError(
                            err, "unable to create private Docket directory", str(target)
                        )
                if created:
                    h = _windows_raw_open(
                        str(target),
                        _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                        _OPEN_EXISTING,
                        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                    )
                    with _win_handle(h):
                        _windows_apply_private_security(h, user_sid_ptr, sd_ptr)
                        _windows_verify_handle(
                            h,
                            user_sid_str=user_sid_str,
                            user_sid_ptr=user_sid_ptr,
                            is_directory=True,
                        )

    dir_handle = _windows_raw_open(
        str(path),
        _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    with _win_handle(dir_handle):
        identity, final_path, _, _ = _windows_verify_handle(
            dir_handle,
            user_sid_str=user_sid_str,
            user_sid_ptr=user_sid_ptr,
            is_directory=True,
        )
        expected_long = _windows_long_path(str(path))
        if ntpath.normcase(ntpath.normpath(final_path)) != ntpath.normcase(
            ntpath.normpath(expected_long)
        ):
            raise OSError(errno.ELOOP, "Docket directory path traversed a reparse point")
        yield _VerifiedWindowsDirectory(
            dir_handle, identity, final_path, user_sid_str, user_sid_buf, user_sid_ptr
        )


def _windows_write_handle(handle: int, data: bytes) -> None:
    from ctypes import wintypes

    kernel, _ = _windows_apis()
    view = memoryview(data)
    total = 0
    while total < len(view):
        chunk = bytes(view[total : total + 65536])
        written = wintypes.DWORD(0)
        if not kernel.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if written.value == 0:
            raise OSError("WriteFile wrote 0 bytes")
        total += written.value
    if not kernel.FlushFileBuffers(handle):
        raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")


def _windows_read_handle(handle: int, max_bytes: int) -> bytes:
    from ctypes import wintypes

    kernel, _ = _windows_apis()
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        step = min(remaining, 65536)
        buf = ctypes.create_string_buffer(step)
        read_count = wintypes.DWORD(0)
        if not kernel.ReadFile(handle, buf, step, ctypes.byref(read_count), None):
            raise OSError(ctypes.get_last_error(), "ReadFile failed")
        if read_count.value == 0:
            break
        chunks.append(buf.raw[: read_count.value])
        remaining -= read_count.value
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise PrivateStorageError("Docket private file exceeds maximum size")
    return raw


@contextlib.contextmanager
def _posix_verified_directory(path: Path):
    private_directory(path)
    dir_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0))
    try:
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise PrivateStorageError("Docket directory must be user-owned, mode 0700, and not a symlink")
        yield dir_fd, (st.st_dev, st.st_ino)
    finally:
        os.close(dir_fd)


def require_private_storage() -> None:
    if is_windows():
        _windows_user_sid()


def private_directory(path: Path) -> None:
    require_private_storage()  # Before mkdir, reads, or secret creation.
    if is_windows():
        with _windows_verified_directory(path):
            return
    _check_dedicated_directory_target(path)
    if path.is_symlink():
        raise OSError(errno.ELOOP, "Docket directory must not be a symlink")
    # Never chmod an existing directory (possibly unrelated user data), and ensure
    # every newly created directory component is created with mode 0700.
    missing: list[Path] = []
    cur = path
    while not os.path.lexists(cur):
        missing.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    for target in reversed(missing):
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise PrivateStorageError("Docket directory must be user-owned, mode 0700, and not a symlink")


def seal_file(path: Path) -> None:
    require_private_storage()
    if is_windows():
        with _windows_verified_directory(path.parent) as vdir:
            target = vdir.child_path(path.name)
            handle = _windows_raw_open(
                target,
                _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            )
            with _win_handle(handle):
                _windows_verify_handle(
                    handle,
                    user_sid_str=vdir.user_sid_str,
                    user_sid_ptr=vdir.user_sid_ptr,
                    is_directory=False,
                    parent_identity=vdir.identity,
                )
        return
    with _posix_verified_directory(path.parent) as (_dir_fd, dir_id):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
                raise PrivateStorageError("Docket file must be a user-owned, single-link regular file")
            parent_st = path.parent.lstat()
            if (parent_st.st_dev, parent_st.st_ino) != dir_id:
                raise PrivateStorageError("Docket file parent directory identity changed")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def create_private_file(path: Path, data: bytes = b"") -> None:
    require_private_storage()
    raw = bytes(data)
    if is_windows():
        with _windows_verified_directory(path.parent) as vdir:
            target = vdir.child_path(path.name)
            with _windows_private_sd(vdir.user_sid_str, is_directory=False) as sd_ptr:
                sa = _SECURITY_ATTRIBUTES(
                    nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
                    lpSecurityDescriptor=sd_ptr,
                    bInheritHandle=0,
                )
                handle = _windows_raw_open(
                    target,
                    _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _SYNCHRONIZE,
                    _CREATE_NEW,
                    _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                    ctypes.byref(sa),
                )
                with _win_handle(handle):
                    _windows_apply_private_security(handle, vdir.user_sid_ptr, sd_ptr)
                    _windows_verify_handle(
                        handle,
                        user_sid_str=vdir.user_sid_str,
                        user_sid_ptr=vdir.user_sid_ptr,
                        is_directory=False,
                        parent_identity=vdir.identity,
                    )
                    if raw:
                        _windows_write_handle(handle, raw)
                    else:
                        kernel, _ = _windows_apis()
                        if not kernel.FlushFileBuffers(handle):
                            raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")
        return
    with _posix_verified_directory(path.parent) as (_dir_fd, dir_id):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
                raise PrivateStorageError("Docket file must be a user-owned, single-link regular file")
            parent_st = path.parent.lstat()
            if (parent_st.st_dev, parent_st.st_ino) != dir_id:
                raise PrivateStorageError("Docket file parent directory identity changed")
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            total = 0
            while total < len(view):
                written = os.write(fd, view[total:])
                if written <= 0:
                    raise OSError("failed to write private Docket file")
                total += written
            os.fsync(fd)
        finally:
            os.close(fd)


def read_private_bytes(path: Path, *, max_bytes: int = 65536) -> bytes:
    require_private_storage()
    if is_windows():
        with _windows_verified_directory(path.parent) as vdir:
            target = vdir.child_path(path.name)
            handle = _windows_raw_open(
                target,
                _GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            )
            with _win_handle(handle):
                _windows_verify_handle(
                    handle,
                    user_sid_str=vdir.user_sid_str,
                    user_sid_ptr=vdir.user_sid_ptr,
                    is_directory=False,
                    parent_identity=vdir.identity,
                    max_bytes=max_bytes,
                )
                return _windows_read_handle(handle, max_bytes)
    with _posix_verified_directory(path.parent) as (_dir_fd, dir_id):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_nlink != 1
                or st.st_size > max_bytes
            ):
                raise PrivateStorageError(
                    "Docket file must be a private owner-only regular file (0600)"
                )
            parent_st = path.parent.lstat()
            if (parent_st.st_dev, parent_st.st_ino) != dir_id:
                raise PrivateStorageError("Docket file parent directory identity changed")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > max_bytes:
                raise PrivateStorageError("Docket private file exceeds maximum size")
            return raw
        finally:
            os.close(fd)


def replace_private_bytes(
    path: Path, data: bytes, *, max_bytes: int = 65536, prefix: str | None = None
) -> None:
    require_private_storage()
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise PrivateStorageError("Docket private file exceeds maximum size")
    temp_name = f"{prefix or f'.{path.stem}-'}{secrets.token_hex(8)}"
    temporary = path.parent / temp_name
    if is_windows():
        with _windows_verified_directory(path.parent) as vdir:
            target = vdir.child_path(path.name)
            try:
                existing = _windows_raw_open(
                    target,
                    _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    _OPEN_EXISTING,
                    _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                )
            except FileNotFoundError:
                pass
            else:
                with _win_handle(existing):
                    _windows_verify_handle(
                        existing,
                        user_sid_str=vdir.user_sid_str,
                        user_sid_ptr=vdir.user_sid_ptr,
                        is_directory=False,
                        parent_identity=vdir.identity,
                        max_bytes=max_bytes,
                    )
            temp_target = vdir.child_path(temp_name)
            with _windows_private_sd(vdir.user_sid_str, is_directory=False) as sd_ptr:
                sa = _SECURITY_ATTRIBUTES(
                    nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
                    lpSecurityDescriptor=sd_ptr,
                    bInheritHandle=0,
                )
                temp_handle = _windows_raw_open(
                    temp_target,
                    _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _SYNCHRONIZE,
                    _CREATE_NEW,
                    _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                    ctypes.byref(sa),
                )
                with _win_handle(temp_handle):
                    _windows_apply_private_security(temp_handle, vdir.user_sid_ptr, sd_ptr)
                    _windows_verify_handle(
                        temp_handle,
                        user_sid_str=vdir.user_sid_str,
                        user_sid_ptr=vdir.user_sid_ptr,
                        is_directory=False,
                        parent_identity=vdir.identity,
                        max_bytes=max_bytes,
                    )
                    _windows_write_handle(temp_handle, raw)
            try:
                os.replace(temporary, path)
                replaced = _windows_raw_open(
                    target,
                    _GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    _OPEN_EXISTING,
                    _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                )
                with _win_handle(replaced):
                    _windows_verify_handle(
                        replaced,
                        user_sid_str=vdir.user_sid_str,
                        user_sid_ptr=vdir.user_sid_ptr,
                        is_directory=False,
                        parent_identity=vdir.identity,
                        max_bytes=max_bytes,
                    )
            finally:
                if os.path.lexists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
        return
    with _posix_verified_directory(path.parent) as (dir_fd, _dir_id):
        if os.path.lexists(path):
            read_private_bytes(path, max_bytes=max_bytes)
        create_private_file(temporary, raw)
        try:
            os.replace(temporary, path)
            os.fsync(dir_fd)
            seal_file(path)
        finally:
            if os.path.lexists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def prepare_private_db(path: Path, *, seal_sidecars: bool = False) -> None:
    """Verify DB and recover only verified private-inherited SQLite sidecars.

    Recovery is required before SQLite opens, as well as before commit. The
    seal_sidecars keyword is retained for callers; neither path relaxes DB ACLs.
    """
    require_private_storage()
    if is_windows():
        with _windows_verified_directory(path.parent) as vdir:
            db_target = vdir.child_path(path.name)
            try:
                with _windows_private_sd(vdir.user_sid_str, is_directory=False) as sd_ptr:
                    sa = _SECURITY_ATTRIBUTES(
                        nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
                        lpSecurityDescriptor=sd_ptr,
                        bInheritHandle=0,
                    )
                    db_handle = _windows_raw_open(
                        db_target,
                        _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _SYNCHRONIZE,
                        _CREATE_NEW,
                        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                        ctypes.byref(sa),
                    )
                    with _win_handle(db_handle):
                        _windows_apply_private_security(db_handle, vdir.user_sid_ptr, sd_ptr)
                        _windows_verify_handle(
                            db_handle,
                            user_sid_str=vdir.user_sid_str,
                            user_sid_ptr=vdir.user_sid_ptr,
                            is_directory=False,
                            parent_identity=vdir.identity,
                        )
            except FileExistsError:
                db_handle = _windows_raw_open(
                    db_target,
                    _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    _OPEN_EXISTING,
                    _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                )
                with _win_handle(db_handle):
                    _windows_verify_handle(
                        db_handle,
                        user_sid_str=vdir.user_sid_str,
                        user_sid_ptr=vdir.user_sid_ptr,
                        is_directory=False,
                        parent_identity=vdir.identity,
                    )
            for suffix in ("-wal", "-shm"):
                sidecar_name = path.name + suffix
                sidecar_target = vdir.child_path(sidecar_name)
                access = _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE | _WRITE_DAC | _WRITE_OWNER
                try:
                    sh = _windows_raw_open(
                        sidecar_target,
                        access,
                        _OPEN_EXISTING,
                        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                    )
                except FileNotFoundError:
                    continue
                with _win_handle(sh):
                    _, _, _, is_protected = _windows_verify_handle(
                        sh,
                        user_sid_str=vdir.user_sid_str,
                        user_sid_ptr=vdir.user_sid_ptr,
                        is_directory=False,
                        parent_identity=vdir.identity,
                        allow_inherited_from_private_parent=True,
                    )
                    if not is_protected:
                        with _windows_private_sd(vdir.user_sid_str, is_directory=False) as sd_ptr:
                            _windows_apply_private_security(sh, vdir.user_sid_ptr, sd_ptr)
                        _windows_verify_handle(
                            sh,
                            user_sid_str=vdir.user_sid_str,
                            user_sid_ptr=vdir.user_sid_ptr,
                            is_directory=False,
                            parent_identity=vdir.identity,
                            allow_inherited_from_private_parent=False,
                        )
        return
    with _posix_verified_directory(path.parent):
        try:
            create_private_file(path, b"")
        except FileExistsError:
            seal_file(path)
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if os.path.lexists(candidate):
                seal_file(candidate)


def windows_process_start(pid: int) -> str | None:
    """PID + 100ns creation FILETIME; only definite absence returns None."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Query-limited + synchronize. Access denied is unavailable, never exited.
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such PID (PID 0 excluded)
            return None
        raise OSError(error, "process identity unavailable")
    try:
        creation, exit_time, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, creation, exit_time, system, user):
            raise OSError(ctypes.get_last_error(), "process times unavailable")
        state = kernel.WaitForSingleObject(handle, 0)  # Never wait for exit.
        if state == 0:
            return None
        if state != 258:  # WAIT_TIMEOUT means still running.
            raise OSError("process state unavailable")
        stamp = creation.dwLowDateTime | (creation.dwHighDateTime << 32)
        if not stamp:
            raise OSError("process creation time unavailable")
        return f"win:{pid}:{stamp}"
    finally:
        kernel.CloseHandle(handle)
