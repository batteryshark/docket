"""Current-user interactive logon task; fixed in-process daemon launcher.

No native operations occur at import. See notes/windows-service.md before use.
"""
from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import runpy
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

from docket import platform as native

MANIFEST = "windows-service.json"
LIMIT = 65536
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
OPERATION_SECONDS = 30

# Microsoft Task Scheduler XSD (xs:all) and Security Contexts for Tasks (LUA
# default); references and conservative normalization scope in windows-service.md.
# These are normalization rules, NOT a field whitelist: all other XML survives.
XML_DEFAULTS = {
    "Task/Principals/Principal/RunLevel": "LeastPrivilege",
    **{"Task/Settings/" + key: value for key, value in (
        ("AllowHardTerminate", "true"), ("StartWhenAvailable", "false"),
        ("RunOnlyIfNetworkAvailable", "false"), ("AllowStartOnDemand", "true"),
        ("Enabled", "true"), ("RunOnlyIfIdle", "false"), ("WakeToRun", "false"),
        ("Priority", "7"), ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "true"), ("StopIfGoingOnBatteries", "true"),
        ("Hidden", "false"), ("DeleteExpiredTaskAfter", "PT0S"),
        ("ExecutionTimeLimit", "PT72H"), ("UseUnifiedSchedulingEngine", "false"),
        ("DisallowStartOnRemoteAppSession", "false"))},
    **{"Task/Settings/IdleSettings/" + key: value for key, value in (
        ("Duration", "PT10M"), ("WaitTimeout", "PT1H"),
        ("StopOnIdleEnd", "true"), ("RestartOnIdle", "false"))},
    **{"Task/Triggers/LogonTrigger/" + key: value for key, value in (
        ("Enabled", "true"), ("ExecutionTimeLimit", "PT72H"), ("Delay", "PT0M"))},
}
XML_ALL = ("Task", "Task/RegistrationInfo", "Task/Settings",
           "Task/Principals/Principal", "Task/Settings/IdleSettings",
           "Task/Settings/RestartOnFailure", "Task/Actions/Exec")
XML_ELEMENT_ONLY = XML_ALL + ("Task/Principals", "Task/Triggers", "Task/Actions",
                              "Task/Triggers/LogonTrigger")


def _ps_rules(values) -> str:
    # Only module constants enter script syntax; never request/path/user input.
    return ",".join("'" + value + "'" for value in values)


class ServiceError(ValueError):
    pass


