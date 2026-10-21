"""Explicit mocked controller tests only; never contacts Windows or the daemon."""
import contextlib
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET
from xml.dom import minidom, Node

from docket import service, service_windows as win
from tests.support import block_source_service_processes


def setUpModule():
    guard = block_source_service_processes()
    unittest.addModuleCleanup(guard.close)


IDENTITY = {"user": "synth", "sid": "S-1-5-21-111-222-333-1001"}
DATA = dict(version=1, root=r"C:\Users\synth\Apps\Docket", python=r"C:\Python311\pythonw.exe",
            home=r"C:\Users\synth\AppData\Local\Docket", digest="a" * 64, **IDENTITY)

# Synthetic reproductions of Scheduler normalization, not raw native evidence.
# Account/profile, SID components and identifiers preserve original value widths.
NATIVE_REGISTERED = r'''<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><SecurityDescriptor>O:S-1-5-21-1111111111-222222222-3333333333-1001D:P(A;;FA;;;S-1-5-21-1111111111-222222222-3333333333-1001)(A;;FA;;;SY)</SecurityDescriptor>
<Description>Owned one-shot Docket install proof aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bootstrap sha256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</Description>
<URI>\Docket-Install-Probe-aaaa-cccccccccccc</URI></RegistrationInfo>
<Principals><Principal id="DocketUser"><UserId>S-1-5-21-1111111111-222222222-3333333333-1001</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals>
<Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT4M</ExecutionTimeLimit><Hidden>true</Hidden><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings></Settings>
<Triggers />
<Actions Context="DocketUser"><Exec><Command>C:\Users\synth\AppData\Local\Programs\Python\Python311\pythonw.exe</Command><Arguments>-B C:\Users\synth\AppData\Local\Programs\Docket\builds\service-aaaaaaaaaaaa\bootstrap.py</Arguments><WorkingDirectory>C:\Users\synth\AppData\Local\Programs\Docket\builds\service-aaaaaaaaaaaa\src</WorkingDirectory></Exec></Actions></Task>'''
NATIVE_EXPECTED = r'''<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Owned one-shot Docket install proof aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bootstrap sha256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</Description><URI>\Docket-Install-Probe-aaaa-cccccccccccc</URI></RegistrationInfo>
<Triggers />
<Principals><Principal id="DocketUser"><UserId>S-1-5-21-1111111111-222222222-3333333333-1001</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>false</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT4M</ExecutionTimeLimit><Priority>7</Priority></Settings>
<Actions Context="DocketUser"><Exec><Command>C:\Users\synth\AppData\Local\Programs\Python\Python311\pythonw.exe</Command><Arguments>-B C:\Users\synth\AppData\Local\Programs\Docket\builds\service-aaaaaaaaaaaa\bootstrap.py</Arguments><WorkingDirectory>C:\Users\synth\AppData\Local\Programs\Docket\builds\service-aaaaaaaaaaaa\src</WorkingDirectory></Exec></Actions></Task>'''


