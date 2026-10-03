"""Offline concurrency verification for VersionedVault.

Covers, with the standard library only and on Windows and POSIX alike:
thread concurrency, cross-process concurrency, first write into an empty
vault, batch commits, conditional-write races, writes after every failure
type, and readers never observing a half-committed record or batch.
"""
import json, multiprocessing, os, subprocess, sys, tempfile, threading, unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app import VersionedVault, VersionConflictError


def _process_put_worker(root,name,count,queue):
    # Each process uses its own instance and appends `count` records under
    # distinct names, then reports every version number it was assigned.
    vault=VersionedVault(root)
    versions=[]
    try:
        for index in range(count):
            versions.append(vault.put(name,"%s-%d"%(name,index)))
        queue.put(("ok",versions))
    except Exception as exc:  # noqa: BLE001 - reported to the parent verbatim
        queue.put(("error",repr(exc)))


def _process_conditional_worker(root,name,expected,value,queue):
    # One conditional append attempt; the result distinguishes success from
    # the expected conflict so the parent can count winners.
    vault=VersionedVault(root)
    try:
        queue.put(("ok",vault.put_if_version(name,expected,value)))
    except VersionConflictError as exc:
        queue.put(("conflict",exc.actual_version))
    except Exception as exc:  # noqa: BLE001
        queue.put(("error",repr(exc)))


def _process_batch_worker(root,tag,count,queue):
    vault=VersionedVault(root)
    versions=[]
    try:
        for index in range(count):
            versions.extend(vault.put_batch(
                [["%s-a-%d"%(tag,index),index],["%s-b-%d"%(tag,index),{"p":index}]]))
        queue.put(("ok",versions))
    except Exception as exc:  # noqa: BLE001
        queue.put(("error",repr(exc)))


class _ConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self._tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root=Path(self._tmp.name)/"vault"
    def chain_versions(self):
        vault=VersionedVault(self.root)
        return [r["version"] for r in vault.history()],vault


