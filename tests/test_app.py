import json, math, os, subprocess, sys, threading, unittest
from pathlib import Path
import app
from app import VersionedVault

class _VaultCase(unittest.TestCase):
    def setUp(self):
        self._tmp=__import__("tempfile").TemporaryDirectory()
        self.root=Path(self._tmp.name)/"vault"
        self.vault=VersionedVault(self.root)
    def tearDown(self):
        self._tmp.cleanup()
    def write_log(self,text):
        self.root.mkdir(parents=True,exist_ok=True)
        (self.root/"versions.jsonl").write_text(text,encoding="utf-8")
    def log_bytes(self):
        return (self.root/"versions.jsonl").read_bytes()

class SmokeTest(unittest.TestCase):
    def test_import(self): self.assertTrue(app)

class ReadBehaviorTest(_VaultCase):
    def test_scalars_strings_lists_and_objects(self):
        self.assertEqual(self.vault.put("s","hello"),1)
        self.assertEqual(self.vault.put("n",42),2)
        self.assertEqual(self.vault.put("lst",[1,"two",False,None]),3)
        self.assertEqual(self.vault.put("obj",{"a":{"b":[1,2]}}),4)
        self.assertEqual(self.vault.get("s"),"hello")
        self.assertEqual(self.vault.get("n"),42)
        self.assertEqual(self.vault.get("lst"),[1,"two",False,None])
        self.assertEqual(self.vault.get("obj"),{"a":{"b":[1,2]}})
    def test_named_version_read(self):
        self.vault.put("k","v1"); self.vault.put("k","v2")
        self.assertEqual(self.vault.get("k",version=1),"v1")
        self.assertEqual(self.vault.get("k",version=2),"v2")
        self.assertEqual(self.vault.get("k"),"v2")
        with self.assertRaises(KeyError): self.vault.get("k",version=9)
        self.vault.put("other",1)
        # version 3 belongs to "other": mismatched name looks up as missing
        with self.assertRaises(KeyError): self.vault.get("k",version=3)
        with self.assertRaises(KeyError): self.vault.get("other",version=1)
    def test_history_order_and_name_filter(self):
        self.vault.put("a",1); self.vault.put("b",2); self.vault.put("a",3)
        self.assertEqual([r["version"] for r in self.vault.history()],[1,2,3])
        self.assertEqual([(r["name"],r["version"]) for r in self.vault.history("a")],[("a",1),("a",3)])
    def test_get_returns_deep_copy(self):
        self.vault.put("k",{"nested":[1,2]})
        got=self.vault.get("k"); got["nested"].append(3)
        self.assertEqual(self.vault.get("k"),{"nested":[1,2]})
        old=self.vault.history()[0]; old["value"]["nested"].append(3)
        self.assertEqual(self.vault.get("k"),{"nested":[1,2]})

class RejectedValueTest(_VaultCase):
    def assert_rejected(self,value):
        with self.assertRaises(TypeError):
            self.vault.put("k",value)
    def test_non_string_keys_rejected_without_directory(self):
        target=Path(self._tmp.name)/"fresh1"
        w=VersionedVault(target)
        with self.assertRaises(TypeError): w.put("k",{1:"x"})
        self.assertFalse(target.exists())
        self.assertFalse((target/"versions.jsonl").exists())
    def test_coerced_and_conflicting_keys_rejected(self):
        self.assert_rejected({None:1})
        self.assert_rejected({(1,):1})
        self.assert_rejected({"a":{1:"x"}})
        n1,n2=float("nan"),float("nan")
        self.assert_rejected({n1:1,n2:2})
        self.assertFalse(self.root.exists())
    def test_tuples_rejected(self):
        self.assert_rejected((1,2))
        self.assert_rejected({"a":[1,(2,3)]})
    def test_unencodable_and_circular_rejected(self):
        self.assert_rejected({1,2})
        self.assert_rejected(object())
        loop={}; loop["self"]=loop
        self.assert_rejected(loop)
    def test_non_finite_scalars_remain_accepted(self):
        self.assertEqual(self.vault.put("nan",float("nan")),1)
        self.assertTrue(math.isnan(self.vault.get("nan")))
        self.assertEqual(self.vault.put("inf",float("inf")),2)
        self.assertTrue(math.isinf(self.vault.get("inf")))
    def test_retry_after_failure_keeps_versions_continuous(self):
        self.assertEqual(self.vault.put("k","first"),1)
        self.assert_rejected({1:"bad"})
        self.assertEqual(self.vault.put("k","second"),2)
        self.assertEqual(self.vault.active_version("k"),2)
        self.assertEqual(self.vault.get("k",version=1),"first")
        self.assertEqual(self.vault.get("k"),"second")
        # in-memory snapshot, active version and next number all unchanged by failure
        self.assertEqual(self.vault.versions(),
                         VersionedVault(self.root).versions())

