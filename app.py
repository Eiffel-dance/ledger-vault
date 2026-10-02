import argparse, copy, fcntl, hashlib, json, math, threading
from pathlib import Path

class VersionConflictError(Exception):
    # Raised by put_if_version when the active version on disk no longer
    # matches the version the caller based its update on.  When the name has
    # never been written, actual_version is None.
    def __init__(self,name,expected_version,actual_version):
        self.name=name
        self.expected_version=expected_version
        self.actual_version=actual_version
        super().__init__(name,expected_version,actual_version)

class _State:
    __slots__=("snapshot","records","by_version")
    def __init__(self,snapshot,records,by_version):
        self.snapshot=snapshot; self.records=records; self.by_version=by_version

class VersionedVault:
    def __init__(self, root="vault"):
        self.root=Path(root); self.log=self.root/"versions.jsonl"
        self._state=_State({},(),{})
        # In-process writers are serialized too: on some local filesystems
        # flock only arbitrates between processes, not threads in one process.
        self._write_lock=threading.RLock()
        self.reload()
    @staticmethod
    def _digest(item):
        body={k:item[k] for k in ("version","name","value")}
        return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    @staticmethod
    def _is_positive_int(value):
        return isinstance(value,int) and not isinstance(value,bool) and value>0
    @staticmethod
    def _ensure_storable(value):
        # A value is only accepted if it can round-trip through the on-disk
        # JSONL record and digest rules with unchanged JSON semantics.  This is
        # pure and side-effect free so callers can run it before creating any
        # directory or opening the log; every failure is a TypeError.
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            # Circular references surface as ValueError from json; unify them
            # with every other encoding failure as TypeError.
            raise TypeError("value is not JSON serializable")
        stack=[value]
        while stack:
            node=stack.pop()
            if isinstance(node,dict):
                tokens=set()
                for key,item in node.items():
                    # json.dumps silently coerces non-string keys (1 -> "1",
                    # None -> "null", (1,) -> "[1]"), and distinct float NaN
                    # keys even collapse onto one duplicated "NaN" key.  The
                    # reloaded object would differ, so reject up front.
                    if not isinstance(key,str):
                        raise TypeError("object keys must be strings")
                    token=json.dumps(key)
                    if token in tokens:
                        raise TypeError("duplicate object key after JSON encoding")
                    tokens.add(token)
                    stack.append(item)
            elif isinstance(node,(list,tuple)):
                # Tuples share JSON array semantics with lists and reload as
                # lists; descend either way so nested keys are checked too.
                stack.extend(node)
    @staticmethod
    def _json_equal(left,right):
        # Structural equality under this vault's JSON semantics: identical
        # container types and scalar values, treating NaN as equal to NaN so
        # an accepted non-finite float still verifies after a reload.  A tuple
        # never equals the list it would reload as, so such a value is rejected.
        if type(left) is not type(right):
            return False
        if isinstance(left,float) and math.isnan(left) and math.isnan(right):
            return True
        if isinstance(left,dict):
            return left.keys()==right.keys() and all(
                VersionedVault._json_equal(left[k],right[k]) for k in left)
        if isinstance(left,list):
            return len(left)==len(right) and all(
                VersionedVault._json_equal(x,y) for x,y in zip(left,right))
        return left==right
    @staticmethod
    def _object_from_pairs(pairs):
        # object_pairs_hook fires for every JSON object at every depth,
        # including the record itself, and hands us every member before
        # json.loads collapses same-named members onto one dict entry.  Names
        # are already decoded, so "a" and "a" compare equal; two members
        # decoding to the same name make the text ambiguous and corrupt the
        # record, even though last-wins parsing would still reproduce a
        # matching digest.
        seen=set()
        for key,_item in pairs:
            if key in seen:
                raise ValueError("invalid vault record")
            seen.add(key)
        return dict(pairs)
    def _parse_record(self,line,previous):
        try:
            item=json.loads(line,object_pairs_hook=self._object_from_pairs)
        except ValueError:
            raise ValueError("invalid vault record")
        if not isinstance(item,dict) or set(item)!={"version","name","value","digest"}:
            raise ValueError("invalid vault record")
        version=item["version"]
        if not self._is_positive_int(version) or version!=previous+1:
            raise ValueError("invalid vault record")
        name=item["name"]
        if not isinstance(name,str) or not name:
            raise ValueError("invalid vault record")
        digest=item["digest"]
        if not isinstance(digest,str) or digest!=self._digest(item):
            raise ValueError("invalid vault record")
        return {"version":version,"name":name,"value":item["value"]}
    def _load(self):
        snapshot,records,by_version={},[],{}
        previous=0
        if self.log.exists():
            try:
                text=self.log.read_text(encoding="utf-8")
            except ValueError:
                raise ValueError("invalid vault record")
            for line in text.splitlines():
                item=self._parse_record(line,previous)
                previous=item["version"]
                records.append(item)
                by_version[item["version"]]=item
                snapshot[item["name"]]={"version":item["version"],"value":item["value"]}
        return _State(snapshot,tuple(records),by_version)
    def reload(self):
        # Build the complete new state first; only replace the snapshot once
        # the whole chain has validated, so readers never see an intermediate
        # state and a corrupt log leaves the previous snapshot untouched.
        state=self._load()
        self._state=state
        return len(state.snapshot)
    def _append_locked(self,name,value,expected_version):
        # Shared body of put and put_if_version.  Every argument has already
        # passed its side-effect-free validation; the in-process lock plus an
        # exclusive flock serializes the whole revalidate-check-append sequence
        # so that two conditional writers can never append against the same
        # base version, as threads or as separate processes.
        with self._write_lock:
            if self.log.exists():
                # A read-only handle is enough to take the lock (flock contends
                # on the inode, so this also serializes against writers that
                # opened the log in append mode) and leaves a conditional miss
                # free of any filesystem side effect.
                lock_handle=self.log.open("r",encoding="utf-8"); created=False
            else:
                # The vault does not exist yet.  Rehearse against the empty
                # chain BEFORE creating anything, preserving put's rule that an
                # unstorable value never creates a directory; a conditional
                # update then necessarily misses (no active version on disk).
                self._build_line(name,value,1)
                if expected_version is not None:
                    raise VersionConflictError(name,expected_version,None)
                self.root.mkdir(parents=True,exist_ok=True)
                lock_handle=self.log.open("a",encoding="utf-8"); created=True
            try:
                fcntl.flock(lock_handle.fileno(),fcntl.LOCK_EX)
                # Revalidate the complete chain from disk under the lock so a
                # new record is only appended when it attaches to the valid
                # chain and the caller's base version is still active.  Another
                # writer may have populated a just-created log before this lock
                # was taken, so the version is always derived here.
                state=self._load()
                if expected_version is not None:
                    active=state.snapshot.get(name)
                    actual_version=active["version"] if active is not None else None
                    if actual_version!=expected_version:
                        # No bytes written and self._state is left untouched.
                        raise VersionConflictError(name,expected_version,actual_version)
                version=len(state.records)+1
                line=self._build_line(name,value,version)
                if created:
                    lock_handle.write(line)
                else:
                    with self.log.open("a",encoding="utf-8") as f:
                        f.write(line)
            finally:
                # Closing the append handle flushes its bytes before the lock
                # is released, so any waiter revalidates against a chain that
                # already includes the new record.
                lock_handle.close()
            self.reload(); return version
    def _build_line(self,name,value,version):
        item={"version":version,"name":name,"value":value}; item["digest"]=self._digest(item)
        line=json.dumps(item,sort_keys=True)+"\n"
        # Rehearse the append: the exact bytes about to be written must parse
        # as the next record in the chain, reproduce the digest, and reload to
        # a value equal to the submitted one.  Only then are they written.
        try:
            parsed=self._parse_record(line[:-1],version-1)
        except ValueError:
            raise TypeError("value is not storable under the vault JSON rules")
        original={"version":version,"name":name,"value":value}
        if not self._json_equal(parsed,original):
            raise TypeError("value is not storable under the vault JSON rules")
        return line
    def put(self,name,value):
        if not isinstance(name,str) or not name:
            raise ValueError("name required")
        # Validate round-trip semantics before touching the filesystem: a value
        # that json.dumps accepts but which reloads as a different object
        # (coerced or duplicated keys, NaN/Infinity, tuple arrays) must fail as
        # TypeError without creating a directory, writing bytes, or touching state.
        self._ensure_storable(value)
        return self._append_locked(name,value,None)
    def put_if_version(self,name,expected_version,value):
        if not isinstance(name,str) or not name:
            raise ValueError("name required")
        if not self._is_positive_int(expected_version):
            raise ValueError("expected_version must be a positive integer")
        # Same round-trip gate as put, run before any filesystem access.
        self._ensure_storable(value)
        return self._append_locked(name,value,expected_version)
    def get(self,name,version=None):
        state=self._state
        if version is None:
            return copy.deepcopy(state.snapshot[name]["value"])
        if not self._is_positive_int(version):
            raise ValueError("version must be a positive integer")
        record=state.by_version.get(version)
        if record is not None and record["name"]==name:
            return copy.deepcopy(record["value"])
        raise KeyError((name,version))
    def active_version(self,name):
        return self._state.snapshot[name]["version"]
    def history(self,name=None):
        records=self._state.records
        if name is not None:
            records=(r for r in records if r["name"]==name)
        return [{"version":r["version"],"name":r["name"],"value":copy.deepcopy(r["value"])} for r in records]
    def versions(self):
        return [{"name":k,"version":v["version"],"value":copy.deepcopy(v["value"])}
                for k,v in sorted(self._state.snapshot.items())]
    def snapshot_at(self,version=None):
        # Read-only whole-repository view as of the first `version` global
        # records: 0 is an empty snapshot, omitting the point (or passing the
        # latest number) reproduces the currently loaded state.  The published
        # _State is captured once and never mutated after publication, so a
        # concurrent reload can only expose the complete old or new state,
        # never a mix.  Nothing here touches disk, the active version or the
        # log, and every returned value is a fresh deep copy.
        state=self._state
        if version is None:
            records=state.records
        else:
            if not isinstance(version,int) or isinstance(version,bool) or version<0:
                raise ValueError("version must be a non-negative integer")
            total=len(state.records)
            if version>total:
                raise ValueError("version must not exceed the number of loaded records")
            records=state.records[:version]
        snapshot={}
        for r in records:
            snapshot[r["name"]]={"version":r["version"],"value":r["value"]}
        return [{"name":k,"version":v["version"],"value":copy.deepcopy(v["value"])}
                for k,v in sorted(snapshot.items())]