class ThreadConcurrencyTest(_ConcurrencyCase):
    def test_threads_on_shared_instances_get_continuous_versions(self):
        # Several threads share two instances of the same root; every put
        # must succeed exactly once and the global chain must be continuous.
        vaults=[VersionedVault(self.root),VersionedVault(self.root)]
        per_thread=25
        results=[[] for _ in range(8)]
        errors=[]
        def worker(slot,vault,tag):
            try:
                for index in range(per_thread):
                    results[slot].append(vault.put(tag,"%d"%index))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads=[threading.Thread(target=worker,
                                  args=(slot,vaults[slot%2],"t%d"%slot))
                 for slot in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors,[])
        assigned=sorted(v for slot in results for v in slot)
        self.assertEqual(assigned,list(range(1,8*per_thread+1)))
        versions,_=self.chain_versions()
        self.assertEqual(versions,list(range(1,8*per_thread+1)))

    def test_mixed_put_and_batch_never_interleave(self):
        vault=VersionedVault(self.root)
        errors=[]
        def putter():
            try:
                for index in range(20):
                    vault.put("single-%d"%index,index)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        def batcher(tag):
            try:
                for index in range(10):
                    vault.put_batch([["%s-x-%d"%(tag,index),index],
                                     ["%s-y-%d"%(tag,index),index],
                                     ["%s-z-%d"%(tag,index),index]])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads=[threading.Thread(target=putter)]+[
            threading.Thread(target=batcher,args=("b%d"%tag,)) for tag in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors,[])
        # 20 singles + 3*10 batches of 3 = 110 records, continuous, and every
        # batch occupies an indivisible consecutive range.
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],list(range(1,111)))
        by_batch={}
        for r in history:
            name=r["name"]
            if name.startswith("b"):
                tag=name.split("-")[0]
                by_batch.setdefault(tag,[]).append(r["version"])
        for tag,versions in by_batch.items():
            for offset in range(0,len(versions),3):
                run=versions[offset:offset+3]
                self.assertEqual(run,list(range(run[0],run[0]+3)),tag)

    def test_conditional_race_allows_a_single_winner(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("k","base"),1)
        outcomes=[]
        outcomes_lock=threading.Lock()
        def contender(index):
            try:
                result=("ok",vault.put_if_version("k",1,"w%d"%index))
            except VersionConflictError as exc:
                result=("conflict",exc.actual_version)
            with outcomes_lock:
                outcomes.append(result)
        threads=[threading.Thread(target=contender,args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        wins=[r for r in outcomes if r[0]=="ok"]
        losses=[r for r in outcomes if r[0]=="conflict"]
        # Exactly one append succeeds; everyone else sees the existing
        # VersionConflictError whose actual_version reflects the disk chain,
        # i.e. the winner's committed version 2.
        self.assertEqual(len(wins),1)
        self.assertEqual(wins[0][1],2)
        self.assertEqual(len(losses),9)
        self.assertTrue(all(actual==2 for _,actual in losses))
        self.assertEqual([r["version"] for r in vault.history()],[1,2])
        self.assertEqual(vault.active_version("k"),2)

    def test_reload_never_sees_partial_commit(self):
        # A reader thread reloads continuously while writers commit batches;
        # every reload must validate the complete chain (no half record, no
        # batch middle state) and observe a continuous version sequence.
        vault=VersionedVault(self.root)
        stop=threading.Event()
        errors=[]
        def reader():
            while not stop.is_set():
                try:
                    other=VersionedVault(self.root)
                    versions=[r["version"] for r in other.history()]
                    if versions!=list(range(1,len(versions)+1)):
                        errors.append("gap: %r"%versions[-5:])
                except ValueError as exc:
                    errors.append("reader saw partial commit: %s"%exc)
        def writer(tag):
            try:
                for index in range(15):
                    vault.put_batch([["%s-a-%d"%(tag,index),[index]*50],
                                     ["%s-b-%d"%(tag,index),[index]*50]])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        readers=[threading.Thread(target=reader) for _ in range(3)]
        writers=[threading.Thread(target=writer,args=("w%d"%t,)) for t in range(3)]
        for t in readers: t.start()
        for t in writers: t.start()
        for t in writers: t.join()
        stop.set()
        for t in readers: t.join()
        self.assertEqual(errors,[])
        self.assertEqual(len(VersionedVault(self.root).history()),90)


class ProcessConcurrencyTest(_ConcurrencyCase):
    def run_processes(self,target,args_per_process):
        ctx=multiprocessing.get_context("spawn" if os.name=="nt" else "fork")
        queue=ctx.Queue()
        procs=[ctx.Process(target=target,args=args+(queue,))
               for args in args_per_process]
        for p in procs: p.start()
        outcomes=[queue.get() for _ in procs]
        for p in procs: p.join(60)
        for p in procs:
            self.assertEqual(p.exitcode,0)
        return outcomes

    def test_processes_share_one_root_with_continuous_versions(self):
        count=15
        outcomes=self.run_processes(
            _process_put_worker,
            [(str(self.root),"p%d"%tag,count) for tag in range(4)])
        assigned=[]
        for status,versions in outcomes:
            self.assertEqual(status,"ok",versions)
            assigned.extend(versions)
        self.assertEqual(sorted(assigned),list(range(1,4*count+1)))
        versions,vault=self.chain_versions()
        self.assertEqual(versions,list(range(1,4*count+1)))
        for tag in range(4):
            self.assertEqual(vault.get("p%d"%tag),"p%d-%d"%(tag,count-1))

    def test_first_concurrent_creation_of_empty_vault(self):
        # The log does not exist when all processes start: creation itself is
        # coordinated, every append still lands exactly once.
        self.assertFalse(self.root.exists())
        outcomes=self.run_processes(
            _process_put_worker,
            [(str(self.root),"first%d"%tag,5) for tag in range(4)])
        assigned=[]
        for status,versions in outcomes:
            self.assertEqual(status,"ok",versions)
            assigned.extend(versions)
        self.assertEqual(sorted(assigned),list(range(1,21)))
        versions,_=self.chain_versions()
        self.assertEqual(versions,list(range(1,21)))

    def test_cross_process_conditional_race_has_one_winner(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("k","base"),1)
        outcomes=self.run_processes(
            _process_conditional_worker,
            [(str(self.root),"k",1,"winner-%d"%tag) for tag in range(6)])
        wins=[o for o in outcomes if o[0]=="ok"]
        losses=[o for o in outcomes if o[0]=="conflict"]
        self.assertEqual(len(wins),1,outcomes)
        self.assertEqual(wins[0][1],2)
        self.assertEqual(len(losses),5,outcomes)
        self.assertTrue(all(actual==2 for _,actual in losses))
        versions,vault=self.chain_versions()
        self.assertEqual(versions,[1,2])

    def test_cross_process_batches_keep_indivisible_ranges(self):
        outcomes=self.run_processes(
            _process_batch_worker,
            [(str(self.root),"p%d"%tag,8) for tag in range(3)])
        assigned=[]
        for status,versions in outcomes:
            self.assertEqual(status,"ok",versions)
            assigned.extend(versions)
        self.assertEqual(sorted(assigned),list(range(1,3*8*2+1)))
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],
                         list(range(1,len(history)+1)))
        runs={}
        for r in history:
            tag=r["name"].split("-")[0]
            runs.setdefault(tag,[]).append(r["version"])
        for tag,versions in runs.items():
            for offset in range(0,len(versions),2):
                pair=versions[offset:offset+2]
                self.assertEqual(pair,[pair[0],pair[0]+1],tag)