# Only fixed COM operations. Request data is JSON, never executable PowerShell.
# TASK_CREATE (2), not CREATE_OR_UPDATE, closes the absent/register collision.
SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
  $r = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')) | ConvertFrom-Json
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $sid = $id.User.Value
  $user = $id.Name.Split('\')[-1]
  if ($r.action -eq 'identity') {
    @{sid=$sid; user=$user} | ConvertTo-Json -Compress
    exit 0
  }
  if ($sid -ne $r.sid -or ('\Docket-' + $user) -cne $r.name) { throw 'Current user mismatch' }
  $svc = New-Object -ComObject 'Schedule.Service'
  $svc.Connect()
  $folder = $svc.GetFolder('\')
  $task = $null
  try { $task = $folder.GetTask($r.name.Substring(1)) }
  catch {
    $cause = $_.Exception
    while ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
    if ($cause.HResult -ne -2147024894) { throw }
  }
  $def = $svc.NewTask(0)
  $def.XmlText = $r.xml
  Add-Type -AssemblyName System.Security
  function Canon($text) {
    if ($text.Length -gt 65536) { throw 'Task XML too large' }
    $settings = New-Object System.Xml.XmlReaderSettings
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]::new($text), $settings)
    $doc = New-Object System.Xml.XmlDocument
    $doc.PreserveWhitespace = $true
    $doc.XmlResolver = $null
    try { $doc.Load($reader) } finally { $reader.Dispose() }
    $ns = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    function XPath($path) { return '/t:' + $path.Replace('/', '/t:') }
    $descriptors = @($doc.SelectNodes('/t:Task/t:RegistrationInfo/t:SecurityDescriptor', $ns))
    if ($descriptors.Count -gt 1) { throw 'Duplicate embedded descriptor' }
    foreach ($node in $descriptors) {
      if ($node.Attributes.Count -ne 0 -or $node.ChildNodes.Count -ne 1 -or
          $node.FirstChild.NodeType -ne [System.Xml.XmlNodeType]::Text) { throw 'Invalid embedded descriptor' }
      VerifySD $node.InnerText
      $null = $node.ParentNode.RemoveChild($node)
    }
    foreach ($rule in @(DEFAULT_RULES)) {
      $path, $value = $rule.Split('|')
      $at = $path.LastIndexOf('/')
      foreach ($parent in @($doc.SelectNodes((XPath $path.Substring(0,$at)), $ns))) {
        $name = $path.Substring($at+1)
        $fields = @($parent.SelectNodes(('t:'+$name), $ns))
        if ($fields.Count -gt 1) { throw 'Duplicate schema scalar' }
        foreach ($child in $fields) {
          # Omit only an exact bare default. Unknown attributes/content survive.
          # Removing rather than inserting also preserves xs:sequence order.
          if ($child.Attributes.Count -eq 0 -and $child.ChildNodes.Count -eq 1 -and
              $child.FirstChild.NodeType -eq [System.Xml.XmlNodeType]::Text -and
              $child.InnerText -ceq $value) { $null = $parent.RemoveChild($child) }
        }
      }
    }
    foreach ($node in @($doc.SelectNodes((XPath 'Task/Triggers/LogonTrigger/UserId'), $ns))) {
      if ($node.ChildNodes.Count -eq 1 -and
          $node.FirstChild.NodeType -eq [System.Xml.XmlNodeType]::Text -and
          $node.InnerText -ieq $id.Name) { $node.InnerText = $sid }
    }
    foreach ($path in @(ELEMENT_RULES)) {
      foreach ($parent in @($doc.SelectNodes((XPath $path), $ns))) {
        foreach ($child in @($parent.ChildNodes)) {
          if ($child.NodeType -in @([System.Xml.XmlNodeType]::Whitespace,
                                   [System.Xml.XmlNodeType]::SignificantWhitespace)) {
            $null = $parent.RemoveChild($child)
          } elseif ($child.NodeType -in @([System.Xml.XmlNodeType]::Text, [System.Xml.XmlNodeType]::CDATA)) {
            throw 'Unexpected mixed schema content'
          }
        }
      }
    }
    foreach ($path in @(ALL_RULES)) {
      foreach ($parent in @($doc.SelectNodes((XPath $path), $ns))) {
        $children = @($parent.ChildNodes | Where-Object { $_ -is [System.Xml.XmlElement] } |
                      Sort-Object -CaseSensitive NamespaceURI, LocalName)
        foreach ($child in $children) { $null = $parent.AppendChild($child) }
      }
    }
    $canon = New-Object System.Security.Cryptography.Xml.XmlDsigC14NWithCommentsTransform
    $canon.LoadInput($doc)
    $stream = New-Object System.IO.StreamReader($canon.GetOutput())
    try { return $stream.ReadToEnd() } finally { $stream.Dispose() }
  }
  $sddl = 'O:' + $sid + 'D:P(A;;FA;;;' + $sid + ')(A;;FA;;;SY)'
  $expected = New-Object Security.AccessControl.RawSecurityDescriptor($sddl)
  function AceKey($ace) {
    # Only ordinary allow ACEs can match this policy. Reject opaque/callback data.
    if ($ace -isnot [Security.AccessControl.CommonAce] -or
        $ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed -or
        $ace.IsCallback -or $ace.OpaqueLength -ne 0) { throw 'Unexpected task ACE' }
    return ('{0}:{1}:{2}:{3}' -f [int]$ace.AceType, [int]$ace.AceFlags,
            $ace.AccessMask, $ace.SecurityIdentifier.Value)
  }
  function VerifySD($text) {
    $sd = New-Object Security.AccessControl.RawSecurityDescriptor($text)
    if ($sd.Owner.Value -ne $sid -or $null -eq $sd.DiscretionaryAcl -or
        -not ($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)) {
      throw 'Task owner or DACL mismatch'
    }
    # Compare complete parsed ACE multisets, not SDDL spelling or a trustee subset.
    # Sorting is safe for this two-allow-ACE policy; no deny/inherited ACE is accepted.
    $wanted = @($expected.DiscretionaryAcl | ForEach-Object { AceKey $_ } | Sort-Object)
    $actual = @($sd.DiscretionaryAcl | ForEach-Object { AceKey $_ } | Sort-Object)
    if ($actual.Count -ne $wanted.Count) { throw 'Task DACL entry count mismatch' }
    for ($i = 0; $i -lt $wanted.Count; $i++) {
      if ($actual[$i] -cne $wanted[$i]) { throw 'Task DACL entry mismatch' }
    }
  }
  function Verify($t) {
    VerifySD ($t.GetSecurityDescriptor(7))
    if ((Canon $t.Xml) -cne (Canon $def.XmlText)) { throw 'Task definition changed or foreign; refusing' }
  }
  if ($null -eq $task) {
    if ($r.action -ne 'install') { @{present=$false} | ConvertTo-Json -Compress; exit 0 }
    # TASK_CREATE | TASK_DONT_ADD_PRINCIPAL_ACE. No overwrite or ACL repair.
    $task = $folder.RegisterTaskDefinition($r.name.Substring(1), $def, 0x12, $sid, $null, 3, $sddl)
  }
  Verify $task
  if ($r.action -in @('stop','ready')) {
    if ($r.action -eq 'stop' -and ($r.runtime -or $task.State -in @(2,4))) { $task.Stop(0) }
    $until = [DateTime]::UtcNow.AddSeconds(5)
    while ($task.State -in @(2,4)) {
      if ([DateTime]::UtcNow -ge $until) { throw 'Task did not stop; refusing further action' }
      Start-Sleep -Milliseconds 100
    }
  }
  Verify $task
  if ($r.action -in @('ready','uninstall','launch') -and $task.State -ne 3) { throw 'Task not Ready; refusing' }
  if ($r.action -eq 'uninstall') { $folder.DeleteTask($r.name.Substring(1), 0) }
  if ($r.action -eq 'launch') {
    $running = $task.Run($null)
    if ($null -eq $running) { throw 'Task launch unavailable' }
  }
  @{present=($r.action -ne 'uninstall'); state=[int]$task.State; lastResult=[int]$task.LastTaskResult} | ConvertTo-Json -Compress
} catch {
  # Never echo untrusted registered XML, environment or native exception text.
  [Console]::Error.WriteLine('Docket task operation refused or unavailable')
  exit 1
}
'''.replace("DEFAULT_RULES", _ps_rules(path + "|" + value for path, value in XML_DEFAULTS.items())
).replace("ELEMENT_RULES", _ps_rules(XML_ELEMENT_ONLY)).replace("ALL_RULES", _ps_rules(XML_ALL))


def _powershell_exe() -> str:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buf = ctypes.create_unicode_buffer(32768)
    size = kernel.GetSystemDirectoryW(buf, len(buf))
    if not size or size >= len(buf):
        raise ServiceError("native system directory unavailable")
    return ntpath.join(path_value(buf.value), "WindowsPowerShell", "v1.0", "powershell.exe")


def _powershell(request: dict, *, deadline=None, script=SCRIPT) -> dict:
    exe = _powershell_exe()
    payload = json.dumps(request, ensure_ascii=True).encode("utf-8")
    if len(payload) > LIMIT:
        raise ServiceError("task request too large")
    script = script.replace("PAYLOAD", base64.b64encode(payload).decode("ascii"))
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # CreateProcess has a 32767 UTF-16 character command-line ceiling.
    args = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    if len(subprocess.list2cmdline(args)) >= 32000:
        raise ServiceError("task paths exceed native command size limit")
    timeout = min(20, _remaining(deadline)) if deadline is not None else 20
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **native.child_options())
    output = [b"", b""]
    overflow = threading.Event()

    def collect(index, pipe):
        try:
            output[index] = pipe.read(LIMIT + 1)
            if len(output[index]) > LIMIT:
                overflow.set()
                proc.kill()
        finally:
            pipe.close()

    readers = [threading.Thread(target=collect, args=(i, pipe), daemon=True)
               for i, pipe in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    cleanup_until = None
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        cleanup_until = time.monotonic() + 5
        proc.kill()
        proc.wait(timeout=_remaining(cleanup_until))
        raise ServiceError("task operation timed out") from None
    except BaseException:
        # Cancellation reaps only our controller child; never Stop/Run a task here.
        cleanup_until = time.monotonic() + 5
        proc.kill()
        proc.wait(timeout=_remaining(cleanup_until))
        raise
    finally:
        until = cleanup_until or min(time.monotonic() + 5, deadline if deadline is not None else float("inf"))
        for reader in readers:
            reader.join(timeout=max(0, until - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        raise ServiceError("task output unavailable")
    if overflow.is_set() or proc.returncode:
        raise ServiceError("task operation refused or unavailable (no task changes retried)")
    return json.loads(output[0].decode("utf-8-sig"))


# Shipped System.Management via PowerShell's WMI accelerator, not optional modules
# or an Add-Type compiler. One numeric PID only; never enumerate by name or kill.
PROCESS_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
  $r = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')) | ConvertFrom-Json
  if ([string]$r.pid -notmatch '^[1-9][0-9]{0,9}$' -or [uint64]$r.pid -gt 4294967295) { throw 'PID' }
  if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -cne $r.sid) { throw 'Identity' }
  $p = [wmi]("\\.\root\cimv2:Win32_Process.Handle='" + [uint32]$r.pid + "'")
  $p.Get()
  if ($r.image -and $p.ExecutablePath -ine $r.image) { throw 'Image' }
  $owner = $p.GetOwnerSid()
  if ($owner.ReturnValue -ne 0) { throw 'Owner' }
  @{pid=[int64]$p.ProcessId; parent=[int64]$p.ParentProcessId; sid=$owner.Sid;
    image=$p.ExecutablePath; command=$p.CommandLine} | ConvertTo-Json -Compress
} catch { [Console]::Error.WriteLine('Docket runtime identity unavailable'); exit 1 }
'''


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ServiceError("service operation timed out; no launch retried")
    return value


