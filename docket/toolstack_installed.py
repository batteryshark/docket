"""Frozen installed Tool Stack configuration; events supply bounded DATA only.

Unmodified broker Registry/HttpRuntime/NodSurface own transport and review. This
adapter binds resource-bearing arguments before that boundary, not by prompt.
No workloads are started here. Config/TOML changes require explicit redeployment.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

from docket.toolstack import ScopedBroker


def private_config(path):
    path=Path(path)
    if not path.is_absolute():
        raise ValueError('absolute private broker configuration required')
    # The immediate control directory is private; higher ancestors may be normal
    # owned project directories, but none may be symlinked or group/world writable.
    for parent in (path.parent,*path.parent.parents):
        s=parent.lstat()
        if stat.S_ISLNK(s.st_mode) or (s.st_mode & 0o022):
            # macOS /private temporary roots can be sticky; they are not accepted
            # as direct config parents, but are safe ancestors of an owned0700 dir.
            if not (s.st_mode & stat.S_ISVTX and parent!=path.parent):
                raise ValueError('untrusted broker config parent')
    if path.parent.stat().st_uid!=os.getuid() or path.parent.stat().st_mode & 0o077:
        raise ValueError('broker configuration parent must be owned0700')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(f.fileno())
        if (not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode & 0o077
            or s.st_nlink!=1 or s.st_size>262144):
            raise ValueError('broker configuration must be owned0600')
        raw=f.read()
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def canonical_hash(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),
                                    ensure_ascii=True,allow_nan=False).encode()).hexdigest()


AUTHORITY=re.compile(r'(^|[^a-z])(path|file|filename|directory|dir|repo|repository|url|uri|host|endpoint|command|cmd|argv|env|environment|config|secret|token|credential|password|key|executable|script|cwd|location|target|destination|source|account|project|organization)([^a-z]|$)')
DANGEROUS=re.compile(r'\b(exec|execute|shell|command|secret|credential|filesystem|file|directory)\b')


class InstalledBroker:
    def __init__(self,config_ref,*,toolstack_root,db_path,approval,expected_fingerprint=None):
        self.config_ref=str(config_ref)
        self.config,self.config_hash=private_config(config_ref)
        required={'version','mode','tools_root','tool_dirs','toolyard_state_file','policy',
                  'fixed_args','data_fields','tool_timeout_seconds','rate_limit'}
        if (set(self.config)!=required or self.config['version']!=2 or self.config['mode']!='installed'
            or type(self.config['tool_timeout_seconds']) is not int
            or not 1<=self.config['tool_timeout_seconds']<=60
            or type(self.config['rate_limit']) is not int or not 1<=self.config['rate_limit']<=120):
            raise ValueError('invalid installed broker configuration')
        for value in ([self.config['toolyard_state_file']]+self.config['tool_dirs']+
                      ([self.config['tools_root']] if self.config['tools_root'] else [])):
            if not isinstance(value,str) or not Path(value).is_absolute():
                raise ValueError('explicit registry and ToolState paths required')
        if not self.config['tools_root'] and not self.config['tool_dirs']:
            raise ValueError('installed registry sources required')
        # No ambient registry, tool state, secret lookup or broker server startup.
        sys.path.insert(0,str(toolstack_root))
        from broker.registry import Registry
        from broker.store import Store
        from broker.context import BrokerContext
        from broker.runtime import HttpRuntime
        from broker.tool_state import ToolState
        from broker.audit import AuditLog
        from broker.ratelimit import RateLimiter
        from broker.surface_nod import NodSurface
        if Path(sys.modules['broker.registry'].__file__).resolve().parents[1]!=Path(toolstack_root).resolve():
            raise ValueError('installed Tool Stack identity mismatch')
        self.registry=Registry.from_sources(self.config['tools_root'],self.config['tool_dirs'])
        self.descriptors=self.describe(self.registry)
        self.sources=self.source_hashes()
        self.fingerprint=canonical_hash({'config_hash':self.config_hash,'operations':self.descriptors,'sources':self.sources})
        if expected_fingerprint and expected_fingerprint!=self.fingerprint:
            raise ValueError('broker deployment changed before allocation')
        if approval['mode']=='nod':
            from docket.sandbox_native import private_text
            surface=NodSurface(approval['url'],private_text(approval['issuer_token_file']).strip(),
                               channel=approval['channel'],timeout=5)
        elif approval['mode']=='refuse':
            surface=None
        else:
            raise ValueError('installed review requires actual NodSurface or refusal')
        private_config(self.config['toolyard_state_file'])  # No ambient/missing/permissive state fallback.
        state=ToolState(self.config['toolyard_state_file'])
        def channel_secret(tool):
            private_config(self.config['toolyard_state_file'])
            value=state.e_secret_for(tool)
            if not value: raise ValueError('installed workload channel identity unavailable')
            return value
        for descriptor in self.descriptors:
            channel_secret(descriptor['tool'])
        self.ctx=BrokerContext(Store(db_path),self.registry,
            HttpRuntime(timeout=self.config['tool_timeout_seconds'],tool_secret=channel_secret),
            None,surface,60,RateLimiter(self.config['rate_limit']))
        self.ctx.audit=AuditLog(self.ctx.store)
        self.scoped=ScopedBroker(self.ctx)

    def source_hashes(self):
        files=[]
        if self.config['tools_root']:
            files+=sorted(Path(self.config['tools_root']).glob('*/toolyard.toml'))
        files+=[Path(d)/'toolyard.toml' for d in self.config['tool_dirs']]
        if len(files)>256: raise ValueError('installed registry source bound exceeded')
        hashes={}
        for path in files:
            s=path.lstat()
            if (not stat.S_ISREG(s.st_mode) or s.st_uid not in {0,os.getuid()}
                or s.st_mode & 0o022 or s.st_size>262144):
                raise ValueError('untrusted installed operation descriptor')
            hashes[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    def describe(self,registry):
        result=[];names=set()
        for tool,ops in self.config['policy'].get('tools',{}).items():
            for op,effect in ops.items():
                if effect=='deny': continue
                if effect not in {'allow','review'}:
                    raise ValueError('invalid installed operation policy')
                entry=registry.lookup(tool,op);meta=registry.describe(tool,op)
                if entry is None or meta is None:
                    raise ValueError('installed operation missing')
                name='toolstack_'+re.sub('[^A-Za-z0-9_]','_',tool)+'__'+re.sub('[^A-Za-z0-9_]','_',op)
                if name in names: raise ValueError('native operation name collision')
                names.add(name)
                fixed=self.config['fixed_args'].get(tool+'.'+op,{})
                if not isinstance(fixed,dict): raise ValueError('invalid installed fixed arguments')
                if op=='refresh_one' and 'name' not in fixed:
                    raise ValueError('secret cache selection must be installed fixed data')
                args=meta.get('args',[])
                if len(args)>32 or len({a['name'] for a in args})!=len(args):
                    raise ValueError('ambiguous operation arguments')
                declared={a['name']:a for a in args}
                if any(k.startswith('_') for k in declared):
                    raise ValueError('reserved broker metadata argument')
                if set(fixed)-set(declared): raise ValueError('undeclared fixed argument')
                contract=self.config['data_fields'].get(tool+'.'+op)
                if (not isinstance(contract,list) or any(not isinstance(k,str) for k in contract)
                    or len(contract)!=len(set(contract)) or set(contract)-set(declared)
                    or set(contract)&set(fixed)):
                    raise ValueError('explicit reviewed operation data_fields contract required')
                if any(a.get('required') and k not in fixed and k not in contract for k,a in declared.items()):
                    raise ValueError('required input is neither fixed nor reviewed data')
                # Actual forwarder interprets body/header strings by default.
                # Initial REST support therefore permits NO event-selected args,
                # regardless of benign names, primitive types or operator list.
                if entry.type=='rest' and (contract or set(fixed)!=set(declared)
                    or (entry.body_kind not in {None,'none'} and 'body' not in fixed)):
                    raise ValueError('REST arguments including full body must be installed fixed data')
                props={};required=[]
                capability=re.sub(r'[_-]',' ',' '.join((tool,op,meta.get('description','')))).lower()
                if re.search(r'\b(secrets?|credentials?|password|api key)\b',capability) and op not in {'refresh','refresh_one'}:
                    raise ValueError('raw secret export operations are not handler capabilities')
                dangerous=DANGEROUS.search(capability)
                if dangerous and not fixed and op not in {'refresh','refresh_one'}:
                    raise ValueError('dangerous operation has no installed resource scope')
                for key,arg in declared.items():
                    self.validate_value(arg,fixed[key],installed_fixed=True) if key in fixed else None
                    if key in fixed: continue
                    if key not in contract: continue
                    description=arg.get('description','')
                    words=re.sub(r'([a-z0-9])([A-Z])',r'\1 \2',key).replace('_',' ')
                    if AUTHORITY.search((words+' '+description).lower()):
                        raise ValueError('resource/authority argument must be installed fixed data')
                    if dangerous and (not fixed or re.search(r'\b(exec|execute|shell|command|secret|credential)\b',capability)):
                        raise ValueError('unrestricted dangerous operation refused')
                    kind=arg.get('type','string')
                    if kind not in {'string','integer','number','boolean'}:
                        raise ValueError('complex arguments must be installed fixed data')
                    schema={'type':kind,'description':description}
                    if kind=='string': schema['maxLength']=min(arg.get('maxLength',4000),4000)
                    for bound in ('enum','minimum','maximum'):
                        if bound in arg: schema[bound]=arg[bound]
                    props[key]=schema
                    if arg.get('required'): required.append(key)
                result.append({'name':name,'tool':tool,'op':op,'effect':effect,
                    'description':meta.get('description',''),'schema':{'type':'object','properties':props,
                    'required':required,'additionalProperties':False},'fixed_args':fixed,
                    'declared_args':args,'transport':asdict(entry)})
        if len(result)>64: raise ValueError('installed operation ceiling exceeded')
        return result

    @staticmethod
    def validate_value(arg,value,*,installed_fixed=False):
        if not installed_fixed:
            InstalledBroker.reject_event_templates(value)
        kind=arg.get('type','string')
        valid={'string':isinstance(value,str),'integer':type(value) is int,
               'number':type(value) in (int,float) and math.isfinite(value),
               'boolean':type(value) is bool,'object':isinstance(value,dict),'array':isinstance(value,list)}
        if not valid.get(kind,False): raise ValueError('operation argument type refused')
        if isinstance(value,str) and len(value.encode())>min(arg.get('maxLength',4000),4000):
            raise ValueError('operation data size refused')
        if 'enum' in arg and value not in arg['enum']: raise ValueError('argument enumeration refused')
        if kind in {'number','integer'} and (value<arg.get('minimum',-1e100) or value>arg.get('maximum',1e100)):
            raise ValueError('operation numeric bound refused')

    @staticmethod
    def reject_event_templates(value,depth=0):
        if depth>16: raise ValueError('event data nesting bound exceeded')
        if isinstance(value,str):
            if re.search(r'\{\{\s*secret\s*:',value,re.IGNORECASE):
                raise ValueError('event-selected secret template refused')
        elif isinstance(value,dict):
            for key,child in value.items():
                InstalledBroker.reject_event_templates(key,depth+1)
                InstalledBroker.reject_event_templates(child,depth+1)
        elif isinstance(value,list):
            for child in value: InstalledBroker.reject_event_templates(child,depth+1)

    def unchanged(self):
        from broker.registry import Registry
        _,config_hash=private_config(self.config_ref)
        if config_hash!=self.config_hash: raise ValueError('installed broker config changed')
        registry=Registry.from_sources(self.config['tools_root'],self.config['tool_dirs'])
        if canonical_hash({'config_hash':config_hash,'operations':self.describe(registry),'sources':self.source_hashes()})!=self.fingerprint:
            raise ValueError('installed operation descriptor changed')

    def native(self,baseline,profile=None):
        """Only installed fixed names; review cannot be widened to allow."""
        from docket.toolstack import narrow_policy
        mapping={d['name']:d for d in self.descriptors}
        requested=baseline['policy'] if profile is None else profile
        if any(name not in mapping for name,effect in requested.items() if effect!='deny'):
            raise ValueError('class exposes uninstalled native capability')
        tools=[];broker_profile={'tools':{}};operation_map={}
        for name,effect in requested.items():
            if effect=='deny': continue
            if effect not in {'allow','review'} or baseline['policy'].get(name,'deny')=='deny':
                raise ValueError('invalid installed narrowing')
            if baseline['policy'][name]=='review' and effect=='allow':
                raise ValueError('native review upgrade refused')
            d=mapping[name]
            tools.append({k:d[k] for k in ('name','tool','op','description','schema')})
            operation_map[name]=[d['tool']+'.'+d['op']]
            broker_profile['tools'].setdefault(d['tool'],{})[d['op']]=effect
        return tools,narrow_policy(self.config['policy'],broker_profile),operation_map

    def handle(self,method,path,headers,body,*,binding,call_id=None):
        from broker.gateway import Response
        from broker.identity import authenticate
        try:
            self.unchanged()
        except ValueError:
            self.ctx.audit.record('policy','installed_deployment_changed','denied',call_id or 'docket',details={})
            return Response(409,{'error':'installed_broker_redeployment_required'},call_id or 'docket')
        if method=='POST' and path.startswith('/v1/actions/'):
            spec=path[len('/v1/actions/'):]
            descriptor=next((d for d in self.descriptors if d['tool']+'.'+d['op']==spec),None)
            try:
                if descriptor is None: raise ValueError('uninstalled operation')
                arguments=body.get('arguments') if isinstance(body,dict) else None
                if not isinstance(arguments,dict): raise ValueError('arguments object required')
                self.reject_event_templates(arguments)
                self.reject_event_templates(body.get('reason'))
                if (set(body)-{'arguments','reason'} or ('reason' in body and
                    (not isinstance(body['reason'],str) or len(body['reason'].encode())>1000))):
                    raise ValueError('invalid broker metadata')
                if set(arguments)&set(descriptor['fixed_args']): raise ValueError('fixed authority override')
                schema=descriptor['schema']
                if set(arguments)-set(schema['properties']) or set(schema['required'])-set(arguments):
                    raise ValueError('undeclared/missing operation data')
                declared={a['name']:a for a in descriptor['declared_args']}
                for key,value in arguments.items(): self.validate_value(declared[key],value)
                body={**body,'arguments':{**arguments,**descriptor['fixed_args']}}
            except ValueError:
                # Preserve per-handler denial/audit without executing anything.
                caller=authenticate(self.ctx.store,next((v for k,v in headers.items() if k.lower()=='authorization'),None))
                if caller and descriptor:
                    rid=self.ctx.store.create_request(call_id or 'docket',caller.id,descriptor['tool'],descriptor['op'],'denied')
                    self.ctx.audit.record('policy','installed_scope_denied','denied',call_id or 'docket',request_id=rid,details={})
                    return Response(403,{'status':'denied','request_id':rid,'reason':'installed_resource_scope'},call_id or 'docket')
                return Response(403,{'error':'installed_operation_denied'},call_id or 'docket')
        return self.scoped.handle(method,path,headers,body,binding=binding,call_id=call_id)
