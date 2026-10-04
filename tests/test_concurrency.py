"""Offline concurrency verification for VersionedVault.

Every scenario here uses only the Python standard library and runs
identically on POSIX and on Windows: thread concurrency, cross-process
concurrency, first write into an empty vault, batch commits, contending
conditional writes, writes after exceptions, a killed writer, readers
during commits, and a regression pass over the command line interface.
"""
import json, os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

import app
from app import VersionedVault, VersionConflictError

REPO=Path(__file__).resolve().parent.parent
WORKER=Path(__file__).resolve().parent/"conc_worker.py"
APP=REPO/"app.py"

class ConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self._tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root=Path(self._tmp.name)/"vault"
        self.log=self.root/"versions.jsonl"

    def run_worker(self,*args):
        result=subprocess.run([sys.executable,str(WORKER),*map(str,args)],
                              capture_output=True,text=True,timeout=180)
        return result

    def start_workers(self,arglists):
        procs=[subprocess.Popen([sys.executable,str(WORKER),*map(str,args)],
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
               for args in arglists]
        results=[]
        for proc in procs:
            out,err=proc.communicate(timeout=180)
            results.append((proc.returncode,out,err))
        return results

    def run_threads(self,bodies):
        # Bodies record their own expected outcomes; anything unexpected is
        # collected here and fails the test after every thread is joined.
        errors=[]
        def wrap(body):
            try: body()
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=wrap,args=(body,)) for body in bodies]
        for thread in threads: thread.start()
        for thread in threads:
            thread.join(180)
            self.assertFalse(thread.is_alive(),"thread did not finish (coordination stuck?)")
        self.assertEqual(errors,[])

    def history_versions(self):
        return [r["version"] for r in VersionedVault(self.root).history()]

class ThreadConcurrencyTest(ConcurrencyCase):
    def test_shared_instance_continuous_versions(self):
        vault=VersionedVault(self.root)
        results=[]
        def body(tid):
            for index in range(25):
                results.append(vault.put("t%d-%d"%(tid,index),index))
        self.run_threads([lambda t=t: body(t) for t in range(8)])
        self.assertEqual(sorted(results),list(range(1,201)))
        self.assertEqual(self.history_versions(),list(range(1,201)))

    def test_separate_instances_first_write_race_on_empty_vault(self):
        # The log does not exist when all threads start: the first concurrent
        # creation must still be serialized, with one continuous chain.
        self.assertFalse(self.root.exists())
        results=[]
        def body(tid):
            VersionedVault(self.root).put("first-%d"%tid,tid)
            results.append(True)
        self.run_threads([lambda t=t: body(t) for t in range(8)])
        self.assertEqual(len(results),8)
        self.assertEqual(self.history_versions(),list(range(1,9)))
        self.assertEqual(sorted(r["name"] for r in VersionedVault(self.root).history()),
                         ["first-%d"%t for t in range(8)])

    def test_mixed_put_and_batch_do_not_interleave(self):
        # Plain puts and batches from several threads (each with its own
        # instance): the chain stays continuous and every batch keeps an
        # indivisible consecutive version range.
        def putter(tid):
            vault=VersionedVault(self.root)
            for index in range(40):
                vault.put("p%d-%d"%(tid,index),index)
        def batcher(tid):
            vault=VersionedVault(self.root)
            for round_ in range(10):
                vault.put_batch([["b%d-%d-%d"%(tid,round_,i),i] for i in range(4)])
        bodies=([lambda t=t: putter(t) for t in range(2)]+
                [lambda t=t: batcher(t) for t in range(2)])
        self.run_threads(bodies)
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],list(range(1,161)))
        groups={}
        for record in history:
            parts=record["name"].split("-")
            if parts[0].startswith("b"):
                groups.setdefault(tuple(parts[:2]),[]).append(record["version"])
        self.assertEqual(len(groups),20)
        for key,versions in groups.items():
            self.assertEqual(len(versions),4,key)
            self.assertEqual(max(versions)-min(versions),3,key)

