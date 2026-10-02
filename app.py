import argparse, copy, hashlib, json, math
from pathlib import Path

class _State:
    __slots__=("snapshot","records","by_version")
    def __init__(self,snapshot,records,by_version):
        self.snapshot=snapshot; self.records=records; self.by_version=by_version

class VersionedVault:
    def __init__(self, root="vault"):
        self.root=Path(root); self.log=self.root/"versions.jsonl"
        self._state=_State({},(),{})
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
    def _parse_record(self,line,previous):
        try:
            item=json.loads(line)
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
    def put(self,name,value):
        if not isinstance(name,str) or not name:
            raise ValueError("name required")
        # Validate round-trip semantics before touching the filesystem: a value
        # that json.dumps accepts but which reloads as a different object
        # (coerced or duplicated keys, NaN/Infinity, tuple arrays) must fail as
        # TypeError without creating a directory, writing bytes, or touching state.
        self._ensure_storable(value)
        # Revalidate the complete chain from disk so a new record is only
        # appended when it can attach to the existing valid chain.
        state=self._load()
        version=len(state.records)+1
        item={"version":version,"name":name,"value":value}; item["digest"]=self._digest(item)
        line=json.dumps(item,sort_keys=True)+"\n"
        # Rehearse the append: the exact bytes about to be written must parse
        # as the next record in the chain, reproduce the digest, and reload to
        # a value equal to the submitted one.  Only then open the log.
        try:
            parsed=self._parse_record(line[:-1],version-1)
        except ValueError:
            raise TypeError("value is not storable under the vault JSON rules")
        original={"version":version,"name":name,"value":value}
        if not self._json_equal(parsed,original):
            raise TypeError("value is not storable under the vault JSON rules")
        self.root.mkdir(parents=True,exist_ok=True)
        with self.log.open("a",encoding="utf-8") as f: f.write(line)
        self.reload(); return version
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
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=("put","get","versions","active","history"))
    p.add_argument("--root",default="vault"); p.add_argument("--name")
    p.add_argument("--value",help="string to store for put, written as-is (mutually exclusive with --value-json)")
    p.add_argument("--value-json",metavar="JSON",
                   help="complete JSON document to store for put; the parsed object, array, number, "
                        "boolean or null keeps its type on later reads (mutually exclusive with --value)")
    p.add_argument("--version",type=int)
    p.add_argument("--json",action="store_true",
                   help="for get: print the value as a single JSON document parseable by json.loads, "
                        "with sorted object keys and unescaped Unicode like versions/history output")
    a=p.parse_args()
    if a.command=="put" and a.value is not None and a.value_json is not None:
        # Reject before any record is appended or a version number is printed.
        p.exit(1,"error: --value and --value-json are mutually exclusive\n")
    value=a.value
    if a.command=="put" and a.value_json is not None:
        try: value=json.loads(a.value_json)
        except ValueError: p.exit(1,"error: --value-json is not a complete JSON document\n")
    v=VersionedVault(a.root)
    try:
        if a.command=="put": print(v.put(a.name,value))
        elif a.command=="get":
            got=v.get(a.name) if a.version is None else v.get(a.name,a.version)
            print(json.dumps(got,ensure_ascii=False,sort_keys=True) if a.json else got)
        elif a.command=="versions": print(json.dumps(v.versions(),ensure_ascii=False,sort_keys=True))
        elif a.command=="active": print(v.active_version(a.name))
        else: print(json.dumps(v.history(a.name),ensure_ascii=False,sort_keys=True))
    except (KeyError,ValueError,TypeError):
        raise SystemExit(1)