def _argv(command):
    import ctypes
    from ctypes import wintypes

    if not isinstance(command, str) or not command or len(command) > 32767:
        raise ServiceError("runtime command unavailable")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    args = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not args:
        raise ServiceError("runtime command unavailable")
    try:
        return list(args[:count.value])
    finally:
        kernel.LocalFree(args)


def _hint(data):
    # Discovery metadata is deliberately ordinary/nonsecret, NOT a private file.
    try:
        with (Path(data["home"]) / "server.json").open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return None
    if len(raw) > 4096:
        raise ServiceError("runtime metadata unavailable")
    info = json.loads(raw)
    if not isinstance(info, dict) or type(info.get("pid")) is not int or not 0 < info["pid"] <= 0xFFFFFFFF:
        raise ServiceError("runtime metadata unavailable")
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", str(info.get("url", "")))
    if not match or not 0 < int(match[1]) < 65536:
        raise ServiceError("runtime URL unavailable")
    return info


def _process(pid, data, deadline, *, image=None):
    before = native.windows_process_start(pid)
    if before is None:
        return None
    request = dict(pid=pid, sid=data["sid"])
    if image is not None:
        request["image"] = image  # Filter parent before requesting owner/command.
    row = _powershell(request, deadline=deadline, script=PROCESS_SCRIPT)
    after = native.windows_process_start(pid)
    args = _argv(row.get("command"))
    expected = ["-B", "-m", "docket.service_windows", "--launch", ntpath.join(data["home"], MANIFEST)]
    if (before != after or row.get("pid") != pid or row.get("sid") != data["sid"] or
            len(args) != 6 or args[1:] != expected or not row.get("image") or
            not (os.path.samefile(path_value(row["image"]), path_value(args[0])) or
                 os.path.samefile(data["python"], path_value(args[0])))):
        raise ServiceError("runtime identity mismatch")
    return dict(pid=pid, birth=before, image=path_value(row["image"]), parent=row.get("parent"))