class TaskXmlSourceFixtureTests(unittest.TestCase):
    """Pure structural policy mirror; does NOT execute PowerShell/.NET C14N."""

    def key(self, text, account=r"EXAMPLE\person", sid="S-1-5-21-111-222-333-1001"):
        doc = minidom.parseString(text)
        def nodes(path):
            found = [doc]
            for name in path.split("/"):
                found = [c for parent in found for c in parent.childNodes
                         if c.nodeType == Node.ELEMENT_NODE and c.namespaceURI == win.NS and c.localName == name]
            return found
        descriptors = nodes("Task/RegistrationInfo/SecurityDescriptor")
        if len(descriptors) > 1:
            raise ValueError("duplicate descriptor")
        desc_sid = "S-1-5-21-1111111111-222222222-3333333333-1001"
        # Only model this intended textual fixture. Native RawSecurityDescriptor
        # equivalence/malformed parsing remains explicitly review-gated.
        intended = f"O:{desc_sid}D:P(A;;FA;;;{desc_sid})(A;;FA;;;SY)"
        for node in descriptors:
            if node.attributes.length or len(node.childNodes) != 1 or node.firstChild.nodeType != Node.TEXT_NODE or node.firstChild.data != intended:
                raise ValueError("invalid descriptor fixture")
            node.parentNode.removeChild(node)
        for path, value in win.XML_DEFAULTS.items():
            parent_path, name = path.rsplit("/", 1)
            for parent in nodes(parent_path):
                fields = [c for c in parent.childNodes if c.nodeType == Node.ELEMENT_NODE and c.namespaceURI == win.NS and c.localName == name]
                if len(fields) > 1:
                    raise ValueError("duplicate scalar")
                for child in fields:
                    if not child.attributes.length and len(child.childNodes) == 1 and child.firstChild.nodeType == Node.TEXT_NODE and child.firstChild.data == value:
                        parent.removeChild(child)
        for node in nodes("Task/Triggers/LogonTrigger/UserId"):
            if len(node.childNodes) == 1 and node.firstChild.nodeType == Node.TEXT_NODE and node.firstChild.data.casefold() == account.casefold():
                node.firstChild.data = sid
        def visit(node, path):
            if node.nodeType != Node.ELEMENT_NODE:
                return (node.nodeType, node.nodeValue)
            children = []
            for child in node.childNodes:
                if path in win.XML_ELEMENT_ONLY and child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
                    if child.nodeType == Node.TEXT_NODE and not child.data.strip():
                        continue
                    raise ValueError("mixed schema content")
                children.append(visit(child, path + "/" + child.localName if child.nodeType == Node.ELEMENT_NODE and child.namespaceURI == win.NS else ""))
            if path in win.XML_ALL:
                children.sort(key=repr)
            attrs = tuple(sorted((a.namespaceURI or "", a.localName, a.value) for a in node.attributes.values()))
            return (node.namespaceURI, node.localName, attrs, tuple(children))
        return visit(doc.documentElement, "Task")

    def test_native_expected_and_registered_definitions_equivalent(self):
        self.assertEqual(self.key(NATIVE_EXPECTED), self.key(NATIVE_REGISTERED))

    def test_all_documented_omissions_equivalent_individually(self):
        for path, value in win.XML_DEFAULTS.items():
            name = path.rsplit("/", 1)[1]
            with self.subTest(path=path):
                parts = path.split("/")[1:-1]
                # A minimal namespaced tree at the exact documented path.
                body = f"<{name}>{value}</{name}>"
                for part in reversed(parts):
                    body = f"<{part}>{body}</{part}>"
                explicit = f'<Task xmlns="{win.NS}">{body}</Task>'
                omitted = explicit.replace(f"<{name}>{value}</{name}>", "")
                self.assertEqual(self.key(explicit), self.key(omitted))
                self.assertNotEqual(self.key(omitted), self.key(explicit.replace(f">{value}<", ">changed<")))

    def test_changed_execution_principal_and_settings_rejected(self):
        for old, new in (("pythonw.exe", "other.exe"), ("-B C:", "-X C:"),
                         ("\\src</WorkingDirectory>", "\\other</WorkingDirectory>"),
                         ("InteractiveToken", "Password"), ("LeastPrivilege", "HighestAvailable"),
                         ("-1001</UserId>", "-1002</UserId>"), ('id="DocketUser"', 'id="Other"'),
                         ("IgnoreNew", "Parallel"), ("PT4M", "PT0S"),
                         ("<Priority>7", "<Priority>8"), ("<Hidden>true", "<Hidden>false"),
                         ("<AllowHardTerminate>true", "<AllowHardTerminate>false"),
                         ("<WakeToRun>false", "<WakeToRun>true"),
                         ("<StartWhenAvailable>false", "<StartWhenAvailable>true"),
                         ("<Enabled>true", "<Enabled>false")):
            with self.subTest(old=old):
                changed = NATIVE_EXPECTED.replace(old, new)
                self.assertNotEqual(changed, NATIVE_EXPECTED)
                self.assertNotEqual(self.key(changed), self.key(NATIVE_EXPECTED))

    def test_triggers_and_action_sequence_preserved(self):
        explicit = NATIVE_EXPECTED.replace("<Triggers />", "<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>u</UserId></LogonTrigger></Triggers>")
        self.assertEqual(self.key(explicit), self.key(explicit.replace("<Enabled>true</Enabled>", "")))
        self.assertNotEqual(self.key(NATIVE_EXPECTED), self.key(NATIVE_EXPECTED.replace(
            "<Triggers />", "<Triggers><LogonTrigger><UserId>foreign</UserId></LogonTrigger></Triggers>")))
        a = NATIVE_EXPECTED.replace("</Actions>", "<Exec><Command>second.exe</Command></Exec></Actions>")
        doc = minidom.parseString(a)
        actions = doc.getElementsByTagName("Actions")[0]
        actions.appendChild(actions.firstChild)
        self.assertNotEqual(self.key(a), self.key(doc.toxml()))
        trigger = "<LogonTrigger><Enabled>false</Enabled><UserId>u</UserId></LogonTrigger>"
        other = "<LogonTrigger><UserId>u</UserId><Enabled>false</Enabled></LogonTrigger>"
        self.assertNotEqual(self.key(NATIVE_EXPECTED.replace("<Triggers />", "<Triggers>" + trigger + "</Triggers>")),
                            self.key(NATIVE_EXPECTED.replace("<Triggers />", "<Triggers>" + other + "</Triggers>")))

    def test_unknown_elements_attributes_text_namespace_and_duplicates_preserved(self):
        for old, new in (("</Settings>", "<Unknown>value</Unknown></Settings>"),
                         ("</Settings>", "<Unknown> </Unknown></Settings>"),
                         ("</Settings>", '<Unknown xmlns="urn:other">value</Unknown></Settings>'),
                         ("<Priority>", '<Priority unknown="x">'),
                         ("<Task ", '<Task xmlns:extra="urn:extra" ')):
            with self.subTest(new=new):
                self.assertNotEqual(self.key(NATIVE_EXPECTED), self.key(NATIVE_EXPECTED.replace(old, new)))
        a = NATIVE_EXPECTED.replace("</Settings>", "<Unknown> </Unknown></Settings>")
        self.assertNotEqual(self.key(a), self.key(a.replace("<Unknown> </Unknown>", "<Unknown>  </Unknown>")))
        with self.assertRaises(ValueError):
            self.key(NATIVE_EXPECTED.replace("</Settings>", "<Priority>7</Priority></Settings>"))
        # Same local name outside its schema path is not a removable default.
        self.assertNotEqual(self.key(a), self.key(a.replace("<Unknown> </Unknown>", "<Unknown><Enabled>true</Enabled></Unknown>")))

    def test_malformed_changed_and_duplicate_embedded_descriptors_refused(self):
        start = NATIVE_REGISTERED.index("<SecurityDescriptor>")
        end = NATIVE_REGISTERED.index("</SecurityDescriptor>") + len("</SecurityDescriptor>")
        descriptor = NATIVE_REGISTERED[start:end]
        for changed in ("<SecurityDescriptor>not-sddl</SecurityDescriptor>",
                        descriptor.replace("D:P", "D:"), descriptor.replace("FA", "FR"),
                        descriptor.replace("SY", "WD"), descriptor + descriptor,
                        descriptor.replace("<SecurityDescriptor>", '<SecurityDescriptor extra="x">'),
                        "<SecurityDescriptor><Unknown/></SecurityDescriptor>"):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.key(NATIVE_REGISTERED.replace(descriptor, changed))

    def test_logon_trigger_user_id_account_spelling_normalized_to_current_sid(self):
        account = r"EXAMPLE\person"
        sid = "S-1-5-21-111-222-333-1001"
        expected = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{sid}</UserId></LogonTrigger></Triggers>")
        # Native Task Scheduler writes qualified account spelling for current identity
        actual = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{account}</UserId></LogonTrigger></Triggers>")
        self.assertEqual(self.key(expected, account=account, sid=sid),
                         self.key(actual, account=account, sid=sid))

    def test_logon_trigger_user_id_case_equivalent_account_spelling_accepted(self):
        account = r"EXAMPLE\person"
        sid = "S-1-5-21-111-222-333-1001"
        expected = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{sid}</UserId></LogonTrigger></Triggers>")
        for variant in (r"example\PERSON", r"Example\Person", r"EXAMPLE\PERSON"):
            actual = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{variant}</UserId></LogonTrigger></Triggers>")
            with self.subTest(variant=variant):
                self.assertEqual(self.key(expected, account=account, sid=sid),
                                 self.key(actual, account=account, sid=sid))

    def test_logon_trigger_user_id_refuses_other_domain_user_bare_name_and_wrong_sid(self):
        account = r"EXAMPLE\person"
        sid = "S-1-5-21-111-222-333-1001"
        expected = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{sid}</UserId></LogonTrigger></Triggers>")
        for refused in (r"OTHER\person", r"EXAMPLE\other", "person", "S-1-5-21-999-888-777-1002"):
            actual = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{refused}</UserId></LogonTrigger></Triggers>")
            with self.subTest(refused=refused):
                self.assertNotEqual(self.key(expected, account=account, sid=sid),
                                    self.key(actual, account=account, sid=sid))

    def test_logon_trigger_user_id_refuses_other_paths_namespaces_and_malformed_children(self):
        account = r"EXAMPLE\person"
        sid = "S-1-5-21-111-222-333-1001"
        expected = NATIVE_EXPECTED.replace("<Triggers />", f"<Triggers><LogonTrigger><UserId>{sid}</UserId></LogonTrigger></Triggers>")
        # Other paths: Principal.UserId retaining account name must not normalize
        actual_principal = expected.replace(
            "<UserId>S-1-5-21-1111111111-222222222-3333333333-1001</UserId>",
            f"<UserId>{account}</UserId>")
        self.assertNotEqual(self.key(expected, account=account, sid=sid),
                            self.key(actual_principal, account=account, sid=sid))
        # Other namespaces: UserId outside task namespace must not normalize
        actual_other_ns = NATIVE_EXPECTED.replace(
            "<Triggers />",
            f'<Triggers><LogonTrigger><UserId xmlns="urn:other">{account}</UserId></LogonTrigger></Triggers>')
        self.assertNotEqual(self.key(expected, account=account, sid=sid),
                            self.key(actual_other_ns, account=account, sid=sid))
        # Malformed nonleaf UserId containing child elements must not normalize
        for malformed in (
            f"<Triggers><LogonTrigger><UserId><Child>{account}</Child></UserId></LogonTrigger></Triggers>",
            f"<Triggers><LogonTrigger><UserId>{account}<Child/></UserId></LogonTrigger></Triggers>",
        ):
            actual_malformed = NATIVE_EXPECTED.replace("<Triggers />", malformed)
            with self.subTest(malformed=malformed):
                self.assertNotEqual(self.key(expected, account=account, sid=sid),
                                    self.key(actual_malformed, account=account, sid=sid))

    def test_runtime_source_keeps_xml_and_acl_boundary(self):
        for text in ("$doc.PreserveWhitespace = $true", "[System.Xml.DtdProcessing]::Prohibit",
                     "VerifySD $node.InnerText", "$descriptors.Count -gt 1",
                     "XmlDsigC14NWithCommentsTransform", "Sort-Object -CaseSensitive NamespaceURI, LocalName",
                     "VerifySD ($t.GetSecurityDescriptor(7))",
                     "(XPath 'Task/Triggers/LogonTrigger/UserId')",
                     "$node.ChildNodes.Count -eq 1",
                     "$node.FirstChild.NodeType -eq [System.Xml.XmlNodeType]::Text",
                     "$node.InnerText -ieq $id.Name",
                     "$node.InnerText = $sid"):
            self.assertIn(text, win.SCRIPT)
        for path, value in win.XML_DEFAULTS.items():
            self.assertIn("'" + path + "|" + value + "'", win.SCRIPT)
        for path in win.XML_ALL:
            self.assertIn("'" + path + "'", win.SCRIPT)
        self.assertNotIn("SetSecurityDescriptor", win.SCRIPT)
        self.assertNotIn("NTAccount", win.SCRIPT)
        self.assertNotIn("Translate", win.SCRIPT)
        self.assertIn(", $def, 0x12, $sid, $null, 3, $sddl)", win.SCRIPT)


