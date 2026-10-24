"""Minute-sampled delivery policy, without a wake transport or scanner."""
import hashlib
import json
import os
import stat
import time
import uuid
from datetime import datetime
from pathlib import Path


def artifacts(row):
    paths = row.get("delivery_artifacts", [])
    if (not isinstance(paths, list) or len(paths) > 16
            or any(not isinstance(p, str) or not p.startswith("/")
                   or len(p) > 4096 or "\x00" in p for p in paths)):
        raise ValueError("invalid delivery artifacts")
    return tuple(paths)


def evidence(directory, artifact_paths=()):
    from docket.delivery_evidence import snapshot
    return snapshot(directory, artifact_paths=artifact_paths)


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute canonical directory required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        view = os.fstat(fd)
        if view.st_uid != os.getuid() or view.st_mode & 0o022:
            raise ValueError("owner-controlled directory required")
        return fd
    except Exception:
        os.close(fd)
        raise


def resolve(pointer=None):
    from docket.parse import validate_session_id
    pointer = pointer or Path.home() / ".chief-of-staff" / "chief.json"
    parent = _directory(pointer.parent)
    try:
        fd = os.open(pointer.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd) as stream:
            view = os.fstat(stream.fileno())
            if (not stat.S_ISREG(view.st_mode) or view.st_uid != os.getuid()
                    or view.st_mode & 0o022 or view.st_size > 16384):
                raise ValueError("invalid chief pointer")
            chief = json.loads(stream.read(16385))
        if not isinstance(chief.get("session"), str) or validate_session_id(chief["session"]):
            raise ValueError("invalid chief session")
        directory = _directory(chief["directory"])
        try:
            try:
                os.mkdir("inbox", 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            inbox = os.open("inbox", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            view = os.fstat(inbox)
            if view.st_uid != os.getuid() or view.st_mode & 0o022:
                raise ValueError("invalid chief inbox")
            return dict(session=chief["session"], directory=chief["directory"],
                        device=view.st_dev, inode=view.st_ino)
        finally:
            os.close(inbox)
    finally:
        os.close(parent)


def publish(key, text, pointer=None, *, destination=None):
    destination = destination or resolve(pointer)
    inbox = _directory(Path(destination["directory"]) / "inbox")
    try:
        view = os.fstat(inbox)
        if (view.st_dev, view.st_ino) != (destination["device"], destination["inode"]):
            raise ValueError("reserved inbox replaced")
        name, temp = "delivery-stall-" + key + ".md", "." + uuid.uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=inbox)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, name, src_dir_fd=inbox, dst_dir_fd=inbox, follow_symlinks=False)
            except FileExistsError:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox)
                with os.fdopen(fd) as stream:
                    view = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(view.st_mode) or view.st_uid != os.getuid()
                            or view.st_mode & 0o077 or view.st_size > 32768
                            or stream.read(32769) != text):
                        raise ValueError("unsafe or conflicting inbox collision")
            os.fsync(inbox)
        finally:
            os.unlink(temp, dir_fd=inbox)
    finally:
        os.close(inbox)