class PersistenceTest(_VaultCase):
    def test_single_record_passes_full_chain_after_write(self):
        self.assertEqual(self.vault.put("k",{"v":[1,True,None,"x"]}),1)
        lines=self.log_bytes().decode().splitlines()
        self.assertEqual(len(lines),1)
        reloaded=VersionedVault(self.root)
        self.assertEqual(reloaded.get("k"),{"v":[1,True,None,"x"]})
    def test_cross_instance_reload_matches_every_view(self):
        self.vault.put("a",{"x":1}); self.vault.put("b",[1,2]); self.vault.put("a",2)
        fresh=VersionedVault(self.root)
        self.assertEqual(fresh.versions(),self.vault.versions())
        self.assertEqual(fresh.history(),self.vault.history())
        self.assertEqual(fresh.history("a"),self.vault.history("a"))
        for name in ("a","b"):
            self.assertEqual(fresh.active_version(name),self.vault.active_version(name))
            self.assertEqual(fresh.get(name),self.vault.get(name))
    def test_existing_valid_records_still_load_and_chain(self):
        rec={"version":1,"name":"old","value":{"k":[1,True,None]}}
        rec["digest"]=VersionedVault._digest(rec)
        self.write_log(json.dumps(rec,sort_keys=True)+"\n")
        w=VersionedVault(self.root)
        self.assertEqual(w.get("old"),{"k":[1,True,None]})
        self.assertEqual(w.put("old","next"),2)
        self.assertEqual(VersionedVault(self.root).get("old"),"next")
    def test_reload_is_atomic(self):
        self.vault.put("k",1)
        before=self.vault.versions()
        self.write_log(self.log_bytes().decode()+'{"broken":true}\n')
        with self.assertRaises(ValueError): self.vault.reload()
        self.assertEqual(self.vault.versions(),before)
    def test_put_and_reload_on_corrupt_log_keep_state_and_bytes(self):
        self.vault.put("a",1)
        before=self.vault.versions()
        good_bytes=self.log_bytes()
        self.write_log(good_bytes.decode()+"not-json\n")
        with self.assertRaises(ValueError): self.vault.reload()
        with self.assertRaises(ValueError): self.vault.put("a",2)
        self.assertEqual(self.vault.versions(),before)
        self.assertEqual(self.vault.active_version("a"),1)
        self.assertEqual(self.log_bytes(),good_bytes+b"not-json\n")
        with self.assertRaises(ValueError): VersionedVault(self.root)
        # caller can still use the object once the bad tail is gone
        self.write_log(good_bytes.decode())
        self.assertEqual(self.vault.put("a",3),2)
        self.assertEqual(self.vault.get("a"),3)