if __name__=="__main__":
    p=argparse.ArgumentParser(description="VersionedVault command line")
    p.add_argument("command",choices=("put","get","versions","active","history","snapshot"))
    p.add_argument("--root",default="vault")
    p.add_argument("--name")
    p.add_argument("--value",help="write the argument verbatim as a string; mutually exclusive with --value-json")
    p.add_argument("--value-json",dest="value_json",
                   help="parse the argument as one complete JSON document and write the parsed "
                        "value (objects, arrays, numbers, booleans and null keep their type); "
                        "mutually exclusive with --value: giving both, or passing text that is "
                        "not a complete JSON document, exits with status 1 without appending a record")
    p.add_argument("--version",
                   help="record/global sequence number: selects one named version for get, "
                        "or a whole-repository point in time (0 = empty snapshot) for snapshot")
    p.add_argument("--if-version",dest="if_version",
                   help="only for put: append only when the named config's active "
                        "version equals N; a missing name or a stale N exits with "
                        "status 1 without writing or printing a version")
    p.add_argument("--json",dest="json_output",action="store_true",
                   help="only affects get: print the value as a single JSON document on stdout "
                        "(sorted object keys, unescaped Unicode), accepted by json.loads; "
                        "without this flag get keeps its default output")
    a=p.parse_args()
    try:
        v=VersionedVault(a.root)
        if a.command=="put":
            # Checked by hand instead of an argparse mutually-exclusive group so
            # the conflict reports the same unified status 1 as every other
            # failure, and is rejected before parsing or writing anything.
            if a.value is not None and a.value_json is not None:
                raise SystemExit(1)
            if a.value_json is not None:
                try:
                    value=json.loads(a.value_json)
                except (ValueError,TypeError):
                    # Not a complete JSON document: no record, no version output.
                    raise SystemExit(1)
            else:
                value=a.value
            if a.if_version is not None:
                # Parsed by hand so every malformed, zero, negative or
                # fractional condition exits 1 through the same path as every
                # other rejected put, without appending or printing anything.
                try:
                    expected=int(a.if_version,10)
                except (ValueError,TypeError):
                    raise SystemExit(1)
                if expected<=0:
                    raise SystemExit(1)
                print(v.put_if_version(a.name,expected,value))
            else:
                print(v.put(a.name,value))
        elif a.command=="get":
            if a.version is None:
                value=v.get(a.name)
            else:
                # argparse-typed integer semantics are kept for get: malformed
                # input reports argument failure (status 2), not the unified 1.
                try:
                    get_version=int(a.version,10)
                except ValueError:
                    p.error("argument --version: invalid int value: %r"%a.version)
                value=v.get(a.name,get_version)
            if a.json_output:
                print(json.dumps(value,ensure_ascii=False,sort_keys=True))
            else:
                print(value)
        elif a.command=="versions":
            print(json.dumps(v.versions(),ensure_ascii=False,sort_keys=True))
        elif a.command=="active":
            print(v.active_version(a.name))
        elif a.command=="snapshot":
            if a.version is None:
                point=None
            else:
                # Parsed by hand, like put's --if-version: malformed text,
                # fractions and negative numbers all take the unified failure
                # path (status 1, nothing printed) instead of argparse's 2.
                try:
                    point=int(a.version,10)
                except ValueError:
                    raise SystemExit(1)
                if point<0:
                    raise SystemExit(1)
            # An out-of-range point raises ValueError from snapshot_at and is
            # handled below, again with no partial output.
            print(json.dumps(v.snapshot_at(point),ensure_ascii=False,sort_keys=True))
        else:
            print(json.dumps(v.history(a.name),ensure_ascii=False,sort_keys=True))
    except (KeyError,ValueError,TypeError,VersionConflictError):
        raise SystemExit(1)