class DeliveryStall:
    def __init__(self, manager, reader=evidence, writer=publish, resolver=resolve):
        self.manager, self.reader, self.writer = manager, reader, writer
        self.resolver = resolver
        self.error = None
        self.samples = {}  # continuity deliberately lost on restart
        self.question_samples = {}
        self.pending = {}
        self.retired = set()
        self.generations = {}
        self.prefix = "delivery-stall:" + uuid.uuid4().hex

    def invalidate(self, sid):
        self.samples.pop(sid, None)
        pending = self.pending.pop(sid, None)
        if pending:
            self.retired.add(pending[0])
            self.generations[pending[1]] = self.generations.get(pending[1], 0) + 1

    def observation(self, sid, scope, directory, paths, now):
        executor = self.manager.scheduler.check_executor
        pending = self.pending.get(sid)
        if pending and pending[1] != scope:
            self.invalidate(sid)
            pending = None
        def enqueue():
            key = f"{self.prefix}:{scope}:{self.generations.get(scope, 0)}"
            reader = self.reader
            def probe():
                start = time.monotonic()
                try:
                    return reader(directory, artifact_paths=paths), time.monotonic() - start
                except Exception:
                    return None, time.monotonic() - start
            if not executor.submit(key, probe):
                self.error = "delivery-stall evidence backlog full"
                self.samples.pop(sid, None)
                return None
            self.pending[sid] = (key, scope, now)
            return self.pending[sid]
        if pending is None:
            pending = enqueue()
            if pending is None:
                return None
        result = executor.take(pending[0])
        if result is None:
            return None
        self.pending.pop(sid, None)
        payload, duration = result
        if payload is None or duration > 120 or not 0 <= (now-pending[2]).total_seconds() <= 120:
            raise ValueError("failed or stale evidence observation")
        # Pipeline the next frozen observation now; waiting another tick would
        # make async samples two cadences apart and ordinary drift break continuity.
        enqueue()
        return payload

    def tick(self, now):
        from docket.checkins import roster
        m = self.manager
        self.error = None
        for key in tuple(self.retired):
            try:
                if m.scheduler.check_executor.take(key) is not None:
                    self.retired.remove(key)
            except Exception:
                self.retired.discard(key)
        try:
            rows = roster(m.roster_path)
        except Exception:
            self.question_samples.clear()
            for sid in set(self.samples) | set(self.pending):
                self.invalidate(sid)
            if m.roster_path.exists():
                self.error = "delivery-stall roster unavailable"
            return
        states = m.state.setdefault("delivery_stalls", {})
        self.question_tick(rows, now)
        if any(s.get("publication") and not s.get("alerted") for s in states.values()):
            self.error = "delivery-stall publication unresolved; frozen inbox reconciliation pending"
        for sid in (set(self.samples) | set(self.pending)) - set(rows):
            self.invalidate(sid)
        for sid, row in rows.items():
            try:
                if row["state"] != "active":
                    self.invalidate(sid)
                    continue
                adapter = m.scheduler.adapter
                goal = adapter.session_goal(sid)
                identity = m.goal_identity(goal) if isinstance(goal, dict) else None
                if (identity is None or goal.get("status") != "active"
                        or adapter.session_state(sid) != "active"
                        or not adapter.validate({"session_id": sid, "directory": row["directory"]}).ok):
                    self.invalidate(sid)
                    continue
                paths = artifacts(row)
                scope = json.dumps([sid, row["directory"], identity], sort_keys=True)
                scope_key = hashlib.sha256(scope.encode()).hexdigest()
                probe_scope = hashlib.sha256((scope + json.dumps(paths)).encode()).hexdigest()
                result = self.observation(sid, probe_scope, row["directory"], paths, now)
                if result is None:
                    continue
                if any(not isinstance(result.get(k), str) or not result[k] or len(result[k]) > 8192
                       for k in ("fingerprint", "reference", "limitations")):
                    raise ValueError("unsupported evidence")
                state = states.setdefault(scope_key, {})
                evidence_scope = result.get("scope")
                if evidence_scope is not None and (not isinstance(evidence_scope, str)
                                                   or len(evidence_scope) != 64
                                                   or any(c not in "0123456789abcdef" for c in evidence_scope)):
                    raise ValueError("invalid evidence scope")
                if state and (state.get("paths") != list(paths)
                              or state.get("evidence_scope") != evidence_scope):
                    # Configuration is not delivery. Rebaseline without rearming.
                    state.update(paths=list(paths), fingerprint=result["fingerprint"],
                                 reference=result["reference"], observed=now.isoformat(),
                                 evidence_scope=evidence_scope)
                    self.samples.pop(sid, None)
                if state.get("fingerprint") != result["fingerprint"]:
                    state.pop("publication", None)
                    state.update(fingerprint=result["fingerprint"], epoch=state.get("epoch", 0) + 1,
                                 observed=now.isoformat(), reference=result["reference"],
                                 alerted=False, paths=list(paths), evidence_scope=evidence_scope)
                    self.samples.pop(sid, None)
                prior = self.samples.get(sid)
                since = prior[2] if prior and prior[0] == scope_key and 0 <= (now-prior[1]).total_seconds() <= 120 else now
                self.samples[sid] = (scope_key, now, since)
                if state["alerted"]:
                    continue
                if state.get("publication"):
                    pending = state["publication"]
                    m.save()
                    self._note_tower(pending.get("tower"))
                    self.writer(pending["key"], pending["text"], destination=pending["destination"])
                    state["alerted"] = True
                    m.save()
                    continue
                if (now-since).total_seconds() < 1800:
                    continue
                key = hashlib.sha256(f"{scope_key}:{state['epoch']}".encode()).hexdigest()
                text = ("# RUNNING without observed delivery\n\n"
                        f"Detected: {now.isoformat()}\nRunning interval: {since.isoformat()} to {now.isoformat()}\n"
                        f"Last observed delivery evidence: {state['observed']} (baseline/change observation, not delivery timestamp)\n"
                        f"Original session: {sid}\nAbsolute directory: {row['directory']}\nGoal: {identity!r}\n"
                        f"Usable: {state['reference']} observed {state['observed']}; no new delivery observed since this baseline.\n"
                        "Remaining: Chief review of this RUNNING lead's no-delivery interval; actual project scope/completion not inferred.\n"
                        f"Limits: {result['limitations']}; minute samples; no trusted install receipts. "
                        "Content-level project observations cannot attribute work to one writer or establish acceptance; "
                        "configured artifact content is not proof of installation success.\n")
                # Resolve before reserving; retries use only this frozen inbox identity.
                # Inbox text still uses the running interval above. Tower minutes do not.
                destination = self.resolver()
                state["publication"] = dict(key=key, text=text, destination=destination)
                payload = None
                try:
                    from docket.tower_publish import enabled, stall_episode
                    if enabled():
                        # Persisted evidence time, not the running streak. Idle gaps
                        # reset ``since`` and must not shrink this notice.
                        observed = datetime.fromisoformat(state["observed"])
                        payload = stall_episode(
                            sid, row["directory"],
                            (now - observed).total_seconds() / 60.0, now, key,
                        )
                except Exception:
                    payload = None
                if payload:
                    state["publication"]["tower"] = payload
                m.save()
                self._note_tower(payload)
                self.writer(key, text, destination=destination)
                state["alerted"] = True
                m.save()
            except Exception:
                self.invalidate(sid)
                self.error = "delivery-stall observation/publication unavailable; reserved effects may be uncertain"
        m.save()

    def question_tick(self, rows, now):
        m = self.manager
        source = getattr(m, "questions", None)
        if source is None or not source.enabled:
            self.question_samples.clear()
            return
        states = m.state.setdefault("question_stalls", {})
        if any(s.get("publication") and not s.get("alerted") for s in states.values()):
            self.error = "delivery-stall question publication unresolved; frozen inbox reconciliation pending"
        for sid in set(self.question_samples) - set(rows):
            self.question_samples.pop(sid, None)
            source.forget(sid)
        for sid, row in rows.items():
            try:
                snap = m.snapshot(sid)
                if not snap or snap[2] != "idle":
                    self.question_samples.pop(sid, None)
                    if not snap:
                        source.forget(sid)
                    continue
                row, goal, _ = snap
                questions = source.get(sid, row["directory"], goal, now)
                if questions is None or len(questions) != 1:
                    self.question_samples.pop(sid, None)
                    continue
                scope = json.dumps([sid, row["directory"], goal, "idle_question", questions[0]], sort_keys=True)
                key = hashlib.sha256(scope.encode()).hexdigest()
                prior = self.question_samples.get(sid)
                since = prior[2] if prior and prior[0] == key and 0 <= (now-prior[1]).total_seconds() <= 120 else now
                self.question_samples[sid] = (key, now, since)
                state = states.get(key, {})
                if state.get("alerted"):
                    continue
                if not state.get("publication") and (now-since).total_seconds() < 1800:
                    continue
                # Separate reads, not an atomic snapshot. Recheck eligibility just
                # before reserving/retrying any effect; unknown always suppresses.
                fresh = m.snapshot(sid)
                if (not fresh or fresh[2] != "idle" or fresh[1] != goal
                        or fresh[0]["directory"] != row["directory"]
                        or source.get(sid, row["directory"], goal, now) != questions):
                    self.question_samples.pop(sid, None)
                    continue
                if not state.get("publication"):
                    text = ("# Observed idle behind one recorded unresolved question\n\n"
                            f"Detected: {now.isoformat()}\nIdle interval: {since.isoformat()} to {now.isoformat()}\n"
                            f"Original session: {sid}\nAbsolute directory: {row['directory']}\nGoal: {goal!r}\n"
                            "Usable: complete fresh bound question-status observations; one unchanged recorded unresolved question.\n"
                            "Remaining: Chief review; no automatic resume.\n"
                            "Limits: minute samples; recorded questions only; not certified inactivity or fault; "
                            "separate native/source reads are not atomic.\n")
                    destination = self.resolver()
                    publication = dict(key=key, text=text, destination=destination)
                    from docket.tower_publish import enabled, stall_episode
                    if enabled():
                        payload = stall_episode(sid, row["directory"], (now-since).total_seconds()/60,
                                                now, key, reason="idle_question")
                        if payload:
                            publication["tower"] = payload
                    state = states.setdefault(key, {})
                    state["publication"] = publication
                pending = state["publication"]
                m.save()
                self._note_tower(pending.get("tower"))
                self.writer(pending["key"], pending["text"], destination=pending["destination"])
                state["alerted"] = True
                m.save()
            except Exception:
                self.question_samples.pop(sid, None)
                self.error = "delivery-stall question observation/publication unavailable; reserved effects may be uncertain"

    def _note_tower(self, payload):
        # New episode only. Already-alerted epochs never reach here. Inbox write is separate.
        try:
            tower = getattr(self.manager.scheduler, "tower", None)
            if tower is None or not payload:
                return
            tower.note_stall(payload)
        except Exception:
            return