class CrossProcessConcurrencyTest(ConcurrencyCase):
    def test_first_write_race_on_empty_vault(self):
        self.assertFalse(self.root.exists())
        results=self.start_workers([["put",self.root,1,"p%d"%i] for i in range(4)])
        for rc,out,err in results:
            self.assertEqual(rc,0,err)
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],[1,2,3,4])
        self.assertEqual(sorted(r["name"] for r in history),
                         ["p%d-0"%i for i in range(4)])

    def test_mixed_puts_and_batches_across_processes(self):
        arglists=[["put",self.root,10,"w%d"%i] for i in range(3)]
        arglists+=[["batch",self.root,5,3,"b%d"%i] for i in range(2)]
        results=self.start_workers(arglists)
        for rc,out,err in results:
            self.assertEqual(rc,0,err)
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],list(range(1,61)))
        groups={}
        for record in history:
            parts=record["name"].split("-")
            if parts[0].startswith("b"):
                groups.setdefault(tuple(parts[:2]),[]).append(record["version"])
        self.assertEqual(len(groups),10)
        for key,versions in groups.items():
            self.assertEqual(len(versions),3,key)
            self.assertEqual(max(versions)-min(versions),2,key)

class ConditionalContentionTest(ConcurrencyCase):
    def test_contending_threads_at_most_one_appends(self):
        base=VersionedVault(self.root)
        self.assertEqual(base.put("shared","base"),1)
        outcomes=[]
        def body(tid):
            vault=VersionedVault(self.root)
            before=vault.versions()
            try:
                outcomes.append(("ok",vault.put_if_version("shared",1,"t%d"%tid)))
            except VersionConflictError as exc:
                outcomes.append(("conflict",exc.name,exc.expected_version,exc.actual_version))
                # a failed call must not have touched the caller's in-memory state
                self.assertEqual(vault.versions(),before)
        self.run_threads([lambda t=t: body(t) for t in range(6)])
        wins=[o for o in outcomes if o[0]=="ok"]
        losses=[o for o in outcomes if o[0]=="conflict"]
        self.assertEqual(wins,[("ok",2)])
        self.assertEqual(len(losses),5)
        for _,name,expected,actual in losses:
            self.assertEqual((name,expected,actual),("shared",1,2))
        # the losers wrote no bytes and consumed no version numbers
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],[1,2])
        self.assertEqual(base.get("shared",version=1),"base")

    def test_contending_processes_at_most_one_appends(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("shared","base"),1)
        results=self.start_workers([["cond",self.root,"shared",1,"w%d"%i] for i in range(8)])
        wins=[r for r in results if r[0]==0]
        losses=[r for r in results if r[0]==3]
        self.assertEqual(len(wins),1,[r[2] for r in results if r[0] not in (0,3)])
        self.assertEqual(len(losses),7)
        for rc,out,err in losses:
            # the conflict reports the active version actually on disk
            self.assertEqual(json.loads(out),2)
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],[1,2])
        self.assertEqual(VersionedVault(self.root).active_version("shared"),2)

    def test_conditional_miss_on_fresh_root_creates_nothing(self):
        self.assertFalse(self.root.exists())
        results=self.start_workers([["cond",self.root,"ghost",1,"x"] for _ in range(4)])
        for rc,out,err in results:
            self.assertEqual(rc,3,err)
            self.assertEqual(json.loads(out),None)
        # no directory, no log, no version consumed
        self.assertFalse(self.root.exists())