def _snapshot(info, data, deadline):
    if info is None:
        return ()
    daemon = _process(info["pid"], data, deadline)
    if daemon is None:
        return ()
    processes = [daemon]
    if not os.path.samefile(daemon["image"], data["python"]):
        # Only this venv's console interpreter establishes the known base image.
        console = ntpath.join(ntpath.dirname(data["python"]), "python.exe")
        base = path_value(str(getattr(sys, "_base_executable", "")))
        image = ntpath.join(ntpath.dirname(base), "pythonw.exe")
        if (ntpath.basename(base).lower() != "python.exe" or not os.path.samefile(sys.executable, console)
                or not os.path.samefile(daemon["image"], image)):
            raise ServiceError("runtime interpreter mismatch")
        parent = daemon["parent"]
        if type(parent) is not int or not 0 < parent <= 0xFFFFFFFF or parent == daemon["pid"]:
            raise ServiceError("runtime parent unavailable")
        redirector = _process(parent, data, deadline, image=data["python"])
        if (redirector is None or not os.path.samefile(redirector["image"], data["python"]) or
                int(redirector["birth"].rsplit(":", 1)[1]) > int(daemon["birth"].rsplit(":", 1)[1])):
            raise ServiceError("runtime redirector mismatch")
        processes.append(redirector)
    if any(native.windows_process_start(p["pid"]) != p["birth"] for p in processes):
        raise ServiceError("runtime identity changed")
    return tuple(processes)


