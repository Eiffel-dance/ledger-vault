import argparse, hashlib, json
from pathlib import Path

class VersionedVault:
    def __init__(self, root="vault"):
        self.root=Path(root); self.log=self.root/"versions.jsonl"; self.snapshot={}; self.reload()
    @staticmethod
    def _digest(item):
        body={k:item[k] for k in ("version","name","value")}
        return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def reload(self):
        candidate={}; previous=0
        if self.log.exists():
            for line in self.log.read_text(encoding="utf-8").splitlines():
                item=json.loads(line)
                if item.get("version") != previous+1 or item.get("digest") != self._digest(item):
                    raise ValueError("invalid vault record")
                candidate[item["name"]]={"version":item["version"],"value":item["value"]}; previous=item["version"]
        self.snapshot=candidate; return len(candidate)
    def put(self,name,value):
        if not name: raise ValueError("name required")
        version=sum(1 for _ in self.log.open(encoding="utf-8"))+1 if self.log.exists() else 1
        item={"version":version,"name":name,"value":value}; item["digest"]=self._digest(item)
        self.root.mkdir(parents=True,exist_ok=True)
        with self.log.open("a",encoding="utf-8") as f: f.write(json.dumps(item,sort_keys=True)+"
")
        self.reload(); return version
    def get(self,name): return self.snapshot[name]["value"]
    def versions(self): return [{"name":k,**v} for k,v in sorted(self.snapshot.items())]
if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("put","get","versions")); p.add_argument("--root",default="vault"); p.add_argument("--name"); p.add_argument("--value"); a=p.parse_args(); v=VersionedVault(a.root)
    if a.command=="put": print(v.put(a.name,a.value))
    elif a.command=="get": print(v.get(a.name))
    else: print(json.dumps(v.versions(),ensure_ascii=False,sort_keys=True))