class TaskDaclSourceFixtureTests(unittest.TestCase):
    """Pure policy fixtures plus source guards, NOT PowerShell/.NET execution."""

    def ace(self, trustee, **changes):
        return dict(dict(common=True, type=0, flags=0, mask=0x1F01FF,
                         trustee=trustee, callback=False, opaque=0), **changes)

    def expected(self):
        return [self.ace(IDENTITY["sid"]), self.ace("S-1-5-18")]

    def accepts(self, aces, owner=IDENTITY["sid"], protected=True):
        # Mirror the fixed script's parsed CommonAce multiset policy. This does
        # not parse SDDL, resolve aliases or establish native Scheduler behavior.
        if owner != IDENTITY["sid"] or not protected or aces is None:
            return False
        def keys(entries):
            result = []
            for ace in entries:
                if not ace["common"] or ace["type"] != 0 or ace["callback"] or ace["opaque"] != 0:
                    raise ValueError("unexpected ACE")
                result.append(tuple(ace[field] for field in ("type", "flags", "mask", "trustee")))
            return sorted(result)
        try:
            return keys(aces) == keys(self.expected())
        except ValueError:
            return False

    def test_source_compares_complete_parsed_multisets(self):
        for text in ("$sddl = 'O:' + $sid + 'D:P(A;;FA;;;' + $sid + ')(A;;FA;;;SY)'",
                     "RawSecurityDescriptor($sddl)", "RawSecurityDescriptor($text)",
                     "VerifySD ($t.GetSecurityDescriptor(7))", "VerifySD $node.InnerText",
                     "$sd.Owner.Value -ne $sid", "$null -eq $sd.DiscretionaryAcl",
                     "[Security.AccessControl.ControlFlags]::DiscretionaryAclProtected",
                     "$ace -isnot [Security.AccessControl.CommonAce]",
                     "$ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed",
                     "$ace.IsCallback -or $ace.OpaqueLength -ne 0",
                     "'{0}:{1}:{2}:{3}' -f [int]$ace.AceType, [int]$ace.AceFlags,",
                     "$ace.AccessMask, $ace.SecurityIdentifier.Value",
                     "$wanted = @($expected.DiscretionaryAcl | ForEach-Object { AceKey $_ } | Sort-Object)",
                     "$actual = @($sd.DiscretionaryAcl | ForEach-Object { AceKey $_ } | Sort-Object)",
                     "$actual.Count -ne $wanted.Count", "$i -lt $wanted.Count",
                     "$actual[$i] -cne $wanted[$i]"):
            self.assertIn(text, win.SCRIPT)
        self.assertEqual(win.SCRIPT.count("$sddl ="), 1)
        self.assertNotIn("-notin @($sid, 'S-1-5-18')", win.SCRIPT)

    def test_equivalent_expected_order(self):
        self.assertTrue(self.accepts(self.expected()))
        self.assertTrue(self.accepts(list(reversed(self.expected()))))

    def test_required_entries_duplicates_and_extras(self):
        user, system = self.expected()
        for aces in (None, [], [user], [system], [user, user], [system, system],
                     [user, system, user], [user, system, system],
                     [user, system, self.ace("S-1-1-0")],
                     [user, self.ace("S-1-1-0")]):
            with self.subTest(aces=aces):
                self.assertFalse(self.accepts(aces))

    def test_each_ace_requires_exact_rights_flags_type_and_trustee(self):
        for index in (0, 1):
            for change in ({"mask": 0}, {"mask": 0x120089}, {"mask": 0x1F01FF | 0x10000000},
                           {"type": 1}, {"type": 5}, {"trustee": "S-1-1-0"},
                           {"common": False}, {"callback": True}, {"opaque": 4},
                           *({"flags": bit} for bit in (1, 2, 4, 8, 16, 64, 128))):
                aces = self.expected()
                aces[index] = dict(aces[index], **change)
                with self.subTest(index=index, change=change):
                    self.assertFalse(self.accepts(aces))

    def test_owner_and_protection_required(self):
        self.assertFalse(self.accepts(self.expected(), owner="S-1-5-18"))
        self.assertFalse(self.accepts(self.expected(), protected=False))

    def test_observed_native_unprotected_extra_user_read_ace_still_refuses(self):
        # Mirror the native SDDL outcome, not an allowed replacement policy.
        observed = self.expected() + [self.ace(IDENTITY["sid"], mask=0x120089)]
        self.assertFalse(self.accepts(observed, protected=False))
        self.assertFalse(self.accepts(observed, protected=True))