def _healthy(info, deadline):
    # Fixed api.Handler._send contract, not a general HTTP client. No buffered
    # parser can consume trickled status/headers/chunk framing behind our clock.
    try:
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", str(info.get("url", "")))
        if not match or not 0 < int(match[1]) < 65536:
            return False
        port = int(match[1])
        _remaining(deadline)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            def step(method, *args):
                sock.settimeout(min(0.5, _remaining(deadline)))
                result = method(*args)
                _remaining(deadline)
                return result

            step(sock.connect, ("127.0.0.1", port))
            request = (f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       "Connection: close\r\n\r\n").encode("ascii")
            while request:
                sent = step(sock.send, request)
                if not sent:
                    return False
                request = request[sent:]
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= 2048:
                    return False
                chunk = step(sock.recv, 1)
                if not chunk:
                    return False
                header.extend(chunk)
            lines = header.decode("ascii").split("\r\n")
            if lines[0] != "HTTP/1.1 200 OK":
                return False
            headers = {}
            for line in lines[1:-2]:
                name, sep, value = line.partition(": ")
                name = name.lower()
                if (not sep or name in headers or
                        name not in ("server", "date", "content-type", "content-length", "connection") or
                        any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    return False
                headers[name] = value
            length = headers.get("content-length", "")
            if (not re.fullmatch(r"[1-9][0-9]{0,3}", length) or int(length) > 4096 or
                    headers.get("content-type") != "application/json" or
                    headers.get("connection", "close").lower() != "close"):
                return False
            raw = bytearray()
            while True:
                chunk = step(sock.recv, int(length) + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > int(length):
                    return False
            if len(raw) != int(length):
                return False
            _remaining(deadline)
            payload = json.loads(raw)
            _remaining(deadline)
            return isinstance(payload, dict) and payload.get("ok") is True
    except (OSError, ValueError, RecursionError):
        pass
    return False


def _lifecycle(action, data, deadline):
    def control(verb, **fields):
        _remaining(deadline)
        return _powershell(dict(action=verb, sid=data["sid"], name="\\Docket-" + data["user"],
                                xml=definition(data), **fields), deadline=deadline)

    state = control("status")
    if not state["present"]:
        return state
    info = _hint(data)
    old = _snapshot(info, data, deadline)
    if state["state"] not in (2, 3, 4) or (state["state"] in (2, 4) and not old):
        raise ServiceError("runtime unavailable; no task touched")
    if action == "start" and old:
        try:
            if (state["state"] != 4 or
                    any(native.windows_process_start(p["pid"]) != p["birth"] for p in old) or
                    not _healthy(info, deadline) or
                    any(native.windows_process_start(p["pid"]) != p["birth"] for p in old)):
                raise ServiceError("existing runtime unavailable")
            _remaining(deadline)
        except (OSError, ValueError):
            raise ServiceError("existing runtime unavailable") from None
        return state  # Idempotent; no Run and no Stop.
    if action != "start":
        control("stop", runtime=bool(old))
        while any(native.windows_process_start(p["pid"]) == p["birth"] for p in old):
            time.sleep(min(0.1, _remaining(deadline)))
        state = control("ready")
    if not state["present"] or state["state"] != 3:
        raise ServiceError("task not Ready; refusing further action")
    if action == "uninstall":
        return control("uninstall")
    if action == "stop":
        return state
    launched_after = time.time_ns() // 100 + 116444736000000000  # Windows FILETIME epoch.
    result = control("launch")  # Exactly one Run, never a retry after failure.
    candidate = None
    while True:
        _remaining(deadline)
        try:
            fresh = _hint(data)
        except json.JSONDecodeError:
            fresh = None  # write_text can be in progress; not readiness or absence proof.
        if candidate is None and fresh:
            birth = native.windows_process_start(fresh["pid"])
            if (birth and int(birth.rsplit(":", 1)[1]) >= launched_after
                    and all(birth != p["birth"] for p in old)):
                candidate = _snapshot(fresh, data, deadline)
                if not candidate or any(int(p["birth"].rsplit(":", 1)[1]) < launched_after for p in candidate):
                    raise ServiceError("new runtime exited")
                info = fresh
        if candidate:
            if any(native.windows_process_start(p["pid"]) != p["birth"] for p in candidate):
                raise ServiceError("new runtime exited")
            if _healthy(info, deadline):
                if any(native.windows_process_start(p["pid"]) != p["birth"] for p in candidate):
                    raise ServiceError("new runtime exited during readiness")
                _remaining(deadline)
                return result
        time.sleep(min(0.1, _remaining(deadline)))


def path_value(value: str) -> str:
    """Refuse Windows ambiguities, XML controls and scheduler % expansion."""
    if not native.fully_qualified_path(value, windows=True) or value.startswith("\\\\"):
        raise ServiceError("service paths must be absolute local drive paths")
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
           or ord(c) in (0xFFFE, 0xFFFF) or c in '%"<>|?*' for c in value):
        raise ServiceError("service path contains unsafe characters")
    drive, tail = ntpath.splitdrive(value)
    parts = tail.replace("/", "\\").split("\\")[1:]
    if not parts or any(not p or p in (".", "..") or p.endswith((" ", ".")) or ":" in p
                        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
                        for p in parts):
        raise ServiceError("service path is ambiguous")
    return drive + "\\" + "\\".join(parts)


def _identity(*, deadline=None) -> dict:
    identity = _powershell({"action": "identity"}, deadline=deadline)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identity["user"]) or not re.fullmatch(
            r"S-1-5-21-(?:\d+-){3}\d+", identity["sid"]):
        raise ServiceError("unsupported current-user task identity")
    return identity


