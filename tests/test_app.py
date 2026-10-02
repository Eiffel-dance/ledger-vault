import json, math, subprocess, sys, unittest
from pathlib import Path
import app
from app import VersionedVault

APP=Path(app.__file__).resolve()

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

class CliTest(_VaultCase):
    def run_cli(self,*args):
        return subprocess.run([sys.executable,str(APP),"--root",str(self.root),*args],
                              capture_output=True,text=True)
    def cli_get(self,name):
        # read back what the CLI subprocess wrote through a fresh reload
        self.vault.reload()
        return self.vault.get(name)
    def test_put_value_json_round_trips_types(self):
        doc='{"b":[1,2.5,true,false,null],"a":{"x":"y"}}'
        r=self.run_cli("put","--name","cfg","--value-json",doc)
        self.assertEqual(r.returncode,0)
        self.assertEqual(r.stdout.strip(),"1")
        self.assertEqual(self.cli_get("cfg"),{"a":{"x":"y"},"b":[1,2.5,True,False,None]})
        # CLI and Python interface read the same JSON values
        r=self.run_cli("get","--name","cfg","--json")
        self.assertEqual(r.returncode,0)
        self.assertEqual(json.loads(r.stdout),self.cli_get("cfg"))
        self.assertEqual(r.stdout.strip(),
                         json.dumps(self.cli_get("cfg"),ensure_ascii=False,sort_keys=True))
    def test_put_value_json_scalars(self):
        for doc,expected in (("42",42),("3.5",3.5),("true",True),("null",None),
                             ('"s"',"s"),("[1,2]",[1,2])):
            r=self.run_cli("put","--name","k","--value-json",doc)
            self.assertEqual(r.returncode,0,doc)
            self.assertEqual(self.cli_get("k"),expected)
            self.assertIs(type(self.cli_get("k")),type(expected))
    def test_put_value_json_invalid_writes_nothing(self):
        for bad in ('{"a":1','not json','{"a":1} trailing','',"{'a':1}"):
            r=self.run_cli("put","--name","k","--value-json",bad)
            self.assertEqual(r.returncode,1,bad)
            self.assertEqual(r.stdout,"")
        self.assertFalse((self.root/"versions.jsonl").exists())
        self.assertEqual(self.vault.versions(),[])
    def test_put_value_and_value_json_conflict(self):
        r=self.run_cli("put","--name","k","--value","s","--value-json","{}")
        self.assertEqual(r.returncode,1)
        self.assertEqual(r.stdout,"")
        self.assertFalse((self.root/"versions.jsonl").exists())
        # argument order does not matter
        r=self.run_cli("put","--value-json","{}","--name","k","--value","s")
        self.assertEqual(r.returncode,1)
        self.assertFalse((self.root/"versions.jsonl").exists())
    def test_put_plain_value_unchanged(self):
        r=self.run_cli("put","--name","k","--value",'{"a":1}')
        self.assertEqual(r.returncode,0)
        self.assertEqual(r.stdout.strip(),"1")
        self.assertEqual(self.cli_get("k"),'{"a":1}')
        r=self.run_cli("put","--name","n")  # omitted value keeps existing result
        self.assertEqual(r.returncode,0)
        self.assertIsNone(self.cli_get("n"))
    def test_get_json_output_conventions(self):
        self.vault.put("k",{"z":"é","a":[1,None]})
        r=self.run_cli("get","--name","k","--json")
        self.assertEqual(r.returncode,0)
        self.assertEqual(json.loads(r.stdout),{"a":[1,None],"z":"é"})
        self.assertIn("é",r.stdout)  # unescaped Unicode like versions/history
        self.assertLess(r.stdout.find('"a"'),r.stdout.find('"z"'))  # sorted keys
        # default get output unchanged: the plain str() of the value
        r=self.run_cli("get","--name","k")
        self.assertEqual(r.returncode,0)
        self.assertEqual(r.stdout.strip(),str(self.vault.get("k")))
        # --json also applies to a named version read
        self.vault.put("k",2)
        r=self.run_cli("get","--name","k","--version","1","--json")
        self.assertEqual(json.loads(r.stdout),{"a":[1,None],"z":"é"})
    def test_cli_failures_exit_1_without_writes(self):
        self.vault.put("a",1)
        good=self.log_bytes()
        for args in (("get","--name","missing","--json"),
                     ("get","--name","a","--version","9","--json"),
                     ("put","--name","k","--value-json",'{1:"x"}'),
                     ("put","--name","k","--value-json",'{"a":1} extra')):
            r=self.run_cli(*args)
            self.assertEqual(r.returncode,1,args)
            self.assertEqual(r.stdout,"")
            self.assertEqual(self.log_bytes(),good)

if __name__=='__main__': unittest.main()