class ConditionalBatchContentionTest(ConcurrencyCase):
    def test_contending_threads_at_most_one_batch_commits(self):
        base=VersionedVault(self.root)
        self.assertEqual(base.put("shared","base"),1)
        outcomes=[]
        def body(tid):
            vault=VersionedVault(self.root)
            before=vault.versions()
            try:
                versions=vault.put_batch_if_versions(
                    [["shared","t%d"%tid],["new-%d"%tid,tid]],
                    {"shared":1,"new-%d"%tid:0})
                outcomes.append(("ok",versions))
            except VersionConflictError as exc:
                outcomes.append(("conflict",exc.name,exc.expected_version,
                                 exc.actual_version))
                # a failed call must not have touched the caller's snapshot
                self.assertEqual(vault.versions(),before)
        self.run_threads([lambda t=t: body(t) for t in range(6)])
        wins=[o for o in outcomes if o[0]=="ok"]
        losses=[o for o in outcomes if o[0]=="conflict"]
        self.assertEqual(len(wins),1,outcomes)
        # the single winning batch occupies one consecutive version range
        self.assertEqual(wins[0][1],[2,3])
        self.assertEqual(len(losses),5)
        for _,name,expected,actual in losses:
            # every loser based itself on "shared"==1 and saw it at version 2
            self.assertEqual(name,"shared")
            self.assertEqual((expected,actual),(1,2))
        # losers wrote no bytes and consumed no version numbers; the winner's
        # two records stayed consecutive
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],[1,2,3])
        self.assertEqual(history[1]["name"],"shared")
        self.assertTrue(history[2]["name"].startswith("new-"))

    def test_contending_processes_at_most_one_batch_commits(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("shared","base"),1)
        arglists=[["batch-if",self.root,
                   json.dumps([["shared","w%d"%i],["new-%d"%i,i]]),
                   json.dumps({"shared":1,"new-%d"%i:0})]
                  for i in range(8)]
        results=self.start_workers(arglists)
        wins=[r for r in results if r[0]==0]
        losses=[r for r in results if r[0]==3]
        self.assertEqual(len(wins),1,[r[2] for r in results if r[0] not in (0,3)])
        self.assertEqual(json.loads(wins[0][1]),[2,3])
        self.assertEqual(len(losses),7)
        for rc,out,err in losses:
            name,expected,actual=json.loads(out)
            self.assertEqual((name,expected,actual),("shared",1,2))
        history=VersionedVault(self.root).history()
        self.assertEqual([r["version"] for r in history],[1,2,3])
        self.assertEqual(VersionedVault(self.root).active_version("shared"),2)

    def test_all_zero_contention_on_empty_vault_creates_nothing_for_losers(self):
        self.assertFalse(self.root.exists())
        # Only one of the contending batches can create both names; every
        # other process finds the names taken and exits 3 without a directory
        # ever existing on its miss path.
        arglists=[["batch-if",self.root,
                   json.dumps([["a",i],["b",i]]),
                   json.dumps({"a":0,"b":0})]
                  for i in range(6)]
        results=self.start_workers(arglists)
        wins=[r for r in results if r[0]==0]
        losses=[r for r in results if r[0]==3]
        self.assertEqual(len(wins),1,[r[2] for r in results if r[0] not in (0,3)])
        self.assertEqual(json.loads(wins[0][1]),[1,2])
        self.assertEqual(len(losses),5)
        for rc,out,err in losses:
            name,expected,actual=json.loads(out)
            # every batch lists "a" first, and once any batch committed both
            # names, the first name to mismatch is always "a" at version 1
            self.assertEqual((name,expected,actual),("a",0,1))
        history=VersionedVault(self.root).history()
        self.assertEqual([(r["version"],r["name"]) for r in history],
                         [(1,"a"),(2,"b")])

