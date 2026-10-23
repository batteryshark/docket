"""Docket-owned summary journal operation; no file/URL/command/read capability.

Integrate into the EXISTING Docket HTTP handler before its ordinary admin bearer
route. Unmodified Tool Stack HttpRuntime sends POST /v1/actions/record with
X-Toolstack-Secret and {arguments, broker_request_id, caller:{name}}. The broker
ingress /v1/actions/docket.record is a DIFFERENT hop, not this endpoint.

Only the installed channel's namespace/source grant authority. Claimed caller
names are never used as authentication, source selection or dedupe namespaces.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

PATH = '/v1/actions/record'
MAX_BODY_BYTES = 8192
MAX_TEXT_BYTES = 512
MAX_REQUEST_ID = 2**63-1
TABLE = 'docket_event_outcomes'
_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$')
_HASH = re.compile(r'^[0-9a-f]{64}$')
_CALLER = re.compile(r'^docket-[0-9a-f]{32}$')

SCHEMA = '''CREATE TABLE IF NOT EXISTS docket_event_outcomes (
  row_id INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL,
  broker_request_id INTEGER NOT NULL CHECK(broker_request_id > 0),
  caller_scope TEXT NOT NULL,
  source TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('intent','recorded')),
  text TEXT,
  created_at_ms INTEGER NOT NULL,
  UNIQUE(namespace, broker_request_id),
  UNIQUE(namespace, caller_scope),
  CHECK(text IS NULL OR length(CAST(text AS BLOB)) <= 512)
)'''


@dataclass(frozen=True)
class JournalResponse:
    status: int
    body: dict


def _private_config(path):
    path = Path(path)
    if not path.is_absolute() or path.parent.is_symlink():
        raise ValueError('explicit private channel configuration required')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError('private owned channel configuration parent required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 4096):
            raise ValueError('channel configuration must be owned0600')
        config = json.load(f)
    if (not isinstance(config, dict) or set(config) != {'version','namespace','source','channel_token_sha256'}
        or type(config['version']) is not int or config['version'] != 1
        or any(not isinstance(config[k],str) or not _LABEL.fullmatch(config[k]) for k in ('namespace','source'))
        or not isinstance(config['channel_token_sha256'],str) or not _HASH.fullmatch(config['channel_token_sha256'])):
        raise ValueError('invalid fixed channel configuration')
    return config


class EventJournal:
    def __init__(self, db_path, channel_config_ref):
        self.path = Path(db_path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ValueError('explicit Docket-owned SQLite path required')
        self.config = _private_config(channel_config_ref)
        self._expected = bytes.fromhex(self.config['channel_token_sha256'])
        if self._expected == hashlib.sha256(b'').digest():
            raise ValueError('empty channel credentials are forbidden')
        db = self._connect()
        try:
            db.execute(SCHEMA)
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=3000')
            db.execute('PRAGMA synchronous=FULL')
        except sqlite3.Error:
            db.close()
            raise
        return db

    def preflight(self, method, path, headers, content_length=None):
        """Return rejection BEFORE reading bytes. Caller closes rejected requests.

        The enclosing HTTP server must set its ordinary finite socket read
        timeout and read exactly the admitted Content-Length, never an unbounded
        body. No other Docket token is accepted as this channel credential.
        """
        if path != PATH:
            return JournalResponse(404, {'error':'not_found'})
        if method != 'POST':
            return JournalResponse(405, {'error':'record_only'})
        headers = {k.lower():v for k,v in headers.items()}
        token = headers.get('x-toolstack-secret', '')
        if not isinstance(token,str) or not token or len(token.encode('utf-8',errors='replace')) > 1024:
            return JournalResponse(401, {'error':'journal_channel_required'})
        supplied = hashlib.sha256(token.encode('utf-8',errors='replace')).digest()
        if not hmac.compare_digest(supplied,self._expected):
            return JournalResponse(401, {'error':'journal_channel_required'})
        if headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
            return JournalResponse(415, {'error':'json_required'})
        if headers.get('transfer-encoding'):
            return JournalResponse(400, {'error':'fixed_length_required'})
        length = content_length if content_length is not None else headers.get('content-length')
        if isinstance(length,str) and len(length)>20:
            return JournalResponse(413, {'error':'body_too_large'})
        if isinstance(length,str) and length.isascii() and length.isdigit():
            length = int(length)
        if type(length) is not int or length < 1:
            return JournalResponse(411, {'error':'bounded_content_length_required'})
        if length > MAX_BODY_BYTES:
            return JournalResponse(413, {'error':'body_too_large'})
        return None

    def handle(self, method, path, headers, raw_body):
        """Only byte input; response contains receipt IDs/hashes, never text/rows."""
        if not isinstance(raw_body, bytes):
            return JournalResponse(400, {'error':'raw_bytes_required'})
        rejected = self.preflight(method,path,headers)
        if rejected:
            return rejected
        lower = {k.lower():v for k,v in headers.items()}
        if len(raw_body) > MAX_BODY_BYTES:
            return JournalResponse(413, {'error':'body_too_large'})
        if int(lower['content-length']) != len(raw_body):
            return JournalResponse(400, {'error':'body_length_mismatch'})
        try:
            def unique(pairs):
                value={}
                for key,child in pairs:
                    if key in value: raise ValueError('duplicate JSON key')
                    value[key]=child
                return value
            envelope = json.loads(raw_body.decode('utf-8'),object_pairs_hook=unique)
            if not isinstance(envelope,dict) or set(envelope) != {'arguments','broker_request_id','caller'}:
                raise ValueError('invalid envelope')
            request_id = envelope['broker_request_id']
            if type(request_id) is not int or not 1 <= request_id <= MAX_REQUEST_ID:
                raise ValueError('invalid broker request identity')
            caller = envelope['caller']
            if (not isinstance(caller,dict) or set(caller) != {'name'} or not isinstance(caller['name'],str)
                or not _CALLER.fullmatch(caller['name'])):
                raise ValueError('invalid untrusted caller metadata')
            args = envelope['arguments']
            if not isinstance(args,dict) or set(args) != {'source','text'}:
                raise ValueError('unknown or missing arguments')
            if args['source'] != self.config['source'] or not isinstance(args['source'],str):
                raise ValueError('installed source scope mismatch')
            text = args['text']
            if not isinstance(text,str) or not 1 <= len(text.encode('utf-8')) <= MAX_TEXT_BYTES:
                raise ValueError('text byte bound')
            body_hash = hashlib.sha256(json.dumps(envelope,sort_keys=True,separators=(',',':'),
                ensure_ascii=True,allow_nan=False).encode()).hexdigest()
            text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        except (ValueError,TypeError,UnicodeError,RecursionError):
            return JournalResponse(400, {'error':'invalid_journal_request'})
        try:
            db = self._connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT row_id,body_sha256,text_sha256,state FROM docket_event_outcomes WHERE namespace=? AND broker_request_id=?',
                    (self.config['namespace'],request_id)).fetchone()
                if old:
                    db.rollback()
                    if old['body_sha256'] != body_hash:
                        return JournalResponse(409, {'error':'broker_request_id_conflict'})
                    if old['state'] != 'recorded':
                        return JournalResponse(409, {'error':'journal_intent_requires_reconciliation'})
                    return JournalResponse(200, {'status':'recorded','row_id':old['row_id'],'sha256':old['text_sha256']})
                # The private channel authenticates the trusted HttpRuntime hop;
                # ScopedBroker supplies this per-instance name, never tool args.
                # Use it ONLY to narrow quota within the installed namespace, not
                # to acquire source/admin/read authority. A new ID cannot turn an
                # uncertain/finished handler into another journal insertion.
                if db.execute('SELECT 1 FROM docket_event_outcomes WHERE namespace=? AND caller_scope=?',
                              (self.config['namespace'],caller['name'])).fetchone():
                    db.rollback()
                    return JournalResponse(409, {'error':'handler_outcome_already_reserved'})
                # Intent and its ONLY effect (this SQLite row) commit atomically.
                # A crash before commit leaves neither; there is no external I/O.
                cursor = db.execute('INSERT INTO docket_event_outcomes(namespace,broker_request_id,caller_scope,source,body_sha256,text_sha256,state,text,created_at_ms) VALUES(?,?,?,?,?,?,\'intent\',NULL,?)',
                    (self.config['namespace'],request_id,caller['name'],self.config['source'],body_hash,text_hash,int(time.time()*1000)))
                row_id = cursor.lastrowid
                db.execute("UPDATE docket_event_outcomes SET text=?,state='recorded' WHERE row_id=?",(text,row_id))
                db.commit()
                return JournalResponse(200, {'status':'recorded','row_id':row_id,'sha256':text_hash})
            finally:
                db.close()
        except sqlite3.Error:
            return JournalResponse(503, {'error':'journal_unavailable'})

    def list_outcomes(self, limit=100, *, before_id=None):
        """Trusted Python admin caller ONLY; never routed by handle/preflight.

        Any later HTTP integration MUST first use existing Docket admin auth.
        Channel holders have no read capability through this module's HTTP API.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('admin read limit must be1..100')
        if before_id is not None and (type(before_id) is not int or not 1 <= before_id <= MAX_REQUEST_ID):
            raise ValueError('invalid admin cursor')
        db = self._connect()
        try:
            db.execute('PRAGMA query_only=ON')
            condition = ' AND row_id<?' if before_id is not None else ''
            params = (before_id,limit) if before_id is not None else (limit,)
            rows = db.execute("SELECT row_id,source,text,text_sha256,created_at_ms FROM docket_event_outcomes WHERE state='recorded'"+
                              condition+' ORDER BY row_id DESC LIMIT ?',params).fetchall()
            return [dict(row) for row in rows]
        finally:
            db.close()
