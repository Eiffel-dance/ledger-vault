import argparse, copy, hashlib, json
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
    @staticmethod
    def _no_duplicate_keys(pairs):
        # Operates on the already-encoded keys, so this also catches Python
        # keys that dumps() maps onto the same JSON key (1/"1", True/"true",
        # None/"null", 1.0/"1.0", multiple NaNs...). Applies to every nested
        # object as well.
        seen=set()
        for key,_ in pairs:
            if key in seen:
                raise ValueError("duplicate key")
            seen.add(key)
        return dict(pairs)
    def _stable_line(self,name,value,version):
        # Determine persistence semantics before mkdir, open-log, or any state
        # change. The candidate record must (1) encode with the exact writer
        # settings, (2) decode the way reload() decodes it without an object
        # merging or losing keys, and (3) reproduce byte-identical JSON and a
        # valid digest after decoding, so the in-memory view and a fresh
        # cross-instance load are the same JSON value. Everything else is a
        # caller (type) error, unified as TypeError.
        item={"version":version,"name":name,"value":value}
        try:
            item["digest"]=self._digest(item)
            text=json.dumps(item,sort_keys=True)
            decoded=json.loads(text,object_pairs_hook=self._no_duplicate_keys)
            if json.dumps(decoded,sort_keys=True)!=text:
                raise ValueError("value does not round-trip")
            if decoded["digest"]!=self._digest(decoded):
                raise ValueError("digest does not round-trip")
        except (TypeError,ValueError) as exc:
            raise TypeError("value is not JSON serializable") from exc
        return text+"\n"
    def put(self,name,value):
        if not isinstance(name,str) or not name:
            raise ValueError("name required")
        # Revalidate the complete chain from disk so a new record is only
        # appended when it can attach to the existing valid chain. Corruption
        # stays a ValueError and is never repaired by appending past it;
        # _load builds a fresh state without touching live state or disk.
        state=self._load()
        version=len(state.records)+1
        # Encode the record and replay it through the reload rules before
        # creating a directory, opening the log, or changing in-memory state.
        line=self._stable_line(name,value,version)
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
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("put","get","versions","active","history")); p.add_argument("--root",default="vault"); p.add_argument("--name"); p.add_argument("--value"); p.add_argument("--version",type=int); a=p.parse_args(); v=VersionedVault(a.root)
    try:
        if a.command=="put": print(v.put(a.name,a.value))
        elif a.command=="get": print(v.get(a.name) if a.version is None else v.get(a.name,a.version))
        elif a.command=="versions": print(json.dumps(v.versions(),ensure_ascii=False,sort_keys=True))
        elif a.command=="active": print(v.active_version(a.name))
        else: print(json.dumps(v.history(a.name),ensure_ascii=False,sort_keys=True))
    except (KeyError,ValueError,TypeError):
        raise SystemExit(1)