def _deployment(root: str, python: str) -> str:
    """Explicit input is a trust assertion; still refuse temp/worktree/reparse paths."""
    for value in (root, python):
        path_value(value)
        native._check_no_windows_reparse_ancestors(Path(value))
        for key in ("TEMP", "TMP"):
            temp = os.environ.get(key)
            same_drive = temp and ntpath.splitdrive(value)[0].lower() == ntpath.splitdrive(temp)[0].lower()
            if same_drive and ntpath.commonpath([value, temp]).lower() == ntpath.normpath(temp).lower():
                raise ServiceError("temporary service deployment refused")
        if any(p.lower() in ("wt", "worktrees", ".worktrees", "temp", "tmp")
               for p in Path(value).parts):
            raise ServiceError("temporary/development worktree deployment refused")
    if (Path(root) / ".git").is_file() or not Path(python).is_file():
        raise ServiceError("worktree or missing interpreter")
    if ntpath.basename(python).lower() != "pythonw.exe":
        raise ServiceError("DOCKET_SERVICE_PYTHON must name explicit persistent pythonw.exe")
    digest = hashlib.sha256()
    package = Path(root) / "docket"
    files = sorted(package.rglob("*.py"))
    if not files or not all((package / f).is_file() for f in ("__main__.py", "service_windows.py")):
        raise ServiceError("persistent Docket package missing")
    for path in files:
        native._check_no_windows_reparse_ancestors(path)
        digest.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _validate(data: dict, identity: dict) -> dict:
    if set(data) != {"version", "root", "python", "home", "sid", "user", "digest"} or data["version"] != 1:
        raise ServiceError("unknown service manifest")
    if any(data[k] != identity[k] for k in ("sid", "user")):
        raise ServiceError("manifest user mismatch")
    for key in ("root", "python", "home"):
        if path_value(data[key]) != data[key]:
            raise ServiceError("manifest path is not canonical")
    if data["home"] != path_value(str(native.state_root())):
        raise ServiceError("manifest state path mismatch")
    if _deployment(data["root"], data["python"]) != data["digest"]:
        raise ServiceError("deployed source changed; refusing task control")
    for key, field in (("DOCKET_SERVICE_ROOT", "root"), ("DOCKET_SERVICE_PYTHON", "python")):
        if os.environ.get(key) and path_value(os.environ[key]) != data[field]:
            raise ServiceError("explicit deployment differs from manifest")
    return data


