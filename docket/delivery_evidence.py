"""Bounded, read-only content evidence; never a clock or a progress decision.

Compare successive successful snapshots only. The first observation (including
pre-existing dirt) is a baseline. Text whitespace is deliberately ignored, so
whitespace-significant edits may be missed. Binary and explicit artifacts are
hashed byte-for-byte. Git ignored untracked files, generated/private/log paths,
empty commits and whitespace-only commits are outside the source scope.
Non-Git directories require operator-configured absolute artifact *files*.
No recursive artifact discovery, submodules, symlinks or partial success.
Unborn repositories and histories exceeding the meaningful-commit search bound
are unavailable, not evidence of a stall.
"""

import hashlib
import os
from pathlib import Path
import selectors
import stat
import subprocess
import time


class EvidenceUnavailable(Exception):
    """Observation was unsafe, incomplete, changing or over budget."""


MAX_FILES = 2048
MAX_BYTES = 16 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_SECONDS = 10
MAX_HISTORY = 64
MAX_WORKTREES = 8
_DIRS = (".git", ".opencode", "lead", "private", "evidence", "__pycache__",
         ".cache", ".pytest_cache", ".mypy_cache", ".venv", "venv",
         "node_modules", "build", "dist", "wt")
_FILES = ("LEAD.md", "*.log", "*.pyc", "*.pyo", "activity*", ".env*",
          "*.pem", "*.key")


def _excluded(name):
    import fnmatch
    parts = Path(name).parts
    return any(p in _DIRS for p in parts) or any(
        fnmatch.fnmatchcase(p, pattern) for p in parts for pattern in _FILES)


