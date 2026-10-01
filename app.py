import argparse, copy, hashlib, json
from pathlib import Path

class VersionedVault:
    def __init__(self, root="vault"):
        self.root=Path(root); self.log=self.root/"versions.jsonl"
        self.snapshot={}; self.history_records=[]; self.reload()
    @staticmethod
    def _digest(item):
        body={k:item[k] for k in ("version","name","value")}
        return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    @staticmethod
    def _invalid(): raise ValueError("invalid vault record")
    @staticmethod
    def _copy_records(records):
        return [{"version":r["version"],"name":r["name"],"value":copy.deepcopy(r["value"])} for r in records]
    def reload(self):
        candidate={}; records=[]; previous=0
        try:
            if self.log.exists():
                text=self.log.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if not line.strip(): self._invalid()
                    item=json.loads(line)
                    if not isinstance(item,dict) or set(item) != {"version","name","value","digest"}:
                        self._invalid()
                    version=item["version"]
                    if isinstance(version,bool) or not isinstance(version,int) or version != previous+1:
                        self._invalid()
                    name=item["name"]
                    if not isinstance(name,str) or not name:
                        self._invalid()
                    if not isinstance(item["digest"],str) or item["digest"] != self._digest(item):
                        self._invalid()
                    records.append({"version":version,"name":name,"value":item["value"]})
                    candidate[name]={"version":version,"value":item["value"]}
                    previous=version
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid vault record") from exc
        # 历史与快照来自同一条已验证记录链，在此一次性交换
        self.snapshot=candidate; self.history_records=records
        return len(candidate)
    def put(self,name,value):
        if not name: raise ValueError("name required")
        version=sum(1 for _ in self.log.open(encoding="utf-8"))+1 if self.log.exists() else 1
        item={"version":version,"name":name,"value":value}; item["digest"]=self._digest(item)
        self.root.mkdir(parents=True,exist_ok=True)
        with self.log.open("a",encoding="utf-8") as f: f.write(json.dumps(item,sort_keys=True)+"\n")
        self.reload(); return version
    def get(self,name,version=None):
        if version is None: return self.snapshot[name]["value"]
        if isinstance(version,bool) or not isinstance(version,int) or version <= 0:
            raise ValueError("version must be a positive integer")
        if not 1 <= version <= len(self.history_records):
            raise KeyError((name,version))
        record=self.history_records[version-1]
        if record["name"] != name:
            raise KeyError((name,version))
        return copy.deepcopy(record["value"])
    def active_version(self,name):
        entry=self.snapshot.get(name)
        if entry is None: raise KeyError(name)
        return entry["version"]
    def history(self,name=None):
        if name is None:
            return self._copy_records(self.history_records)
        return self._copy_records(r for r in self.history_records if r["name"] == name)
    def versions(self): return [{"name":k,**v} for k,v in sorted(self.snapshot.items())]
if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("put","get","versions","active","history")); p.add_argument("--root",default="vault"); p.add_argument("--name"); p.add_argument("--value"); p.add_argument("--version",type=int); a=p.parse_args(); v=VersionedVault(a.root)
    if a.command=="put": print(v.put(a.name,a.value))
    elif a.command=="get":
        try: print(v.get(a.name,a.version))
        except (KeyError,ValueError): raise SystemExit(1)
    elif a.command=="versions": print(json.dumps(v.versions(),ensure_ascii=False,sort_keys=True))
    elif a.command=="active":
        try: print(v.active_version(a.name))
        except KeyError: raise SystemExit(1)
    else:
        print(json.dumps(v.history(a.name),ensure_ascii=False))