class RecoveryAfterFailureTest(_ConcurrencyCase):
    def test_lock_released_after_every_failure_type(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("a",1),1)
        # ValueError from the corrupt chain, TypeError from an unstorable
        # value, VersionConflictError from a stale condition: after each one
        # the coordination is free again and a normal write succeeds.
        good=(self.root/"versions.jsonl").read_bytes()
        (self.root/"versions.jsonl").write_bytes(good+b"garbage\n")
        with self.assertRaises(ValueError):
            vault.put("b",2)
        (self.root/"versions.jsonl").write_bytes(good)
        self.assertEqual(vault.put("b",2),2)
        with self.assertRaises(TypeError):
            vault.put("c",{1:"x"})
        self.assertEqual(vault.put("c",3),3)
        with self.assertRaises(VersionConflictError):
            vault.put_if_version("a",99,4)
        self.assertEqual(vault.put_if_version("a",1,4),4)
        self.assertEqual(vault.active_version("a"),4)
        self.assertEqual([r["version"] for r in vault.history()],[1,2,3,4])

    def test_failed_conditional_writes_nothing_and_consumes_no_version(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("k","v1"),1)
        before=(self.root/"versions.jsonl").read_bytes()
        snapshot=vault.versions()
        with self.assertRaises(VersionConflictError) as ctx:
            vault.put_if_version("k",7,"nope")
        self.assertEqual(ctx.exception.actual_version,1)
        self.assertEqual((self.root/"versions.jsonl").read_bytes(),before)
        self.assertEqual(vault.versions(),snapshot)
        self.assertEqual(vault.put("k","v2"),2)


class CliRegressionTest(_ConcurrencyCase):
    def run_cli(self,*args):
        return subprocess.run(
            [sys.executable,str(Path(__file__).resolve().parent.parent/"app.py"),
             "--root",str(self.root),*args],
            capture_output=True,text=True)
    def test_put_get_versions_cycle(self):
        result=self.run_cli("put","--name","k","--value-json",'{"a":[1,2]}')
        self.assertEqual((result.returncode,result.stdout.strip()),(0,"1"))
        result=self.run_cli("put","--name","k","--value","plain")
        self.assertEqual((result.returncode,result.stdout.strip()),(0,"2"))
        result=self.run_cli("get","--name","k")
        self.assertEqual((result.returncode,result.stdout.strip()),(0,"plain"))
        result=self.run_cli("get","--name","k","--version","1","--json")
        self.assertEqual(json.loads(result.stdout),{"a":[1,2]})
        result=self.run_cli("put","--name","k","--if-version","2","--value","newer")
        self.assertEqual((result.returncode,result.stdout.strip()),(0,"3"))
        result=self.run_cli("put","--name","k","--if-version","2","--value","lost")
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout,"")
        result=self.run_cli("active","--name","k")
        self.assertEqual((result.returncode,result.stdout.strip()),(0,"3"))
        result=self.run_cli("versions")
        self.assertEqual(json.loads(result.stdout),
                         [{"name":"k","version":3,"value":"newer"}])
        result=self.run_cli("history","--name","k")
        self.assertEqual([r["version"] for r in json.loads(result.stdout)],[1,2,3])
        result=self.run_cli("snapshot","--version","1")
        self.assertEqual(json.loads(result.stdout),
                         [{"name":"k","version":1,"value":{"a":[1,2]}}])
        result=self.run_cli("diff","--from-version","1","--to-version","3")
        diff=json.loads(result.stdout)
        self.assertEqual([d["name"] for d in diff],["k"])
        self.assertEqual(diff[0]["from"]["value"],{"a":[1,2]})
        self.assertEqual(diff[0]["to"]["value"],"newer")


if __name__=="__main__":
    unittest.main()