def definition(data: dict) -> str:
    task = ET.Element("Task", {"xmlns": NS, "version": "1.2"})

    def add(parent, tag, text=None, **attrs):
        element = ET.SubElement(parent, tag, attrs)
        element.text = text
        return element

    info = add(task, "RegistrationInfo")
    add(info, "Description", "Docket per-user service v1; source sha256=" + data["digest"])
    add(info, "URI", "\\Docket-" + data["user"])
    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", data["sid"])
    principal = add(add(task, "Principals"), "Principal", id="DocketUser")
    add(principal, "UserId", data["sid"])
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                       ("DisallowStartIfOnBatteries", "false"), ("StopIfGoingOnBatteries", "false"),
                       ("AllowHardTerminate", "true"), ("StartWhenAvailable", "false"),
                       ("RunOnlyIfNetworkAvailable", "false")):
        add(settings, key, value)
    idle = add(settings, "IdleSettings")
    add(idle, "StopOnIdleEnd", "false")
    add(idle, "RestartOnIdle", "false")
    for key, value in (("AllowStartOnDemand", "true"), ("Enabled", "true"), ("Hidden", "true"),
                       ("RunOnlyIfIdle", "false"), ("WakeToRun", "false"),
                       ("ExecutionTimeLimit", "PT0S"), ("Priority", "7")):
        add(settings, key, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "3")
    action = add(add(task, "Actions", Context="DocketUser"), "Exec")
    add(action, "Command", data["python"])
    add(action, "Arguments", subprocess.list2cmdline([
        "-B", "-m", "docket.service_windows", "--launch", ntpath.join(data["home"], MANIFEST)]))
    add(action, "WorkingDirectory", data["root"])
    return ET.tostring(task, encoding="unicode")


