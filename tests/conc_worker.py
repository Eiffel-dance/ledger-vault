"""Worker process for the cross-process concurrency tests.

Not a test module (its name does not start with "test"), so unittest
discovery ignores it.  It uses only the standard library and the public
VersionedVault API, exactly like any offline consumer of the vault.

Usage:
    python conc_worker.py put ROOT COUNT PREFIX
    python conc_worker.py batch ROOT COUNT SIZE PREFIX
    python conc_worker.py cond ROOT NAME EXPECTED VALUE
    python conc_worker.py loop ROOT PREFIX

cond exits 0 on success and 3 on VersionConflictError, printing the
conflict's actual_version as one JSON document on stdout; any unexpected
failure exits 1.  loop appends forever (or until it is killed) so the
tests can verify that a dying writer releases the coordination.
"""
import json, sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

from app import VersionedVault, VersionConflictError

def main(argv):
    mode,root=argv[0],argv[1]
    vault=VersionedVault(root)
    if mode=="put":
        count=int(argv[2]); prefix=argv[3]
        for index in range(count):
            vault.put("%s-%d"%(prefix,index),index)
        return 0
    if mode=="batch":
        count=int(argv[2]); size=int(argv[3]); prefix=argv[4]
        for round_ in range(count):
            vault.put_batch([["%s-%d-%d"%(prefix,round_,i),[round_,i]] for i in range(size)])
        return 0
    if mode=="cond":
        name,expected,value=argv[2],int(argv[3]),argv[4]
        try:
            vault.put_if_version(name,expected,value)
        except VersionConflictError as exc:
            print(json.dumps(exc.actual_version))
            return 3
        return 0
    if mode=="loop":
        prefix=argv[2]; index=0
        while True:
            vault.put("%s-%d"%(prefix,index),index)
            index+=1
    return 1

if __name__=="__main__":
    sys.exit(main(sys.argv[1:]))