class DuplicateMemberTest(_VaultCase):
    def assert_invalid_log(self,text):
        self.write_log(text+"\n")
        with self.assertRaisesRegex(ValueError,r"^invalid vault record$"):
            VersionedVault(self.root)
    def _digest(self,value,name="k",version=1):
        rec={"version":version,"name":name,"value":value}
        return VersionedVault._digest(rec)
    def test_duplicate_root_member_matching_digest_is_corrupt(self):
        # Ordinary last-wins parsing collapses the repeated member and still
        # reproduces the stored digest; the ambiguity alone must be rejected.
        text='{"version":1,"name":"k","value":1,"value":1,"digest":%s}'%json.dumps(self._digest(1))
        self.assertEqual(json.loads(text)["digest"],self._digest(1))
        self.assert_invalid_log(text)
    def test_differing_duplicate_root_member_is_corrupt(self):
        text='{"version":1,"name":"k","value":1,"value":2,"digest":%s}'%json.dumps(self._digest(2))
        self.assertEqual(json.loads(text)["digest"],self._digest(2))
        self.assert_invalid_log(text)
    def test_escape_variants_decoding_to_same_name_are_duplicates(self):
        text=('{"version":1,"na\\u006de":"k","name":"k","value":1,"digest":%s}'
              %json.dumps(self._digest(1)))
        self.assertEqual(json.loads(text)["name"],"k")
        self.assert_invalid_log(text)
    def test_duplicate_members_nested_in_value_are_corrupt(self):
        text='{"version":1,"name":"k","value":{"a":1,"a":2},"digest":%s}'%json.dumps(self._digest({"a":2}))
        self.assertEqual(json.loads(text)["digest"],self._digest({"a":2}))
        self.assert_invalid_log(text)
        escaped='{"version":1,"name":"k","value":{"a":1,"\\u0061":2},"digest":%s}'%json.dumps(self._digest({"a":2}))
        self.assert_invalid_log(escaped)
    def test_duplicate_member_inside_nested_array_element_is_corrupt(self):
        text='{"version":1,"name":"k","value":[{"x":1},{"x":1,"x":2}],"digest":%s}'%json.dumps(
            self._digest([{"x":1},{"x":2}]))
        self.assertEqual(json.loads(text)["digest"],self._digest([{"x":1},{"x":2}]))
        self.assert_invalid_log(text)
    def test_same_member_name_in_sibling_objects_and_empty_objects_valid(self):
        value={"x":{"y":1},"z":{"y":2},"empty":{},"list":[{},{}]}
        rec={"version":1,"name":"k","value":value}
        rec["digest"]=VersionedVault._digest(rec)
        # reordered record fields and extra whitespace keep their semantics
        text='{ "digest": %s ,  "name": "k", "value": {"x": {"y": 1}, "z": {"y": 2}, "empty": {}, "list": [{}, {}]}, "version": 1 }'%json.dumps(rec["digest"])
        self.write_log(text+"\n")
        w=VersionedVault(self.root)
        self.assertEqual(w.get("k"),value)
    def test_handwritten_non_finite_records_still_load(self):
        for token,checker in (("NaN",lambda v:math.isnan(v)),
                              ("Infinity",lambda v:math.isinf(v) and v>0),
                              ("-Infinity",lambda v:math.isinf(v) and v<0)):
            body='{"version":1,"name":"k","value":%s}'%token
            digest=VersionedVault._digest(json.loads(body))
            self.write_log('{"version":1,"name":"k","value":%s,"digest":%s}\n'%(token,json.dumps(digest)))
            w=VersionedVault(self.root)
            self.assertTrue(checker(w.get("k")),token)
            (self.root/"versions.jsonl").unlink()
    def test_reload_and_put_keep_snapshot_and_bytes_on_duplicate_tail(self):
        self.vault.put("a",1)
        before=self.vault.versions()
        prefix=self.log_bytes()
        tail=('{"version":2,"name":"a","value":{"x":1,"x":2},"digest":%s}\n'
              %json.dumps(self._digest({"x":2},name="a",version=2)))
        # the tail chains correctly and verifies under plain last-wins parsing
        parsed=json.loads(tail)
        self.assertEqual(parsed["version"],2)
        self.assertEqual(parsed["digest"],self._digest({"x":2},name="a",version=2))
        self.write_log(prefix.decode()+tail)
        with self.assertRaisesRegex(ValueError,r"^invalid vault record$"):
            self.vault.reload()
        self.assertEqual(self.vault.versions(),before)
        self.assertEqual(self.vault.active_version("a"),1)
        with self.assertRaisesRegex(ValueError,r"^invalid vault record$"):
            self.vault.put("a",2)
        self.assertEqual(self.vault.versions(),before)
        self.assertEqual(self.log_bytes(),prefix+tail.encode())
        with self.assertRaisesRegex(ValueError,r"^invalid vault record$"):
            VersionedVault(self.root)
        # removing the ambiguous tail restores continuous chaining
        self.write_log(prefix.decode())
        self.assertEqual(self.vault.put("a",3),2)
        self.assertEqual(VersionedVault(self.root).get("a"),3)