class RecoveryTest(ConcurrencyCase):
    def test_coordination_released_after_every_failure_kind(self):
        vault=VersionedVault(self.root)
        self.assertEqual(vault.put("a",1),1)
        with self.assertRaises(VersionConflictError):
            vault.put_if_version("a",99,2)
        with self.assertRaises(TypeError):
            vault.put("b",{1:"x"})
        with self.assertRaises(ValueError):
            vault.put_batch([["b",2],["b",3]])
        good=self.log.read_bytes()
        self.log.write_bytes(good+b"not-json\n")
        with self.assertRaises(ValueError):
            vault.put("c",3)
        with self.assertRaises(ValueError):
            vault.reload()
        self.log.write_bytes(good)
        # every failure released the coordination: this instance keeps working
        self.assertEqual(vault.put("c",3),2)
        # and so does a separate process
        result=self.run_worker("put",self.root,1,"other")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.history_versions(),[1,2,3])

    def test_killed_writer_releases_coordination(self):
        proc=subprocess.Popen([sys.executable,str(WORKER),"loop",self.root,"killed"],
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.time()+60
            while time.time()<deadline:
                if self.log.exists() and self.log.stat().st_size>0:
                    break
                time.sleep(0.05)
            else:
                self.fail("writer never produced a record")
        finally:
            proc.kill()
            proc.wait(timeout=30)
        # A later call must not block forever.  If the kill landed mid-commit
        # and left an incomplete record, the existing corruption rule applies
        # instead: ValueError("invalid vault record"), never a hang.
        result=self.run_worker("put",self.root,1,"after")
        if result.returncode!=0:
            self.assertIn("invalid vault record",result.stderr)

class ReaderConsistencyTest(ConcurrencyCase):
    def test_reload_only_reads_complete_commits_in_process(self):
        writer=VersionedVault(self.root)
        reader=VersionedVault(self.root)
        done=threading.Event()
        errors=[]
        def write():
            for round_ in range(15):
                writer.put_batch([["w-%d-%d"%(round_,i),"x"*1500] for i in range(10)])
        def read():
            while not done.is_set():
                try:
                    reader.reload()
                except Exception as exc:
                    errors.append(exc)
                    return
        reader_thread=threading.Thread(target=read)
        writer_thread=threading.Thread(target=write)
        reader_thread.start(); writer_thread.start()
        writer_thread.join(180)
        self.assertFalse(writer_thread.is_alive())
        done.set(); reader_thread.join(180)
        self.assertEqual(errors,[])
        self.assertEqual([r["version"] for r in reader.history()],list(range(1,151)))

    def test_reload_only_reads_complete_commits_across_processes(self):
        proc=subprocess.Popen([sys.executable,str(WORKER),"batch",str(self.root),"8","10","pw"],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        reader=VersionedVault(self.root)
        errors=[]
        while proc.poll() is None:
            try:
                reader.reload()
            except Exception as exc:
                errors.append(exc)
                break
        out,err=proc.communicate(timeout=180)
        self.assertEqual(proc.returncode,0,err)
        self.assertEqual(errors,[])
        self.assertEqual(reader.reload(),80)
        self.assertEqual([r["version"] for r in reader.history()],list(range(1,81)))

class AuditConsistencyTest(ConcurrencyCase):
    def test_audit_only_reads_complete_commits_in_process(self):
        writer=VersionedVault(self.root)
        done=threading.Event()
        errors=[]
        def write():
            for round_ in range(15):
                writer.put_batch([["w-%d-%d"%(round_,i),"x"*1500] for i in range(10)])
        def read():
            while not done.is_set():
                try:
                    report=VersionedVault.audit(self.root)
                except Exception as exc:
                    errors.append(exc)
                    return
                # Every report describes one complete chain: the highest
                # version equals the record count and is a multiple of the
                # batch size seen so far — never a mid-batch ordinal.
                if (report["record_count"]!=report["last_version"]
                        or report["last_version"]%10!=0):
                    errors.append(AssertionError("torn chain: %r"%report))
                    return
        reader_thread=threading.Thread(target=read)
        writer_thread=threading.Thread(target=write)
        reader_thread.start(); writer_thread.start()
        writer_thread.join(180)
        self.assertFalse(writer_thread.is_alive())
        done.set(); reader_thread.join(180)
        self.assertEqual(errors,[])
        self.assertEqual(VersionedVault.audit(self.root),
                         {"record_count":150,"active_names":150,"last_version":150})

    def test_verify_during_cross_process_writes_never_reports_torn_chain(self):
        proc=subprocess.Popen([sys.executable,str(WORKER),"batch",str(self.root),"8","10","pw"],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        errors=[]
        while proc.poll() is None:
            r=subprocess.run([sys.executable,str(APP),"--root",str(self.root),"verify"],
                             capture_output=True,text=True,timeout=60)
            if r.returncode==0:
                report=json.loads(r.stdout)
                if report["record_count"]!=report["last_version"] or report["last_version"]%10!=0:
                    errors.append("torn chain: %r"%report)
                    break
            else:
                # A status 1 here would mean the audit saw a torn record —
                # the coordination must make that impossible.
                errors.append("unexpected status %d: %s"%(r.returncode,r.stderr))
                break
        out,err=proc.communicate(timeout=180)
        self.assertEqual(proc.returncode,0,err)
        self.assertEqual(errors,[])
        r=subprocess.run([sys.executable,str(APP),"--root",str(self.root),"verify"],
                         capture_output=True,text=True,timeout=60)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         {"record_count":80,"active_names":80,"last_version":80})

class WindowsBranchSimulationTest(ConcurrencyCase):
    # The msvcrt branch of the lock helpers normally only executes on
    # Windows.  Loading app.py under a fake os.name=="nt" with a fake msvcrt
    # (backed by fcntl, so contention with the real implementation is real)
    # exercises that branch — its far-offset seek, its retry loop and its
    # unlock — on any POSIX machine as well.
    @unittest.skipIf(os.name=="nt","simulation of the Windows branch only runs on POSIX")
    def test_msvcrt_branch_under_genuine_contention(self):
        import errno, fcntl, importlib.util, types
        fake_msvcrt=types.ModuleType("msvcrt")
        fake_msvcrt.LK_NBLCK=1; fake_msvcrt.LK_UNLCK=0
        def locking(fd,mode,nbytes):
            if mode==fake_msvcrt.LK_NBLCK:
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except OSError:
                    raise OSError(errno.EACCES,"region locked by another process")
            else:
                fcntl.flock(fd,fcntl.LOCK_UN)
        fake_msvcrt.locking=locking
        fake_os=types.ModuleType("os")
        fake_os.__dict__.update(os.__dict__)
        fake_os.name="nt"
        saved={name:sys.modules.get(name) for name in ("os","msvcrt")}
        sys.modules["os"]=fake_os; sys.modules["msvcrt"]=fake_msvcrt
        try:
            spec=importlib.util.spec_from_file_location("app_simulated_nt",APP)
            module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            for name,module_ref in saved.items():
                if module_ref is None: del sys.modules[name]
                else: sys.modules[name]=module_ref
        vault=module.VersionedVault(self.root)
        # a real flock-based writer hammers the same root from another
        # process while the simulated Windows branch writes here
        proc=subprocess.Popen([sys.executable,str(WORKER),"loop",str(self.root),"ext"],
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for index in range(30):
                vault.put("sim-%d"%index,index)
        finally:
            proc.kill()
            proc.wait(timeout=30)
        history=VersionedVault(self.root).history()
        versions=[r["version"] for r in history]
        self.assertEqual(versions,list(range(1,len(versions)+1)))
        self.assertTrue({"sim-%d"%i for i in range(30)}<={r["name"] for r in history})

class CommandLineRegressionTest(ConcurrencyCase):
    def cli(self,*args):
        return subprocess.run([sys.executable,str(APP),"--root",str(self.root),*args],
                              capture_output=True,text=True,timeout=60)

    def test_commands_outputs_and_exit_statuses(self):
        r=self.cli("put","--name","a","--value-json",'{"x": 1}')
        self.assertEqual((r.returncode,r.stdout),(0,"1\n"),r.stderr)
        r=self.cli("put","--name","b","--value","hello")
        self.assertEqual((r.returncode,r.stdout),(0,"2\n"),r.stderr)
        r=self.cli("put","--name","a","--if-version","1","--value-json","[1,2]")
        self.assertEqual((r.returncode,r.stdout),(0,"3\n"),r.stderr)
        # stale condition: status 1, nothing appended
        r=self.cli("put","--name","a","--if-version","1","--value","z")
        self.assertEqual((r.returncode,r.stdout), (1,""))
        # conditional put on a missing name: status 1, no record
        r=self.cli("put","--name","ghost","--if-version","1","--value","z")
        self.assertEqual((r.returncode,r.stdout),(1,""))
        r=self.cli("get","--name","a","--json")
        self.assertEqual((r.returncode,r.stdout),(0,"[1, 2]\n"))
        r=self.cli("get","--name","a","--version","1","--json")
        self.assertEqual((r.returncode,r.stdout),(0,'{"x": 1}\n'))
        r=self.cli("active","--name","a")
        self.assertEqual((r.returncode,r.stdout),(0,"3\n"))
        r=self.cli("versions")
        self.assertEqual(r.returncode,0)
        self.assertEqual([(v["name"],v["version"]) for v in json.loads(r.stdout)],
                         [("a",3),("b",2)])
        r=self.cli("history","--name","a")
        self.assertEqual([v["version"] for v in json.loads(r.stdout)],[1,3])
        r=self.cli("snapshot","--version","1")
        self.assertEqual([(v["name"],v["version"]) for v in json.loads(r.stdout)],[("a",1)])
        r=self.cli("diff","--from-version","1","--to-version","3")
        self.assertEqual(r.returncode,0)
        self.assertEqual([d["name"] for d in json.loads(r.stdout)],["a","b"])
        # failures: unified status 1, argparse type error status 2
        self.assertEqual(self.cli("get","--name","missing").returncode,1)
        self.assertEqual(self.cli("get","--name","a","--version","abc").returncode,2)
        self.assertEqual(self.cli("put","--name","a","--value","x",
                                  "--value-json","{}").returncode,1)
        self.assertEqual(self.cli("snapshot","--version","-1").returncode,1)
        self.assertEqual(self.cli("diff","--from-version","2").returncode,1)

    def test_batch_if_command_outputs_and_failures(self):
        r=self.cli("put","--name","a","--value-json","1")
        self.assertEqual((r.returncode,r.stdout),(0,"1\n"),r.stderr)
        # successful conditional batch: one json.loads-able array line
        r=self.cli("batch-if",
                   "--items-json",'[["b", 2], ["a", [1, 2]]]',
                   "--expected-versions-json",'{"b": 0, "a": 1}')
        self.assertEqual((r.returncode,r.stdout),(0,"[2, 3]\n"),r.stderr)
        self.assertEqual(json.loads(r.stdout),[2,3])
        # conflict: status 1, empty stdout, no extra records
        r=self.cli("batch-if",
                   "--items-json",'[["a", 9]]',
                   "--expected-versions-json",'{"a": 1}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        # create-only expectation on a now existing name also fails
        r=self.cli("batch-if",
                   "--items-json",'[["b", 9]]',
                   "--expected-versions-json",'{"b": 0}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        # still only the three records from the successful calls
        self.assertEqual(len(VersionedVault(self.root).history()),3)
        # missing options
        self.assertEqual(self.cli("batch-if",
                                  "--items-json",'[["c",1]]').returncode,1)
        self.assertEqual(self.cli("batch-if",
                                  "--expected-versions-json",'{"c":0}').returncode,1)
        # JSON that is not one complete document
        for bad_items in ('', '[["c",1]', 'nope', '[["c",1]]extra',
                          '[[\"c\",1]] {}'):
            r=self.cli("batch-if","--items-json",bad_items,
                       "--expected-versions-json",'{"c":0}')
            self.assertEqual((r.returncode,r.stdout),(1,""),repr(bad_items))
        for bad_expected in ('', '{"c":', 'nope', '{"c":0} {}'):
            r=self.cli("batch-if","--items-json",'[["c",1]]',
                       "--expected-versions-json",bad_expected)
            self.assertEqual((r.returncode,r.stdout),(1,""),repr(bad_expected))
        # boolean, fractional, negative and string versions are all rejected
        for bad_version in ("true","false","1.0","-1",'"0"','null'):
            r=self.cli("batch-if","--items-json",'[["c",1]]',
                       "--expected-versions-json",'{"c":%s}'%bad_version)
            self.assertEqual((r.returncode,r.stdout),(1,""),bad_version)
        # duplicate names in items, mismatching name sets
        r=self.cli("batch-if","--items-json",'[["c",1],["c",2]]',
                   "--expected-versions-json",'{"c":0}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        r=self.cli("batch-if","--items-json",'[["c",1]]',
                   "--expected-versions-json",'{"c":0,"d":0}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        r=self.cli("batch-if","--items-json",'[["c",1],["d",2]]',
                   "--expected-versions-json",'{"c":0}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        # malformed item shape
        r=self.cli("batch-if","--items-json",'{"c":1}',
                   "--expected-versions-json",'{"c":0}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        r=self.cli("batch-if","--items-json",'[]',
                   "--expected-versions-json",'{}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        # a repeated JSON object member in the mapping is ambiguous: reject
        r=self.cli("batch-if","--items-json",'[["c",1]]',
                   "--expected-versions-json",'{"c":0,"c":1}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        # no failed call created version 4 or name c
        self.assertEqual([r["version"] for r in VersionedVault(self.root).history()],
                         [1,2,3])
        # the corrected call finally succeeds
        r=self.cli("batch-if","--items-json",'[["c", {"ok": true}]]',
                   "--expected-versions-json",'{"c":0}')
        self.assertEqual((r.returncode,r.stdout),(0,"[4]\n"),r.stderr)
        self.assertEqual(json.loads(r.stdout),[4])
        # duplicate members inside a value collapse under plain JSON parsing,
        # exactly as the existing put --value-json rule does
        r=self.cli("batch-if","--items-json",'[["d",{"x":1,"x":2}]]',
                   "--expected-versions-json",'{"d":0}')
        self.assertEqual((r.returncode,r.stdout),(0,"[5]\n"),r.stderr)
        self.assertEqual(VersionedVault(self.root).get("d"),{"x":2})

    def test_batch_if_on_fresh_root_miss_creates_nothing(self):
        fresh=Path(self._tmp.name)/"fresh-batch-if"
        self.assertFalse(fresh.exists())
        r=subprocess.run([sys.executable,str(APP),"--root",str(fresh),"batch-if",
                          "--items-json",'[["a",1]]',
                          "--expected-versions-json",'{"a":1}'],
                         capture_output=True,text=True,timeout=60)
        self.assertEqual((r.returncode,r.stdout),(1,""))
        self.assertFalse(fresh.exists())

    def test_batch_if_rejected_by_verify_option_guard(self):
        # verify still accepts only --root: the new batch-if options fail it
        # the same way every other extra option does
        for extra in (("--items-json",'[]'),
                      ("--expected-versions-json",'{}')):
            r=self.cli("verify",*extra)
            self.assertEqual((r.returncode,r.stdout),(1,""),extra)

    def test_batch_if_on_corrupt_log_exits_1_without_output_or_writes(self):
        vault=VersionedVault(self.root)
        vault.put("a",1)
        before=self.log.read_bytes()
        with self.log.open("a",encoding="utf-8") as f:
            f.write("not-json\n")
        r=self.cli("batch-if","--items-json",'[["a",2]]',
                   "--expected-versions-json",'{"a":1}')
        self.assertEqual((r.returncode,r.stdout),(1,""))
        self.assertEqual(self.log.read_bytes(),before+b"not-json\n")

    def test_verify_outputs_one_ordered_line_and_empty_cases(self):
        fresh=Path(self._tmp.name)/"fresh"
        r=subprocess.run([sys.executable,str(APP),"--root",str(fresh),"verify"],
                         capture_output=True,text=True,timeout=60)
        self.assertEqual((r.returncode,r.stdout),
                         (0,'{"active_names": 0, "last_version": 0, "record_count": 0}\n'),r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         {"record_count":0,"active_names":0,"last_version":0})
        self.assertFalse(fresh.exists())
        # a populated, intact root reports its single complete state
        vault=VersionedVault(self.root)
        vault.put("a",1); vault.put("b",2); vault.put("a",3)
        r=self.cli("verify")
        self.assertEqual((r.returncode,r.stdout),
                         (0,'{"active_names": 2, "last_version": 3, "record_count": 3}\n'))
        # only --root is accepted: any other option fails like existing
        # option-semantics failures (status 1, empty stdout), and an unknown
        # subcommand keeps argparse's status 2
        for extra in (("--name","a"),("--value","x"),("--version","1"),("--json",),
                      ("--if-version","1"),("--from-version","0","--to-version","1"),
                      ("--value-json","1")):
            r=self.cli("verify",*extra)
            self.assertEqual((r.returncode,r.stdout),(1,""),extra)

    def test_verify_corrupt_log_exits_1_without_output_or_writes(self):
        vault=VersionedVault(self.root)
        vault.put("a",1)
        before=self.log.read_bytes()
        with self.log.open("a",encoding="utf-8") as f:
            f.write("not-json\n")
        r=self.cli("verify")
        self.assertEqual((r.returncode,r.stdout),(1,""))
        self.assertEqual(self.log.read_bytes(),before+b"not-json\n")
        # a corrupt log fails every command with status 1 and no partial output
        with self.log.open("a",encoding="utf-8") as f:
            f.write("not-json\n")
        for args in (("versions",),("get","--name","a"),("put","--name","c","--value","1")):
            r=self.cli(*args)
            self.assertEqual((r.returncode,r.stdout),(1,""),args)

if __name__=="__main__": unittest.main()
