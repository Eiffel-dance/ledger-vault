import json, math, unittest
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

class BatchTest(_VaultCase):
    def test_batch_appends_consecutive_versions_in_order(self):
        self.assertEqual(self.vault.put("pre",0),1)
        result=self.vault.put_batch([["a",1],["b",[1,2]],["c",{"x":True}]])
        self.assertEqual(result,[2,3,4])
        self.assertEqual(self.vault.get("a"),1)
        self.assertEqual(self.vault.get("b"),[1,2])
        self.assertEqual(self.vault.get("c"),{"x":True})
        self.assertEqual([r["version"] for r in self.vault.history()],[1,2,3,4])
        self.assertEqual(self.vault.put("post",9),5)
    def test_tuple_container_and_entries_accepted(self):
        self.assertEqual(self.vault.put_batch((("a",1),("b",2))),[1,2])
        self.assertEqual(self.vault.get("b"),2)
    def test_batch_update_keeps_history_and_last_record_wins(self):
        self.vault.put("a","old")
        self.assertEqual(self.vault.put_batch([["a","new"],["b",1],["c","x"]]),[2,3,4])
        self.assertEqual(self.vault.get("a"),"new")
        self.assertEqual(self.vault.get("a",version=1),"old")
        self.assertEqual(self.vault.active_version("a"),2)
        self.assertEqual([(r["name"],r["version"]) for r in self.vault.history("a")],
                         [("a",1),("a",2)])
        self.assertEqual([(v["name"],v["version"]) for v in self.vault.versions()],
                         [("a",2),("b",3),("c",4)])
        fresh=VersionedVault(self.root)
        self.assertEqual(fresh.versions(),self.vault.versions())
        self.assertEqual(fresh.history(),self.vault.history())
    def test_rejected_containers_and_shapes(self):
        for bad in (None,"ab",{"a":1},{1,2},[],(),[["a",1],["b",2]][:0]):
            with self.assertRaises(ValueError,msg=repr(bad)):
                self.vault.put_batch(bad)
        for bad in ([1],[["a"]],[["a",1,2]],["ab"],[("a",)],[(1,2)],[[None,1]],[[1,"x"]],[["",1]]):
            with self.assertRaises(ValueError,msg=repr(bad)):
                self.vault.put_batch(bad)
        self.assertFalse(self.root.exists())
    def test_duplicate_names_rejected(self):
        with self.assertRaises(ValueError):
            self.vault.put_batch([["a",1],["a",2]])
        with self.assertRaises(ValueError):
            self.vault.put_batch([["a",1],["b",2],["a",3]])
        self.assertFalse(self.root.exists())
    def test_unstorable_value_rejects_whole_batch_as_typeerror(self):
        target=Path(self._tmp.name)/"freshbatch"
        w=VersionedVault(target)
        with self.assertRaises(TypeError):
            w.put_batch([["good",1],["bad",{1:"x"}]])
        with self.assertRaises(TypeError):
            w.put_batch([["bad",(1,2)]])
        self.assertFalse(target.exists())
        self.assertEqual(self.vault.put("k","v"),1)
        before_bytes=self.log_bytes(); before=self.vault.versions()
        with self.assertRaises(TypeError):
            self.vault.put_batch([["k2","ok"],["k3",object()]])
        self.assertEqual(self.log_bytes(),before_bytes)
        self.assertEqual(self.vault.versions(),before)
        self.assertEqual(self.vault.put("k2","ok"),2)
    def test_failed_batch_keeps_bytes_state_and_next_version(self):
        self.vault.put("a",1)
        before_bytes=self.log_bytes(); before=self.vault.versions()
        with self.assertRaises(ValueError):
            self.vault.put_batch([["b",2],["b",3]])
        self.assertEqual(self.log_bytes(),before_bytes)
        self.assertEqual(self.vault.versions(),before)
        self.assertEqual(self.vault.put("b",2),2)
    def test_corrupt_log_blocks_batch_without_writes_or_state_change(self):
        self.vault.put("a",1)
        before=self.vault.versions(); good=self.log_bytes()
        self.write_log(good.decode()+"not-json\n")
        with self.assertRaisesRegex(ValueError,r"^invalid vault record$"):
            self.vault.put_batch([["b",2],["c",3]])
        self.assertEqual(self.vault.versions(),before)
        self.assertEqual(self.log_bytes(),good+b"not-json\n")
        self.write_log(good.decode())
        self.assertEqual(self.vault.put_batch([["b",2],["c",3]]),[2,3])
    def test_batch_records_match_existing_format_and_digest(self):
        self.vault.put_batch([["a",{"k":[1,None]}],["b","x"]])
        for line,version in zip(self.log_bytes().decode().splitlines(),(1,2)):
            item=json.loads(line)
            self.assertEqual(set(item),{"version","name","value","digest"})
            self.assertEqual(item["version"],version)
            self.assertEqual(item["digest"],VersionedVault._digest(item))
    def test_batch_isolated_from_concurrent_put(self):
        import threading
        self.vault.put("seed",0)
        barrier=threading.Barrier(2)
        def batch():
            barrier.wait()
            self.vault.put_batch([["b",1],["c",2],["d",3]])
        def single():
            barrier.wait()
            self.vault.put("e",4)
        threads=[threading.Thread(target=batch),threading.Thread(target=single)]
        for t in threads: t.start()
        for t in threads: t.join()
        records=self.vault.history()
        self.assertEqual([r["version"] for r in records],[1,2,3,4,5])
        # the batch occupies three consecutive versions: the single put lands
        # entirely before or after it, never interleaved
        names=[r["name"] for r in records]
        self.assertIn(names[1:],(["b","c","d","e"],["e","b","c","d"]))

if __name__=='__main__': unittest.main()