class WindowsServiceTests(unittest.TestCase):
    def test_paths_refuse_ambiguity_and_scheduler_expansion(self):
        for value in ("", "docket", r"C:docket", r"\docket", r"\\host\share\docket",
                      r"\\?\C:\Docket", r"C:\Apps\..\Docket", r"C:\Apps\NUL.txt",
                      "C:\\Apps\\x.", "C:\\Apps\\x ", r"C:\%USERPROFILE%\Docket",
                      'C:\\Apps\\bad"', "C:\\Apps\\bad\n", r"C:\Apps\x:stream", "C:\\Apps\\"):
            with self.subTest(value=value), self.assertRaises(win.ServiceError):
                win.path_value(value)
        self.assertEqual(win.path_value("C:/Apps/A & B/é"), "C:\\Apps\\A & B\\é")

    def test_xml_fixed_action_and_least_privilege(self):
        data = dict(DATA, root=r"C:\Apps\Docket & friends", home=r"C:\State\Docket & logs")
        xml = win.definition(data)
        task = ET.fromstring(xml)
        def text(tag):
            return task.find(".//{" + win.NS + "}" + tag).text
        self.assertEqual(text("Command"), DATA["python"])
        self.assertEqual(text("Arguments"), subprocess.list2cmdline([
            "-B", "-m", "docket.service_windows", "--launch",
            r"C:\State\Docket & logs\windows-service.json"]))
        for key, value in (("LogonType", "InteractiveToken"), ("RunLevel", "LeastPrivilege"),
                           ("MultipleInstancesPolicy", "IgnoreNew"), ("Count", "3"),
                           ("Hidden", "true"), ("URI", r"\Docket-synth")):
            self.assertEqual(text(key), value)
        self.assertNotIn("Password", xml)
        self.assertNotIn("cmd.exe", xml)
        self.assertNotIn("PATH", xml)
        self.assertIn("&amp;", xml)

    def test_manifest_validation(self):
        with patch.object(win.native, "state_root", return_value=DATA["home"]), \
                patch.object(win, "_deployment", return_value=DATA["digest"]), \
                patch.dict(os.environ, {}, clear=True):
            self.assertEqual(win._validate(DATA, IDENTITY), DATA)
            for changes in ({"sid": "S-1-5-18"}, {"version": 2}, {"digest": "changed"},
                            {"home": r"C:\Other"}, {"root": r"C:\Apps\..\Docket"}, {"extra": "no"}):
                with self.subTest(changes=changes), self.assertRaises(win.ServiceError):
                    win._validate(dict(DATA, **changes), IDENTITY)
            with patch.dict(os.environ, {"DOCKET_SERVICE_ROOT": r"C:\Other"}), self.assertRaises(win.ServiceError):
                win._validate(DATA, IDENTITY)

    def controller(self, action, present=True, raw=None, failure=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(sys, "platform", "win32"))
        stack.enter_context(patch.object(win, "_identity", return_value=IDENTITY))
        stack.enter_context(patch.object(win.native, "state_root", return_value=DATA["home"]))
        stack.enter_context(patch.object(win.native, "read_private_bytes", return_value=raw or json.dumps(DATA).encode()))
        stack.enter_context(patch.object(win, "_validate", return_value=DATA))
        stack.enter_context(patch.dict(os.environ, {"DOCKET_SERVICE_ROOT": DATA["root"],
                                                  "DOCKET_SERVICE_PYTHON": DATA["python"]}))
        native = stack.enter_context(patch.object(win, "_powershell", return_value={"present": present}, side_effect=failure))
        stack.enter_context(patch.object(win, "_lifecycle", side_effect=lambda verb, data, deadline:
                                        win._powershell(dict(action=verb, sid=data["sid"],
                                                             name=r"\Docket-synth", xml=win.definition(data)))))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return win.run(action), native

    def test_all_actions_exact_name_definition(self):
        for action in ("install", "status", "start", "stop", "restart", "uninstall"):
            with self.subTest(action=action):
                code, native = self.controller(action)
                self.assertEqual(code, 0)
                request = native.call_args.args[0]
                self.assertEqual(request, dict(action=action, sid=DATA["sid"], name=r"\Docket-synth", xml=win.definition(DATA)))

    def test_foreign_refusal_no_retry_and_absence(self):
        code, native = self.controller("start", failure=win.ServiceError("foreign"))
        self.assertEqual(code, 1)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(self.controller("status", present=False)[0], 1)
        self.assertEqual(self.controller("uninstall", present=False)[0], 0)

    def test_install_requires_explicit_inputs_even_existing(self):
        with patch.object(sys, "platform", "win32"), patch.object(win, "_identity", return_value=IDENTITY), \
                patch.dict(os.environ, {}, clear=True), patch.object(win, "_powershell") as native, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(win.run("install"), 1)
            native.assert_not_called()

    def test_delegation_selects_windows_linux_and_refuses_unsupported(self):
        with patch.object(sys, "platform", "win32"), patch.object(win, "run", return_value=7) as backend:
            self.assertEqual(service.run("status"), 7)
            backend.assert_called_once_with("status")
        from docket import service_linux
        with patch.object(sys, "platform", "linux"), patch.object(win, "run") as backend, \
                patch.object(service_linux, "run", return_value=8) as linux:
            self.assertEqual(service.run("status"), 8)
            linux.assert_called_once_with("status")
            backend.assert_not_called()
        with patch.object(sys, "platform", "freebsd13"), patch.object(win, "run") as backend, \
                patch.object(service_linux, "run") as linux, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.run("status"), 1)
            backend.assert_not_called()
            linux.assert_not_called()

    def test_source_guard_blocks_native_service_commands_before_launch(self):
        for command in ("systemctl", "launchctl", "schtasks", "PowerShell"):
            with self.subTest(command=command), self.assertRaisesRegex(AssertionError, "forbid native"):
                subprocess.run([command, "status"], timeout=1)
            with self.subTest(shell=command), self.assertRaisesRegex(AssertionError, "forbid native"):
                os.system(command)

    def test_launch_in_process_environment_and_argv(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(win.native, "read_private_bytes", return_value=json.dumps(DATA).encode()), \
                patch.object(win, "_identity", return_value=IDENTITY), \
                patch.object(win, "_validate", return_value=DATA), \
                patch.object(os.path, "samefile", return_value=True), \
                patch.object(os, "chdir") as chdir, patch.object(win, "_redirect") as logs, \
                patch.object(win.runpy, "run_module") as module, \
                patch.object(sys, "argv", ["original"]), \
                patch.dict(os.environ, {"DOCKET_SECRET": "not persisted"}, clear=True):
            win.launch(DATA["home"] + "\\" + win.MANIFEST)
            self.assertEqual(sys.argv, ["docket", "daemon"])
            self.assertEqual(os.environ["DOCKET_HOME"], DATA["home"])
            self.assertNotIn("DOCKET_SECRET", os.environ)
            self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "1")
            chdir.assert_called_once_with(DATA["root"])
            logs.assert_called_once_with(Path(DATA["home"]))
            module.assert_called_once_with("docket", run_name="__main__", alter_sys=True)

    def test_launch_refuses_manifest_location_and_deployment(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(win.native, "read_private_bytes", return_value=json.dumps(DATA).encode()), \
                patch.object(win, "_identity", return_value=IDENTITY), \
                patch.object(win, "_validate", return_value=DATA), \
                patch.object(os.path, "samefile", return_value=False), \
                patch.object(win, "_redirect") as logs, patch.object(win.runpy, "run_module") as module, \
                patch.dict(os.environ, {}, clear=True):
            for path in (r"C:\Other\windows-service.json", DATA["home"] + "\\" + win.MANIFEST):
                with self.assertRaises(win.ServiceError):
                    win.launch(path)
            logs.assert_not_called()
            module.assert_not_called()

    def test_fixed_native_script_security_guards(self):
        # Source assertions complement mocked behavior; native COM proof is deferred.
        for text in ("RegisterTaskDefinition", ", 0x12, $sid, $null, 3, $sddl)",
                     "Verify $task", "Task definition changed or foreign", "Task owner or DACL mismatch",
                     "Current user mismatch", "Task did not stop", "$folder.DeleteTask($r.name.Substring(1), 0)"):
            self.assertIn(text, win.SCRIPT)
        self.assertNotIn("Invoke-Expression", win.SCRIPT)
        self.assertNotIn("Stop-Process", win.SCRIPT)

    def test_native_runner_fixed_encoding_and_bounds(self):
        process = Mock(stdout=io.BytesIO(b'{"present":true}'), stderr=io.BytesIO(), returncode=0)
        request = dict(action="status", sid=DATA["sid"], name=r"\Docket-synth",
                       xml=win.definition(DATA), untrusted="'; & arbitrary.exe <é>")
        with patch.object(win, "_powershell_exe", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), \
                patch.object(win.native, "child_options", return_value={"stdin": subprocess.DEVNULL, "close_fds": True}), \
                patch.object(win.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(win._powershell(request), {"present": True})
            args = popen.call_args.args[0]
            self.assertEqual(args[1:5], ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
            encoded_json = base64.b64encode(json.dumps(request, ensure_ascii=True).encode()).decode()
            self.assertEqual(base64.b64decode(args[5]).decode("utf-16le"), win.SCRIPT.replace("PAYLOAD", encoded_json))
            self.assertNotIn("shell", popen.call_args.kwargs)
            process.wait.assert_called_once_with(timeout=20)
            with self.assertRaises(win.ServiceError):
                win._powershell({"action": "x" * win.LIMIT})
            self.assertEqual(popen.call_count, 1)

    def test_native_runner_timeout_and_output_overflow(self):
        for overflow in (False, True):
            process = Mock(stdout=io.BytesIO(b"x" * (win.LIMIT + 1) if overflow else b"{}"),
                           stderr=io.BytesIO(), returncode=0)
            if not overflow:
                process.wait.side_effect = [subprocess.TimeoutExpired("mock", 20), 0]
            with self.subTest(overflow=overflow), \
                    patch.object(win, "_powershell_exe", return_value=r"C:\Windows\powershell.exe"), \
                    patch.object(win.native, "child_options", return_value={}), \
                    patch.object(win.subprocess, "Popen", return_value=process), \
                    self.assertRaises(win.ServiceError):
                win._powershell({"action": "identity"})
            process.kill.assert_called_once_with()

    def test_missing_manifest_no_control_and_exclusive_install(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(win, "_identity", return_value=IDENTITY), \
                patch.object(win.native, "state_root", return_value=DATA["home"]), \
                patch.object(win.native, "read_private_bytes", side_effect=FileNotFoundError), \
                patch.object(win.native, "create_private_file") as create, \
                patch.object(win, "_deployment", return_value=DATA["digest"]), \
                patch.object(win, "_validate", return_value=DATA), \
                patch.object(win, "_powershell", return_value={"present": True}) as native, \
                patch.dict(os.environ, {"DOCKET_SERVICE_ROOT": DATA["root"], "DOCKET_SERVICE_PYTHON": DATA["python"]}), \
                contextlib.redirect_stdout(io.StringIO()):
            for action in ("status", "start", "stop", "restart", "uninstall"):
                self.assertEqual(win.run(action), 1)
            native.assert_not_called()
            create.assert_not_called()
            self.assertEqual(win.run("install"), 0)
            self.assertEqual(json.loads(create.call_args.args[1]), DATA)
            native.assert_called_once()


STAMP = 116444736000000000


class RestartSequencingTests(unittest.TestCase):
    """Virtual time + native boundaries only; no process, listener or DB exists."""

    def setUp(self):
        self.clock = 0.0
        self.stopping = False
        self.launched = False
        self.suppressed = False
        self.exit_at = 0.3
        self.calls = []
        self.old = tuple(dict(pid=p, birth=f"win:{p}:{STAMP - 100 + p}") for p in (10, 11))
        self.new = tuple(dict(pid=p, birth=f"win:{p}:{STAMP + 100 + p}") for p in (20, 21))
        self.old_info = dict(pid=10, url="http://127.0.0.1:8768")
        self.new_info = dict(pid=20, url="http://127.0.0.1:8768")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        def mock(name, **kw):
            return self.stack.enter_context(patch.object(win, name, **kw))
        mock("_powershell", side_effect=self.control)
        self.hint = mock("_hint", side_effect=lambda data: self.new_info if self.launched and not self.suppressed else self.old_info)
        self.snapshot = mock("_snapshot", side_effect=lambda info, data, deadline: self.new if info == self.new_info else self.old)
        self.health = mock("_healthy", return_value=True)
        self.birth = self.stack.enter_context(patch.object(win.native, "windows_process_start", side_effect=self.identity))
        self.stack.enter_context(patch.object(win.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(win.time, "time_ns", return_value=0))
        self.sleep = self.stack.enter_context(patch.object(win.time, "sleep", side_effect=self.advance))

    def advance(self, value):
        self.clock += value

    def identity(self, pid):
        if pid in (10, 11):
            return self.old[pid - 10]["birth"] if not self.stopping or self.clock < self.exit_at else None
        return self.new[pid - 20]["birth"] if self.launched and not self.suppressed else None

    def control(self, request, **kwargs):
        action = request["action"]
        self.calls.append(action)
        self.assertEqual(request["sid"], IDENTITY["sid"])
        self.assertEqual(request["xml"], win.definition(DATA))
        if action == "stop":
            self.stopping = True
        if action == "launch":
            # IgnoreNew would suppress a premature Run despite transient Ready.
            self.assertTrue(all(self.birth(p["pid"]) != p["birth"] for p in self.old))
            self.launched = True
        return dict(present=action != "uninstall", state=3 if self.stopping and not self.launched else 4)

    def test_transient_ready_old_http_and_delayed_stop_require_real_exit(self):
        self.assertTrue(win._lifecycle("restart", DATA, 2)["present"])
        self.assertGreaterEqual(self.clock, self.exit_at)
        self.assertEqual(self.calls, ["status", "stop", "ready", "launch"])
        self.health.assert_called_once_with(self.new_info, 2)
        self.assertEqual(self.snapshot.call_count, 2)

    def test_old_runtime_timeout_never_runs_even_if_task_ready(self):
        self.exit_at = 10
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 0.5)
        self.assertNotIn("launch", self.calls)
        self.health.assert_not_called()
        self.assertEqual(self.snapshot.call_count, 1)

    def test_suppressed_single_run_cannot_accept_old_metadata_or_http(self):
        self.suppressed = True
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 0.8)
        self.assertEqual(self.calls.count("launch"), 1)
        self.health.assert_not_called()

    def test_reused_pid_is_not_waited_on_as_old_identity(self):
        original = self.identity
        self.birth.side_effect = lambda pid: f"win:{pid}:{STAMP - 1}" if pid in (10, 11) and self.stopping else original(pid)
        self.assertTrue(win._lifecycle("restart", DATA, 2)["present"])
        self.assertEqual(self.clock, 0)

    def test_inaccessible_old_runtime_wait_refuses_without_run(self):
        self.birth.side_effect = PermissionError("private native detail")
        with self.assertRaises(PermissionError):
            win._lifecycle("restart", DATA, 2)
        self.assertNotIn("launch", self.calls)

    def test_unverifiable_snapshot_refuses_before_stop(self):
        self.snapshot.side_effect = win.ServiceError("unverifiable")
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 2)
        self.assertEqual(self.calls, ["status"])

    def test_cancellation_during_exit_wait_never_restarts(self):
        self.sleep.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            win._lifecycle("restart", DATA, 2)
        self.assertEqual(self.calls, ["status", "stop"])

    def test_normal_stop_does_not_launch(self):
        self.assertTrue(win._lifecycle("stop", DATA, 2)["present"])
        self.assertNotIn("launch", self.calls)
        self.health.assert_not_called()

    def test_uninstall_waits_then_deletes_without_launch(self):
        self.assertFalse(win._lifecycle("uninstall", DATA, 2)["present"])
        self.assertEqual(self.calls, ["status", "stop", "ready", "uninstall"])
        self.health.assert_not_called()

    def test_idempotent_start_preserves_old_runtime(self):
        win._lifecycle("start", DATA, 2)
        self.assertEqual(self.calls, ["status"])
        self.health.assert_called_once_with(self.old_info, 2)

    def test_new_identity_exits_during_health_is_failure(self):
        self.health.side_effect = lambda *args: setattr(self, "launched", False) or True
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 2)
        self.assertEqual(self.calls.count("launch"), 1)

    def test_new_unhealthy_runtime_has_shared_deadline_and_no_retry(self):
        self.health.return_value = False
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 0.8)
        self.assertEqual(self.calls.count("launch"), 1)
        self.assertEqual(self.snapshot.call_count, 2)

    def test_stopped_start_launches_once_with_new_identity(self):
        self.stopping = True
        self.exit_at = 0
        self.snapshot.side_effect = lambda info, data, deadline: self.new if info == self.new_info else ()
        self.assertTrue(win._lifecycle("start", DATA, 2)["present"])
        self.assertEqual(self.calls, ["status", "launch"])
        self.health.assert_called_once_with(self.new_info, 2)

    def test_ready_must_be_rechecked_after_old_exit(self):
        control = self.control
        def changed(request, **kwargs):
            result = control(request, **kwargs)
            if request["action"] == "ready" and self.stopping:
                result["state"] = 4
            return result
        win._powershell.side_effect = changed
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 2)
        self.assertNotIn("launch", self.calls)

    def test_no_metadata_while_running_is_not_permission_to_stop(self):
        self.hint.side_effect = None
        self.hint.return_value = None
        self.snapshot.side_effect = None
        self.snapshot.return_value = ()
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 2)
        self.assertEqual(self.calls, ["status"])

    def test_old_service_shaped_reused_metadata_cannot_be_new_launch(self):
        self.suppressed = True
        self.birth.side_effect = lambda pid: f"win:{pid}:{STAMP - 1}"
        with self.assertRaises(win.ServiceError):
            win._lifecycle("restart", DATA, 0.8)
        self.health.assert_not_called()

    def test_partial_metadata_after_run_is_not_ready_then_new_proof_succeeds(self):
        self.hint.side_effect = [self.old_info, json.JSONDecodeError("partial", "", 0), self.new_info]
        self.assertTrue(win._lifecycle("restart", DATA, 2)["present"])
        self.assertEqual(self.calls.count("launch"), 1)
        self.health.assert_called_once_with(self.new_info, 2)