def run(action: str) -> int:
    try:
        deadline = time.monotonic() + OPERATION_SECONDS
        if sys.platform != "win32" or action not in ("install", "status", "start", "stop", "restart", "uninstall"):
            raise ServiceError("unsupported Windows service operation")
        identity = _identity(deadline=deadline)
        if action == "install" and not all(os.environ.get(k) for k in (
                "DOCKET_SERVICE_ROOT", "DOCKET_SERVICE_PYTHON")):
            raise ServiceError("install requires explicit persistent root and pythonw")
        home = Path(path_value(str(native.state_root())))
        manifest = home / MANIFEST
        try:
            data = json.loads(native.read_private_bytes(manifest))
        except FileNotFoundError:
            if action != "install":
                raise ServiceError("manifest absent; no task touched") from None
            root = path_value(os.environ.get("DOCKET_SERVICE_ROOT", ""))
            python = path_value(os.environ.get("DOCKET_SERVICE_PYTHON", ""))
            data = dict(version=1, root=root, python=python, home=str(home), **identity,
                        digest=_deployment(root, python))
            native.create_private_file(manifest, json.dumps(data).encode("utf-8"))
        data = _validate(data, identity)
        name = "\\Docket-" + data["user"]
        if action in ("start", "stop", "restart", "uninstall"):
            result = _lifecycle(action, data, deadline)
        else:
            result = _powershell(dict(action=action, sid=data["sid"], name=name, xml=definition(data)), deadline=deadline)
        print(f"docket service: {name} {action}: {json.dumps(result, sort_keys=True)}")
        print(f"logs: {home / 'daemon.out.log'} ; {home / 'daemon.err.log'}")
        return 0 if result["present"] or action == "uninstall" else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("docket service: refused or unavailable; verify private manifest, deployment and exact task ownership")
        return 1


def _redirect(home: Path) -> None:
    """Append through verified native private handles, never ordinary open()."""
    import msvcrt

    for number, name in ((1, "daemon.out.log"), (2, "daemon.err.log")):
        path = home / name
        try:
            native.create_private_file(path)
        except FileExistsError:
            pass
        with native._windows_verified_directory(home) as directory:
            handle = native._windows_raw_open(
                directory.child_path(name), 0x0004 | native._READ_CONTROL | native._FILE_READ_ATTRIBUTES | native._SYNCHRONIZE,
                native._OPEN_EXISTING, native._FILE_ATTRIBUTE_NORMAL | native._FILE_FLAG_OPEN_REPARSE_POINT)
            try:
                native._windows_verify_handle(
                    handle, user_sid_str=directory.user_sid_str, user_sid_ptr=directory.user_sid_ptr,
                    is_directory=False, parent_identity=directory.identity)
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND)
            except BaseException:
                native._windows_apis()[0].CloseHandle(handle)
                raise
        stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        # pythonw may have no original streams; create/replace CRT descriptors too.
        os.dup2(stream.fileno(), number)
        setattr(sys, "stdout" if number == 1 else "stderr", stream)


def launch(manifest: str) -> None:
    if sys.platform != "win32":
        raise ServiceError("native Windows launcher only")
    manifest = path_value(manifest)
    data = json.loads(native.read_private_bytes(Path(manifest)))
    # No scheduler env is assumed. Only the guarded manifest selects DOCKET_HOME.
    if manifest != ntpath.join(path_value(data["home"]), MANIFEST):
        raise ServiceError("unexpected manifest path")
    os.environ["DOCKET_HOME"] = data["home"]
    data = _validate(data, _identity())
    if not os.path.samefile(sys.executable, data["python"]) or not os.path.samefile(
            Path(__file__).resolve().parent.parent, data["root"]):
        raise ServiceError("launcher is not the deployed interpreter/package")
    for key in tuple(os.environ):
        if key.startswith("DOCKET_"):
            del os.environ[key]
    os.environ.update(DOCKET_HOME=data["home"], DOCKET_SERVICE_ROOT=data["root"],
                      DOCKET_SERVICE_PYTHON=data["python"], PYTHONDONTWRITEBYTECODE="1")
    os.chdir(data["root"])
    _redirect(Path(data["home"]))
    sys.argv = ["docket", "daemon"]
    runpy.run_module("docket", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--launch":
        raise SystemExit(1)
    launch(sys.argv[2])