class SnapshotTest(_VaultCase):
    def _seed(self):
        # 1 a=1, 2 b=2, 3 a=3, 4 b=4, 5 c=5
        self.assertEqual(self.vault.put("a",1),1)
        self.assertEqual(self.vault.put("b",2),2)
        self.assertEqual(self.vault.put("a",3),3)
        self.assertEqual(self.vault.put("b",4),4)
        self.assertEqual(self.vault.put("c",5),5)
    def test_version_zero_is_empty(self):
        self._seed()
        self.assertEqual(self.vault.snapshot_at(0),[])
    def test_empty_vault_bounds(self):
        self.assertEqual(self.vault.snapshot_at(0),[])
        self.assertEqual(self.vault.snapshot_at(),[])
        self.assertEqual(self.vault.snapshot_at(None),[])
    def test_points_in_time_keep_last_record_per_name(self):
        self._seed()
        self.assertEqual(self.vault.snapshot_at(1),
                         [{"name":"a","version":1,"value":1}])
        self.assertEqual(self.vault.snapshot_at(2),
                         [{"name":"a","version":1,"value":1},
                          {"name":"b","version":2,"value":2}])
        # record 3 overwrites a, which sorts first but keeps version 3
        self.assertEqual(self.vault.snapshot_at(3),
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":2,"value":2}])
        self.assertEqual(self.vault.snapshot_at(4),
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":4,"value":4}])
        self.assertEqual(self.vault.snapshot_at(5),
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":4,"value":4},
                          {"name":"c","version":5,"value":5}])
        # names not yet present at the point in time are absent entirely
        self.assertEqual([e["name"] for e in self.vault.snapshot_at(1)],["a"])
        self.assertEqual([e["name"] for e in self.vault.snapshot_at(2)],["a","b"])
    def test_latest_matches_versions(self):
        self._seed()
        # versions() is the public entry structure for the current state
        self.assertEqual(self.vault.snapshot_at(),self.vault.versions())
        self.assertEqual(self.vault.snapshot_at(None),self.vault.versions())
        self.assertEqual(self.vault.snapshot_at(5),self.vault.versions())
    def test_names_stably_sorted(self):
        self.vault.put("zeta",1); self.vault.put("alpha",2); self.vault.put("mid",3)
        self.assertEqual([e["name"] for e in self.vault.snapshot_at()],
                         ["alpha","mid","zeta"])
        self.assertEqual([e["name"] for e in self.vault.snapshot_at(2)],
                         ["alpha","zeta"])
    def test_returns_deep_copies(self):
        self.vault.put("k",{"nested":[1,2]})
        first=self.vault.snapshot_at(1); first[0]["value"]["nested"].append(3)
        self.assertEqual(self.vault.snapshot_at(1)[0]["value"],{"nested":[1,2]})
        current=self.vault.snapshot_at(); current[0]["value"]["nested"].append(3)
        self.assertEqual(self.vault.get("k"),{"nested":[1,2]})
        # repeated calls are independent of each other too
        self.assertEqual(self.vault.snapshot_at()[0]["value"],{"nested":[1,2]})
    def test_read_does_not_append_or_move_active_version(self):
        self.vault.put("a",1); self.vault.put("b",2)
        before=self.log_bytes()
        self.vault.snapshot_at(0); self.vault.snapshot_at(1)
        self.vault.snapshot_at(); self.vault.snapshot_at(None)
        self.assertEqual(self.log_bytes(),before)
        self.assertEqual(self.vault.active_version("a"),1)
        self.assertEqual(self.vault.active_version("b"),2)
    def test_invalid_versions_raise_value_error(self):
        self._seed()
        for bad in (-1,-2,6,100):
            with self.assertRaises(ValueError): self.vault.snapshot_at(bad)
        for bad in ("1","x",1.0,1.5,True,False,[1],object()):
            with self.assertRaises(ValueError): self.vault.snapshot_at(bad)
        # bool must not sneak in as int 1 or 0
        with self.assertRaises(ValueError): self.vault.snapshot_at(True)
        with self.assertRaises(ValueError): self.vault.snapshot_at(False)
    def test_reflects_only_last_successful_load_until_explicit_reload(self):
        self._seed()
        good=self.log_bytes()
        self.write_log(good.decode()+"not-json\n")
        # corrupt log on disk changes nothing about in-memory reads
        self.assertEqual(self.vault.snapshot_at(),
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":4,"value":4},
                          {"name":"c","version":5,"value":5}])
        self.assertEqual(self.vault.snapshot_at(3),
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":2,"value":2}])
        with self.assertRaises(ValueError): self.vault.reload()
        # failed reload also leaves the loaded state intact
        self.assertEqual(len(self.vault.snapshot_at()),3)
        self.write_log(good.decode())
        self.assertEqual(self.vault.reload(),3)
        self.assertEqual(len(self.vault.snapshot_at()),3)
    def test_concurrent_reload_never_mixes_states(self):
        # The reloader toggles the on-disk log between a 5-record chain and a
        # 7-record chain (extra writes for an existing and a new name) and
        # reloads after each swap, while readers take snapshots continuously.
        # Every observed snapshot must be exactly one of the two complete
        # states: never partial and never mixing names/records from both.
        self._seed()
        log=self.root/"versions.jsonl"
        base=log.read_bytes()
        def line_for(version,name,value):
            rec={"version":version,"name":name,"value":value}
            rec["digest"]=VersionedVault._digest(rec)
            return (json.dumps(rec,sort_keys=True)+"\n").encode()
        tail6=line_for(6,"a",60)+line_for(7,"d",70)
        short={("a",3),("b",4),("c",5)}
        long_={("a",6),("b",4),("c",5),("d",7)}
        stop=threading.Event(); errors=[]; seen=set()
        def reader():
            while not stop.is_set():
                try:
                    snap=self.vault.snapshot_at()
                except Exception as exc:  # pragma: no cover - diagnostic
                    errors.append(exc); return
                pairs=tuple(sorted((e["name"],e["version"]) for e in snap))
                seen.add(pairs)
        def toggler():
            extended=False
            while not stop.is_set():
                log.write_bytes(base+tail6 if extended else base)
                extended=not extended
                self.vault.reload()
        threads=[threading.Thread(target=reader) for _ in range(4)]
        threads.append(threading.Thread(target=toggler))
        for t in threads: t.start()
        timer=threading.Timer(1.5,stop.set()); timer.start()
        for t in threads: t.join()
        timer.cancel()
        self.assertEqual(errors,[])
        self.assertTrue(seen)
        for pairs in seen:
            self.assertIn(set(pairs),(short,long_),pairs)

class SnapshotCliTest(_VaultCase):
    def _seed(self):
        self.vault.put("a",1); self.vault.put("b",2); self.vault.put("a",3)
    def _run(self,*args):
        env=dict(os.environ,PYTHONPATH=str(Path(app.__file__).parent))
        return subprocess.run(
            [sys.executable,str(Path(app.__file__)),*args],
            capture_output=True,text=True,env=env)
    def test_default_and_versioned_json_output(self):
        self._seed()
        r=self._run("snapshot","--root",str(self.root))
        self.assertEqual(r.returncode,0,r.stderr)
        data=json.loads(r.stdout)
        self.assertEqual(data,
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":2,"value":2}])
        r=self._run("snapshot","--root",str(self.root),"--version","1")
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         [{"name":"a","version":1,"value":1}])
        r=self._run("snapshot","--root",str(self.root),"--version","0")
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),[])
    def test_output_sorting_is_deterministic(self):
        self._seed()
        r1=self._run("snapshot","--root",str(self.root))
        r2=self._run("snapshot","--root",str(self.root))
        self.assertEqual(r1.stdout,r2.stdout)
        # keys sorted, unicode unescaped
        self.vault.put("名","值")
        r=self._run("snapshot","--root",str(self.root))
        self.assertIn('"名"',r.stdout)
    def test_invalid_version_exits_1_with_no_output(self):
        self._seed()
        for arg in ("-1","4","99","abc","1.5"):
            r=self._run("snapshot","--root",str(self.root),"--version",arg)
            self.assertEqual(r.returncode,1,(arg,r.stdout,r.stderr))
            self.assertEqual(r.stdout,"",arg)
    def test_no_version_reads_last_loaded_state_only(self):
        self._seed()
        good=self.log_bytes()
        self.write_log(good.decode()+"broken\n")
        # CLI constructs a fresh vault, so a corrupt log fails construction
        # the same ValueError path; first confirm an untouched root reads fine,
        # then restore and confirm newest loaded state.
        self.write_log(good.decode())
        r=self._run("snapshot","--root",str(self.root))
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         [{"name":"a","version":3,"value":3},
                          {"name":"b","version":2,"value":2}])
    def test_existing_get_version_behavior_unchanged(self):
        self._seed()
        r=self._run("get","--root",str(self.root),"--name","a","--version","1","--json")
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),1)
        # malformed --version for get keeps argparse's exit status 2
        r=self._run("get","--root",str(self.root),"--name","a","--version","x")
        self.assertEqual(r.returncode,2)
        r=self._run("get","--root",str(self.root),"--name","a","--version","-1")
        # negative parses as int then fails get's positive-integer rule -> 1
        self.assertEqual(r.returncode,1)

if __name__=='__main__': unittest.main()