class RuntimeIdentityTests(unittest.TestCase):
    def process(self, *, rows=None, births=None, args=None):
        row = dict(pid=10, parent=11, sid=IDENTITY["sid"], image=DATA["python"], command="not logged")
        if rows:
            row.update(rows)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        birth = f"win:10:{STAMP}"
        native = stack.enter_context(patch.object(win.native, "windows_process_start", side_effect=births or [birth, birth]))
        query = stack.enter_context(patch.object(win, "_powershell", return_value=row))
        stack.enter_context(patch.object(win, "_argv", return_value=args or [DATA["python"], "-B", "-m", "docket.service_windows", "--launch", ntpath_join_manifest()]))
        stack.enter_context(patch.object(os.path, "samefile", return_value=True))
        return native, query

    def test_pid_scoped_query_birth_owner_and_fixed_arguments(self):
        native, query = self.process()
        self.assertEqual(win._process(10, DATA, 10)["birth"], f"win:10:{STAMP}")
        query.assert_called_once_with(dict(pid=10, sid=IDENTITY["sid"]), deadline=10, script=win.PROCESS_SCRIPT)
        self.assertEqual(native.call_count, 2)

    def test_absent_pid_does_not_query(self):
        _, query = self.process(births=[None])
        self.assertIsNone(win._process(10, DATA, 10))
        query.assert_not_called()

    def test_reuse_during_query_refused(self):
        self.process(births=[f"win:10:{STAMP}", f"win:10:{STAMP + 1}"])
        with self.assertRaises(win.ServiceError):
            win._process(10, DATA, 10)

    def test_foreign_owner_pid_image_and_arguments_refused(self):
        for change in (dict(sid="S-1-5-18"), dict(pid=12), dict(image=None)):
            with self.subTest(change=change):
                self.process(rows=change)
                with self.assertRaises(win.ServiceError):
                    win._process(10, DATA, 10)
        self.process(args=[DATA["python"], "-m", "docket", "daemon"])
        with self.assertRaises(win.ServiceError):
            win._process(10, DATA, 10)

    def test_direct_action_does_not_inspect_scheduler_parent(self):
        daemon = dict(pid=10, birth=f"win:10:{STAMP}", image=DATA["python"], parent=999)
        with patch.object(win, "_process", return_value=daemon) as query, \
                patch.object(os.path, "samefile", return_value=True), \
                patch.object(win.native, "windows_process_start", return_value=daemon["birth"]):
            self.assertEqual(win._snapshot(dict(pid=10), DATA, 10), (daemon,))
            query.assert_called_once_with(10, DATA, 10)

    def test_only_matching_older_venv_redirector_is_captured(self):
        base = r"C:\Base\python.exe"
        daemon = dict(pid=10, birth=f"win:10:{STAMP}", image=r"C:\Base\pythonw.exe", parent=11)
        parent = dict(pid=11, birth=f"win:11:{STAMP - 1}", image=DATA["python"], parent=999)
        def same(a, b):
            return str(a).lower() == str(b).lower()
        with patch.object(win, "_process", side_effect=[daemon, parent]) as query, \
                patch.object(os.path, "samefile", side_effect=same), \
                patch.object(sys, "executable", r"C:\Python311\python.exe"), \
                patch.object(sys, "_base_executable", base, create=True), \
                patch.object(win.native, "windows_process_start", side_effect=[daemon["birth"], parent["birth"]]):
            self.assertEqual(win._snapshot(dict(pid=10), DATA, 10), (daemon, parent))
            self.assertEqual([c.args[0] for c in query.call_args_list], [10, 11])
        for change in (dict(image=r"C:\Other\pythonw.exe"), dict(birth=f"win:11:{STAMP + 1}")):
            with self.subTest(change=change), patch.object(win, "_process", side_effect=[daemon, dict(parent, **change)]), \
                    patch.object(os.path, "samefile", side_effect=same), \
                    patch.object(sys, "executable", r"C:\Python311\python.exe"), \
                    patch.object(sys, "_base_executable", base, create=True), self.assertRaises(win.ServiceError):
                win._snapshot(dict(pid=10), DATA, 10)

    def test_provider_has_no_module_compiler_enumeration_or_termination(self):
        for text in ("Get-CimInstance", "Get-WmiObject", "Add-Type", "SELECT", "Terminate", "EnginePID", "GetInstances"):
            self.assertNotIn(text, win.PROCESS_SCRIPT)
        self.assertIn("Win32_Process.Handle=", win.PROCESS_SCRIPT)
        self.assertIn("GetOwnerSid", win.PROCESS_SCRIPT)
        self.assertLess(win.PROCESS_SCRIPT.index("$r.image -and"), win.PROCESS_SCRIPT.index("$p.GetOwnerSid()"))

    def test_unknown_base_image_does_not_query_parent(self):
        daemon = dict(pid=10, birth=f"win:10:{STAMP}", image=r"C:\Other\pythonw.exe", parent=999)
        with patch.object(win, "_process", return_value=daemon) as query, \
                patch.object(os.path, "samefile", side_effect=lambda a, b: str(a).lower() == str(b).lower()), \
                patch.object(sys, "executable", r"C:\Python311\python.exe"), \
                patch.object(sys, "_base_executable", r"C:\Base\python.exe", create=True), self.assertRaises(win.ServiceError):
            win._snapshot(dict(pid=10), DATA, 10)
        query.assert_called_once_with(10, DATA, 10)

    def test_snapshot_rechecks_birth_after_parent_query(self):
        daemon = dict(pid=10, birth=f"win:10:{STAMP}", image=DATA["python"], parent=999)
        with patch.object(win, "_process", return_value=daemon), \
                patch.object(os.path, "samefile", return_value=True), \
                patch.object(win.native, "windows_process_start", return_value=None), self.assertRaises(win.ServiceError):
            win._snapshot(dict(pid=10), DATA, 10)

    def test_argument_parser_calls_native_split_and_frees_result(self):
        import ctypes
        from ctypes import wintypes
        values = (wintypes.LPWSTR * 2)("pythonw.exe", "-B")
        def split(command, count):
            count._obj.value = 2
            return values
        shell, kernel = Mock(), Mock()
        shell.CommandLineToArgvW.side_effect = split
        with patch.object(ctypes, "WinDLL", side_effect=[shell, kernel], create=True):
            self.assertEqual(win._argv('pythonw.exe -B'), ["pythonw.exe", "-B"])
        kernel.LocalFree.assert_called_once_with(values)