class _Reader:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.bytes = 0
        self.files = 0
        self.output = 0
        self.roots = ()

    def count_files(self, count):
        self.files += count
        if self.files > MAX_FILES:
            raise EvidenceUnavailable("file enumeration bound exceeded")

    def check(self):
        if time.monotonic() >= self.deadline:
            raise EvidenceUnavailable("observation time bound exceeded")

    def git(self, root, *args):
        self.check()
        # No shell, pager, optional locks, hooks, external diff or text conversion.
        env = {"PATH": os.defpath, "HOME": "/nonexistent", "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
        cmd = ["git", "--no-pager", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=/dev/null", "-C", str(root), *args]
        with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            data = bytearray()
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        self.check()
                        if not selector.select(max(0, self.deadline - time.monotonic())):
                            self.check()
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                        self.output += len(chunk)
                        if self.output > MAX_OUTPUT:
                            raise EvidenceUnavailable("git output bound exceeded")
                proc.wait(timeout=max(.001, self.deadline - time.monotonic()))
                if proc.returncode:
                    raise EvidenceUnavailable("git observation unavailable")
                return bytes(data)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def file(self, path, text=False):
        self.check()
        # Walk from / using directory fds: even a substituted ancestor cannot
        # redirect a read through a symlink outside the selected source root.
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise EvidenceUnavailable("absolute non-escaping file required")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
                os.close(fd)
                fd = new
            leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=fd)
            try:
                before = os.fstat(leaf)
                if not stat.S_ISREG(before.st_mode):
                    raise EvidenceUnavailable("non-regular file unsupported")
                chunks = []
                while True:
                    self.check()
                    chunk = os.read(leaf, 65536)
                    if not chunk:
                        break
                    self.bytes += len(chunk)
                    if self.bytes > MAX_BYTES:
                        raise EvidenceUnavailable("content byte bound exceeded")
                    chunks.append(chunk)
                after = os.fstat(leaf)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise EvidenceUnavailable("content changed during observation")
                data = b"".join(chunks)
                if text and b"\0" not in data:
                    data = b"".join(data.split())
                return hashlib.sha256(data).digest()
            finally:
                os.close(leaf)
        finally:
            os.close(fd)

    def source(self, root):
        raw = self.git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        names = sorted(set(os.fsdecode(p) for p in raw.split(b"\0") if p))
        self.count_files(len(names))
        result = []
        for name in names:
            if _excluded(name):
                continue
            p = Path(name)
            if p.is_absolute() or ".." in p.parts:
                raise EvidenceUnavailable("unsafe source path")
            if any(tree != root and tree.is_relative_to(root)
                   and (root / p).is_relative_to(tree)
                   for tree in self.roots):
                continue
            # A missing tracked file is an observed deletion, not a read error.
            try:
                digest = self.file(root / p, text=True)
            except FileNotFoundError:
                if (root / p).exists():
                    raise
                digest = b"deleted"
            result.append((os.fsencode(name), digest))
        return result

    def commit(self, root):
        exclusions = [":(glob,exclude)**/" + p + "/**" for p in _DIRS]
        exclusions += [":(glob,exclude)**/" + p for p in _FILES]
        raw = self.git(root, "log", "-" + str(MAX_HISTORY + 1),
                       "--format=COMMIT:%H", "--numstat", "-w",
                       "--ignore-blank-lines", "--no-renames", "--no-ext-diff",
                       "--no-textconv", "--", ".", *exclusions)
        commits = 0
        current = b""
        for line in raw.splitlines():
            if line.startswith(b"COMMIT:"):
                commits += 1
                current = line[7:]
            elif line.strip():
                if commits > MAX_HISTORY:
                    break
                return current
        if commits > MAX_HISTORY:
            raise EvidenceUnavailable("meaningful commit outside history bound")
        return b"no-meaningful-commit"

    def worktrees(self, root):
        """Only registered, same-repository, physically descendant roots."""
        common = self.git(root, "rev-parse", "--path-format=absolute",
                          "--git-common-dir").removesuffix(b"\n")
        common = Path(os.fsdecode(common)).resolve(strict=True)
        raw = self.git(root, "worktree", "list", "--porcelain", "-z")
        roots = {root}
        for record in raw.split(b"\0\0"):
            self.check()
            if not record:
                continue
            fields = record.split(b"\0")
            if not fields[0].startswith(b"worktree "):
                raise EvidenceUnavailable("unrecognized worktree enumeration")
            candidate = Path(os.fsdecode(fields[0][9:]))
            if candidate == root:
                continue
            if not candidate.is_absolute() or ".." in candidate.parts:
                raise EvidenceUnavailable("unsafe registered worktree path")
            if not candidate.is_relative_to(root):
                continue
            # No arbitrary walk, and no following symlinks in registered roots.
            if not _safe_root(candidate) or (candidate / ".git").is_symlink():
                continue
            other = self.git(candidate, "rev-parse", "--path-format=absolute",
                             "--git-common-dir").removesuffix(b"\n")
            if Path(os.fsdecode(other)).resolve(strict=True) != common:
                continue
            roots.add(candidate)
            if len(roots) > MAX_WORKTREES + 1:
                raise EvidenceUnavailable("registered worktree bound exceeded")
        return sorted(roots)


def _safe_root(root):
    return all(not p.is_symlink() and p.is_dir() for p in [root, *root.parents])


def snapshot(directory, artifact_paths=()):
    """Return opaque fingerprint/reference/scope/limitations, or EvidenceUnavailable.

    Budgets cover both consistency passes together. References contain no paths,
    filenames, commit messages or content. This is not an atomic filesystem
    snapshot; two matching bounded passes detect ordinary concurrent mutation.
    """
    try:
        reader = _Reader()
        root = Path(directory)
        if not root.is_absolute() or ".." in root.parts:
            raise EvidenceUnavailable("absolute project directory required")
        # Validate every root component without traversing symbolic links.
        if not _safe_root(root):
            raise EvidenceUnavailable("unsafe project directory")
        artifacts = []
        for path in artifact_paths:
            reader.check()
            if len(artifacts) >= MAX_FILES:
                raise EvidenceUnavailable("artifact enumeration bound exceeded")
            p = Path(path)
            if not p.is_absolute() or ".." in p.parts:
                raise EvidenceUnavailable("absolute artifact file required")
            artifacts.append(p)
        artifacts = sorted(set(artifacts))
        git = os.path.lexists(root / ".git")
        if git:
            if (root / ".git").is_symlink():
                raise EvidenceUnavailable("unsafe Git metadata path")
            top = os.fsdecode(reader.git(root, "rev-parse", "--show-toplevel").removesuffix(b"\n"))
            if top != str(root):
                raise EvidenceUnavailable("project must be repository root")
        elif not artifacts:
            raise EvidenceUnavailable("non-Git source unsupported; configure artifacts")

        def observe():
            h = hashlib.sha256(b"docket-evidence-v1")
            roots = reader.worktrees(root) if git else []
            reader.roots = roots
            scope = hashlib.sha256(b"docket-evidence-scope-v1")
            for tag, paths in ((b"worktree", roots), (b"artifact", artifacts)):
                for path in paths:
                    name = os.fsencode(path)
                    scope.update(tag + len(name).to_bytes(8, "big") + name)
            states = set()
            for tree in roots:
                state = hashlib.sha256(b"source-state-v1")
                for name, digest in reader.source(tree):
                    state.update(len(name).to_bytes(8, "big") + name + digest)
                state.update(reader.commit(tree))
                states.add(state.digest())
            # Root names/counts are not delivery. Identical checkouts deduplicate.
            for state in sorted(states):
                h.update(state)
            reader.count_files(len(artifacts))
            for p in artifacts:
                name, digest = os.fsencode(p), reader.file(p)
                h.update(len(name).to_bytes(8, "big") + name + digest)
            reader.check()
            return h.hexdigest(), tuple(roots), scope.hexdigest()

        first, topology, scope = observe()
        if observe() != (first, topology, scope):
            raise EvidenceUnavailable("observation changed between passes")
        return {"fingerprint": first, "reference": "content-sha256:" + first,
                "scope": scope,
                "limitations": "Two-pass bounded observation, not atomic. "
                "Text whitespace ignored; ignored/generated/private/log paths excluded. "
                "Artifacts are exact bytes. First snapshot is baseline, not progress. "
                "Callers must rebaseline on scope change; topology/config change is not delivery. "
                + ("Git root plus up to 8 registered same-repo descendant worktrees; "
                   "outside/unsafe roots excluded; no submodules. Identical states deduplicated. "
                   "Adding/removing a distinct pre-existing worktree state can change the "
                   "fingerprint without new delivery; topology must be stable when comparing."
                   if git else "Artifact-only; non-Git source not observed.")}
    except EvidenceUnavailable:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise EvidenceUnavailable("observation unavailable or unsafe") from None