class ReadinessBoundaryTests(unittest.TestCase):
    def test_evidenced_deployment_paths_fit_encoded_controller_ceiling(self):
        data = dict(DATA, root=r"C:\Users\synth\AppData\Local\Programs\Docket\builds\service-dddddddddddd\src",
                    python=r"C:\Users\synth\AppData\Local\Programs\Docket\builds\service-eeeeeeeeeeee\runtime\Scripts\pythonw.exe")
        request = dict(action="stop", runtime=True, sid=data["sid"], name=r"\Docket-synth", xml=win.definition(data))
        script = win.SCRIPT.replace("PAYLOAD", base64.b64encode(json.dumps(request, ensure_ascii=True).encode()).decode())
        args = [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode()]
        self.assertLess(len(subprocess.list2cmdline(args)), 32000)

    def test_hint_is_bounded_ordinary_metadata_and_local_only(self):
        for info in (dict(pid=1, url="http://127.0.0.1:8768"),
                     dict(pid=True, url="http://127.0.0.1:8768"),
                     dict(pid=1, url="http://127.0.0.1:8768@elsewhere"),
                     dict(pid=1, url="http://localhost:8768"),
                     dict(pid=1, url="http://127.0.0.1:0")):
            with self.subTest(info=info), patch.object(Path, "open", return_value=io.BytesIO(json.dumps(info).encode())) as opened, \
                    patch.object(win.native, "read_private_bytes") as private:
                if type(info["pid"]) is int and info["url"] == "http://127.0.0.1:8768":
                    self.assertEqual(win._hint(DATA), info)
                else:
                    with self.assertRaises(win.ServiceError):
                        win._hint(DATA)
                opened.assert_called_once_with("rb")
                private.assert_not_called()
        with patch.object(Path, "open", return_value=io.BytesIO(b" " * 4097)), self.assertRaises(win.ServiceError):
            win._hint(DATA)

    def test_probe_disables_proxy_redirects_and_bounds_time_bytes(self):
        sock = HealthSocket(health_wire())
        with patch.object(win.socket, "socket", return_value=sock) as factory, \
                patch.object(win.socket, "getaddrinfo", side_effect=AssertionError("DNS")), \
                patch.dict(os.environ, {"http_proxy": "http://elsewhere:80"}), \
                patch.object(win.time, "monotonic", side_effect=lambda: sock.clock):
            self.assertTrue(win._healthy(dict(url="http://127.0.0.1:8768"), 2))
        factory.assert_called_once_with(win.socket.AF_INET, win.socket.SOCK_STREAM)
        self.assertEqual(sock.address, ("127.0.0.1", 8768))
        self.assertEqual(sock.sent, b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:8768\r\nConnection: close\r\n\r\n")
        self.assertTrue(sock.closed)
        self.assertTrue(all(t <= 0.5 for t in sock.timeouts))

    def test_probe_rejects_oversize_non_object_and_unhealthy_responses(self):
        for body in (b'x' * 4097, b'[]', b'{"ok":false}'):
            sock = HealthSocket(health_wire(body))
            with self.subTest(size=len(body)), \
                    patch.object(win.socket, "socket", return_value=sock), \
                    patch.object(win.time, "monotonic", side_effect=lambda: sock.clock):
                self.assertFalse(win._healthy(dict(url="http://127.0.0.1:8768"), 2))
            self.assertTrue(sock.closed)

    def test_runner_uses_remaining_overall_budget(self):
        process = Mock(stdout=io.BytesIO(b'{}'), stderr=io.BytesIO(), returncode=0)
        with patch.object(win, "_powershell_exe", return_value=r"C:\Windows\powershell.exe"), \
                patch.object(win.native, "child_options", return_value={}), \
                patch.object(win.subprocess, "Popen", return_value=process) as popen, \
                patch.object(win.time, "monotonic", return_value=10):
            win._powershell(dict(pid=1, sid=IDENTITY["sid"]), deadline=11, script=win.PROCESS_SCRIPT)
            process.wait.assert_called_once_with(timeout=1)
            with self.assertRaises(win.ServiceError):
                win._powershell(dict(action="status"), deadline=10)
            self.assertEqual(popen.call_count, 1)

    def test_runner_cancellation_reaps_only_its_controller(self):
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=0)
        process.wait.side_effect = [KeyboardInterrupt, 0]
        with patch.object(win, "_powershell_exe", return_value=r"C:\Windows\powershell.exe"), \
                patch.object(win.native, "child_options", return_value={}), \
                patch.object(win.subprocess, "Popen", return_value=process) as popen, self.assertRaises(KeyboardInterrupt):
            win._powershell(dict(action="status"))
        process.kill.assert_called_once_with()
        self.assertEqual(popen.call_count, 1)

    def test_safe_diagnostics_do_not_include_identity_query_details(self):
        output = io.StringIO()
        with patch.object(sys, "platform", "win32"), \
                patch.object(win, "_identity", return_value=IDENTITY), \
                patch.object(win.native, "state_root", return_value=DATA["home"]), \
                patch.object(win.native, "read_private_bytes", return_value=json.dumps(DATA).encode()), \
                patch.object(win, "_validate", return_value=DATA), \
                patch.object(win, "_lifecycle", side_effect=win.ServiceError("RAW-SENSITIVE-COMMAND")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(win.run("restart"), 1)
        self.assertNotIn("RAW-SENSITIVE-COMMAND", output.getvalue())
        self.assertIn("refused or unavailable", output.getvalue())


def health_wire(body=b'{"ok": true, "version": "test", "pending": 0, "outstanding": 0, "host": {}}'):
    # api.Handler protocol_version/_send + BaseHTTPRequestHandler.send_response.
    return (b"HTTP/1.1 200 OK\r\nServer: BaseHTTP/0.6 Python/3.13\r\n"
            b"Date: Tue, 20 Oct 2026 00:00:00 GMT\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


class HealthSocket:
    """Raw socket boundary only: virtual time, never creates a socket/parser."""
    def __init__(self, wire, *, delay=0, slow_from=0, write_size=1000, phase=None):
        self.wire = wire
        self.clock = 0.0
        self.delay = delay
        self.slow_from = slow_from
        self.write_size = write_size
        self.phase = phase
        self.pos = 0
        self.sent = b""
        self.timeouts = []
        self.closed = False
        self.address = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, value):
        self.timeouts.append(value)

    def wait(self, phase, delay):
        if self.phase == phase:
            raise ConnectionRefusedError("private socket detail")
        if delay > self.timeouts[-1]:
            self.clock += self.timeouts[-1]
            raise TimeoutError
        self.clock += delay

    def connect(self, address):
        self.address = address
        self.wait("connect", self.delay if self.phase == "slow-connect" else 0)

    def send(self, data):
        self.wait("send", self.delay if self.phase == "slow-send" else 0)
        sent = min(self.write_size, len(data))
        self.sent += data[:sent]
        return sent

    def recv(self, size):
        self.wait("recv", self.delay if self.pos >= self.slow_from else 0)
        chunk = self.wire[self.pos:self.pos + min(size, 1)]
        self.pos += len(chunk)
        return chunk


class AbsoluteHealthTests(unittest.TestCase):
    def probe(self, sock, deadline=1, url="http://127.0.0.1:8768"):
        with patch.object(win.socket, "socket", return_value=sock), \
                patch.object(win.socket, "getaddrinfo", side_effect=AssertionError("DNS")), \
                patch.object(win.time, "monotonic", side_effect=lambda: sock.clock):
            result = win._healthy(dict(url=url), deadline)
        self.assertTrue(sock.closed)
        self.assertLessEqual(sock.clock, deadline + 1e-12)
        return result

    def test_trickled_status_and_headers_share_absolute_deadline(self):
        for start in (0, len(b"HTTP/1.1 200 OK\r\n")):
            sock = HealthSocket(health_wire(), delay=0.1, slow_from=start)
            self.assertFalse(self.probe(sock))
            self.assertLess(sock.pos, len(sock.wire))

    def test_trickled_body_shares_absolute_deadline(self):
        wire = health_wire()
        sock = HealthSocket(wire, delay=0.1, slow_from=wire.index(b"\r\n\r\n") + 4)
        self.assertFalse(self.probe(sock))
        self.assertLess(sock.pos, len(wire))

    def test_chunk_extensions_are_refused_without_consuming_framing(self):
        for body in (b'a;' + b'x' * 5000, b'a\r\n{"ok":true}\r\n0\r\n' + b'x' * 5000):
            header = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            sock = HealthSocket(header + body, delay=0.1, slow_from=len(header))
            self.assertFalse(self.probe(sock))
            self.assertEqual(sock.pos, len(header))

    def test_partial_request_writes_and_endpoint_shaped_response(self):
        sock = HealthSocket(health_wire(), write_size=1)
        self.assertTrue(self.probe(sock))
        self.assertTrue(sock.sent.endswith(b"Connection: close\r\n\r\n"))
        self.assertGreater(len(sock.timeouts), len(sock.sent))

    def test_connect_and_partial_writes_obey_remaining_budget(self):
        for phase in ("slow-connect", "slow-send"):
            sock = HealthSocket(health_wire(), delay=0.4, write_size=1, phase=phase)
            self.assertFalse(self.probe(sock, deadline=0.7))
            self.assertAlmostEqual(sock.clock, 0.7)
            self.assertAlmostEqual(sock.timeouts[-1], 0.3)

    def test_refused_connect_send_recv_and_zero_write_close(self):
        for phase in ("connect", "send", "recv"):
            self.assertFalse(self.probe(HealthSocket(health_wire(), phase=phase)))
        self.assertFalse(self.probe(HealthSocket(health_wire(), write_size=0)))

    def test_exact_expiry_before_success_and_after_json(self):
        sock = HealthSocket(health_wire(b'{"ok":true}'), delay=0.1)
        self.assertFalse(self.probe(sock, deadline=0.1))
        sock = HealthSocket(health_wire())
        real_loads = json.loads
        def parse(raw):
            result = real_loads(raw)
            sock.clock = 1
            return result
        with patch.object(win.json, "loads", side_effect=parse):
            self.assertFalse(self.probe(sock))

    def test_expired_budget_and_invalid_destinations_never_create_socket(self):
        with patch.object(win.socket, "socket", side_effect=AssertionError("socket")), \
                patch.object(win.time, "monotonic", return_value=1):
            self.assertFalse(win._healthy(dict(url="http://127.0.0.1:8768"), 1))
            for url in ("http://localhost:8768", "https://127.0.0.1:8768", "http://127.0.0.1:0",
                        "http://127.0.0.1:65536", "http://127.0.0.1:80@elsewhere", "http://127.0.0.1:80/health"):
                self.assertFalse(win._healthy(dict(url=url), 2))

    def test_header_and_body_caps_and_exact_body_limit(self):
        for wire in (b"HTTP/1.1 200 OK\r\nServer: " + b"x" * 3000,
                     health_wire(b"x" * 4097)):
            sock = HealthSocket(wire)
            self.assertFalse(self.probe(sock))
            self.assertLessEqual(sock.pos, 2048)
        self.assertTrue(self.probe(HealthSocket(health_wire(b'{"ok":true}' + b' ' * 4085))))

    def test_malformed_unknown_redirected_and_mismatched_responses_fail_closed(self):
        good = health_wire(b'{"ok":true}')
        wires = [b"", good[:-1], good + b"x", good.replace(b"200 OK", b"302 Found"),
                 good.replace(b"200 OK", b"500 Error"), good.replace(b"HTTP/1.1", b"HTTP/1.0"),
                 good.replace(b"Content-Length: 11", b"Content-Length: 0"),
                 good.replace(b"Content-Length: 11", b"Content-Length: +11"),
                 good.replace(b"Content-Length: 11", b"Content-Length: 011"),
                 good.replace(b"Content-Length: 11\r\n", b""),
                 good.replace(b"Content-Length: 11", b"Content-Length: 11\r\nContent-Length: 11"),
                 good.replace(b"application/json", b"text/html"),
                 good.replace(b"Server: ", b" Server: "),
                 good.replace(b"Server: ", b"Server:\t"),
                 good.replace(b"Server: ", b"Server: \x00"),
                 good.replace(b"Server: ", b"X-Unknown: "),
                 good.replace(b"Server: ", b"Location: "),
                 health_wire(b'{"ok":1}'), health_wire(b'{broken'), health_wire(b'\xff')]
        for wire in wires:
            with self.subTest(wire=wire[:80]):
                self.assertFalse(self.probe(HealthSocket(wire)))

    def test_cancellation_closes_socket_without_background_worker(self):
        sock = HealthSocket(health_wire())
        sock.recv = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.probe(sock)
        self.assertTrue(sock.closed)


class IdempotentBirthBoundaryTests(unittest.TestCase):
    def test_captured_daemon_and_redirector_checked_before_and_after_health(self):
        for redirector in (False, True):
            f = RestartSequencingTests()
            f.setUp()
            try:
                if not redirector:
                    f.old = f.old[:1]
                win._lifecycle("start", DATA, 2)
                self.assertEqual([c.args[0] for c in f.birth.call_args_list],
                                 [p["pid"] for p in f.old] * 2)
                self.assertEqual(f.calls, ["status"])
            finally:
                f.doCleanups()

    def test_missing_reused_unavailable_birth_before_or_during_health_refuses(self):
        for pid in (10, 11):
            for when in ("before", "during"):
                for outcome in (None, "reused", PermissionError("PRIVATE")):
                    with self.subTest(pid=pid, when=when, outcome=type(outcome).__name__):
                        f = RestartSequencingTests()
                        f.setUp()
                        try:
                            changed = [when == "before"]
                            def birth(p):
                                if p == pid and changed[0]:
                                    if isinstance(outcome, Exception):
                                        raise outcome
                                    return outcome
                                return f.identity(p)
                            def health(*args):
                                changed[0] = True
                                return True
                            f.birth.side_effect = birth
                            f.health.side_effect = health
                            with self.assertRaisesRegex(win.ServiceError, "^existing runtime unavailable$"):
                                win._lifecycle("start", DATA, 2)
                            self.assertEqual(f.calls, ["status"])
                            if when == "before":
                                f.health.assert_not_called()
                            else:
                                f.health.assert_called_once()
                        finally:
                            f.doCleanups()


class NewRuntimeDeadlineTests(unittest.TestCase):
    def check_final_birth_deadline(self, action, deadline, finish):
        healthy = win._healthy
        f = RestartSequencingTests()
        try:
            f.setUp()
            if action == "start":
                f.stopping = True
                f.exit_at = 0
                f.snapshot.side_effect = lambda info, data, deadline: f.new if info == f.new_info else ()
            sock = HealthSocket(health_wire())
            f.stack.enter_context(patch.object(win.socket, "socket", return_value=sock))
            complete = False
            final_reads = []

            def health(*args):
                nonlocal complete
                f.clock = deadline - 0.2
                result = healthy(*args)
                self.assertTrue(result)
                self.assertLess(f.clock, deadline)
                self.assertTrue(sock.closed)
                complete = True
                return result

            def birth(pid):
                value = f.identity(pid)
                if complete:
                    final_reads.append(dict(pid=pid, birth=value))
                    if len(final_reads) == len(f.new):
                        f.clock = finish
                return value

            f.health.side_effect = health
            f.birth.side_effect = birth
            if finish >= deadline:
                with self.assertRaises(win.ServiceError):
                    win._lifecycle(action, DATA, deadline)
            else:
                self.assertEqual(win._lifecycle(action, DATA, deadline), dict(present=True, state=4))
            self.assertEqual(final_reads, list(f.new))
            self.assertEqual(f.clock, finish)
            self.assertEqual(f.calls, ["status", "launch"] if action == "start"
                             else ["status", "stop", "ready", "launch"])
            f.health.assert_called_once_with(f.new_info, deadline)
            self.assertTrue(sock.closed)
        finally:
            f.doCleanups()

    def test_final_matching_births_at_or_after_deadline_refuse(self):
        for action in ("start", "restart"):
            for deadline in (2, 30):
                for overrun in (0, 0.1):
                    with self.subTest(action=action, deadline=deadline, overrun=overrun):
                        self.check_final_birth_deadline(action, deadline, deadline + overrun)

    def test_final_matching_births_before_deadline_succeed(self):
        for action in ("start", "restart"):
            for deadline in (2, 30):
                with self.subTest(action=action, deadline=deadline):
                    self.check_final_birth_deadline(action, deadline, deadline - 0.1)


def ntpath_join_manifest():
    return DATA["home"] + "\\" + win.MANIFEST


if __name__ == "__main__":
    unittest.main()
