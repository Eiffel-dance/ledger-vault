import json, threading, unittest
from pathlib import Path
from app import VersionedVault

class _ViewCase(unittest.TestCase):
    def setUp(self):
        self._tmp=__import__("tempfile").TemporaryDirectory()
        self.root=Path(self._tmp.name)/"vault"
        self.vault=VersionedVault(self.root)
    def tearDown(self):
        self._tmp.cleanup()
    def log_bytes(self):
        return (self.root/"versions.jsonl").read_bytes()
    def seed(self):
        # a@1, b@2, a@3: history, per-name versions and Unicode ordering all
        # have something to work with.
        self.assertEqual(self.vault.put("a",{"v":1}),1)
        self.assertEqual(self.vault.put("b",[1,2]),2)
        self.assertEqual(self.vault.put("a",{"v":3}),3)

class EmptyViewTest(_ViewCase):
    def test_empty_vault_view_is_usable(self):
        view=self.vault.read_view()
        self.assertEqual(view.versions(),[])
        self.assertEqual(view.history(),[])
        self.assertEqual(view.snapshot_at(0),[])
        self.assertEqual(view.snapshot_at(),[])
        self.assertEqual(view.diff_at(0,0),[])
        with self.assertRaises(KeyError): view.get("anything")
        with self.assertRaises(KeyError): view.active_version("anything")
        # point beyond zero records is still out of range
        with self.assertRaises(ValueError): view.snapshot_at(1)

class ReadParityTest(_ViewCase):
    def test_every_read_matches_the_vault_at_capture(self):
        self.seed()
        view=self.vault.read_view()
        self.assertEqual(view.get("a"),self.vault.get("a"))
        self.assertEqual(view.get("a"),{"v":3})
        self.assertEqual(view.get("a",version=1),{"v":1})
        self.assertEqual(view.get("b"),[1,2])
        self.assertEqual(view.active_version("a"),3)
        self.assertEqual(view.versions(),self.vault.versions())
        self.assertEqual(view.history(),self.vault.history())
        self.assertEqual(view.history("a"),self.vault.history("a"))
        for point in (None,0,1,2,3):
            self.assertEqual(view.snapshot_at(point),self.vault.snapshot_at(point))
        for f,t in ((0,0),(0,1),(1,3),(0,3),(2,3),(3,3)):
            self.assertEqual(view.diff_at(f,t),self.vault.diff_at(f,t))

    def test_unicode_name_ordering_and_filter(self):
        self.vault.put("名前",1)
        self.vault.put("z",2)
        self.vault.put("東",3)
        self.vault.put("abc",4)
        view=self.vault.read_view()
        self.assertEqual([r["name"] for r in view.versions()],
                         [r["name"] for r in self.vault.versions()])
        self.assertEqual([r["name"] for r in view.versions()],
                         sorted(["名前","z","東","abc"]))
        self.assertEqual([r["version"] for r in view.history("名前")],[1])

    def test_diff_from_to_structure_and_deep_copies(self):
        self.seed()
        view=self.vault.read_view()
        diff=view.diff_at(1,3)
        self.assertEqual([d["name"] for d in diff],["a","b"])
        for entry in diff:
            self.assertEqual(set(entry),{"name","from","to"})
            for side in ("from","to"):
                if entry[side] is not None:
                    self.assertEqual(set(entry[side]),{"version","value"})
        a_change=next(d for d in diff if d["name"]=="a")
        self.assertEqual(a_change["from"],{"version":1,"value":{"v":1}})
        self.assertEqual(a_change["to"],{"version":3,"value":{"v":3}})
        b_added=next(d for d in diff if d["name"]=="b")
        self.assertIsNone(b_added["from"])
        self.assertEqual(b_added["to"],{"version":2,"value":[1,2]})

    def test_results_are_independent_deep_copies(self):
        self.seed()
        view=self.vault.read_view()
        got=view.get("a"); got["v"]=999
        self.assertEqual(view.get("a"),{"v":3})
        snap=view.snapshot_at(2); snap[0]["value"]["v"]=999
        self.assertEqual(view.snapshot_at(2),self.vault.snapshot_at(2))
        d=view.diff_at(0,3); d[0]["to"]["value"]="hacked"
        self.assertEqual(view.get("a"),{"v":3})
        # two reads of the same point do not share object identity
        self.assertIsNot(view.get("b"),view.get("b"))

class ErrorParityTest(_ViewCase):
    def test_key_errors_match_existing_reads(self):
        self.seed()
        view=self.vault.read_view()
        with self.assertRaises(KeyError): view.get("missing")
        with self.assertRaises(KeyError): view.active_version("missing")
        with self.assertRaises(KeyError): view.get("a",version=2)   # version 2 is "b"
        with self.assertRaises(KeyError): view.get("b",version=1)
        with self.assertRaises(KeyError): view.get("a",version=9)
        # an unknown name's history is an empty filter result, not an error
        self.assertEqual(view.history("missing"),[])
        self.assertEqual(self.vault.history("missing"),[])

    def test_get_version_validation_matches(self):
        self.seed()
        view=self.vault.read_view()
        for bad in (0,-1,True,False,1.5,"1"):
            with self.assertRaises(ValueError,msg=repr(bad)):
                view.get("a",bad)

    def test_snapshot_validation_matches(self):
        self.seed()
        view=self.vault.read_view()
        with self.assertRaises(ValueError): view.snapshot_at(4)   # beyond 3 captured
        for bad in (-1,True,False,1.5,"2"):
            with self.assertRaises(ValueError,msg=repr(bad)):
                view.snapshot_at(bad)

    def test_diff_validation_matches(self):
        self.seed()
        view=self.vault.read_view()
        with self.assertRaises(ValueError): view.diff_at(2,4)     # to beyond captured
        with self.assertRaises(ValueError): view.diff_at(3,2)     # reversed
        with self.assertRaises(ValueError): view.diff_at(-1,2)
        with self.assertRaises(ValueError): view.diff_at(2,-1)
        for bad in (True,False,1.0,"2"):
            with self.assertRaises(ValueError,msg=repr(bad)): view.diff_at(bad,3)
            with self.assertRaises(ValueError,msg=repr(bad)): view.diff_at(0,bad)

class FrozenSemanticsTest(_ViewCase):
    def test_same_instance_writes_are_invisible(self):
        self.seed()
        view=self.vault.read_view()
        self.assertEqual(self.vault.put("a",{"v":4}),4)
        self.assertEqual(self.vault.put("c",5),5)
        self.assertEqual(view.get("a"),{"v":3})
        self.assertEqual(view.active_version("a"),3)
        self.assertEqual([r["name"] for r in view.versions()],["a","b"])
        with self.assertRaises(KeyError): view.get("c")
        self.assertEqual([r["version"] for r in view.history()],[1,2,3])
        self.assertEqual(view.snapshot_at(),view.snapshot_at(3))
        with self.assertRaises(ValueError): view.snapshot_at(4)
        self.assertEqual(view.diff_at(0,3),self.vault.diff_at(0,3))
        with self.assertRaises(ValueError): view.diff_at(0,4)
        # the live vault moved; the view did not
        self.assertEqual(self.vault.get("a"),{"v":4})
        self.assertEqual(self.vault.active_version("a"),4)

    def test_batch_and_conditional_writes_are_invisible(self):
        self.seed()
        view=self.vault.read_view()
        self.assertEqual(self.vault.put_batch([["d",1],["e",2]]),[4,5])
        self.assertEqual(self.vault.put_if_version("a",3,{"v":6}),6)
        self.assertEqual(self.vault.put_batch_if_versions([["g",7]],{"g":0}),[7])
        self.assertEqual(len(view.history()),3)
        for name in ("d","e","g"):
            with self.assertRaises(KeyError,msg=name): view.get(name)
        self.assertEqual(view.snapshot_at(3)[0]["name"],"a")

    def test_other_instance_writes_and_reload_are_invisible(self):
        self.seed()
        view=self.vault.read_view()
        other=VersionedVault(self.root)
        other.put("a",{"v":90})
        other.put_batch([["x",1],["y",2]])
        # reload the capturing instance onto the newer chain: view must stay
        self.assertEqual(self.vault.reload(),4)
        self.assertEqual(self.vault.active_version("a"),4)
        self.assertEqual(view.get("a"),{"v":3})
        self.assertEqual(view.active_version("a"),3)
        self.assertEqual([r["version"] for r in view.history()],[1,2,3])
        self.assertEqual(len(view.versions()),2)
        self.assertEqual(view.snapshot_at(3),[
            {"name":"a","version":3,"value":{"v":3}},
            {"name":"b","version":2,"value":[1,2]}])

    def test_stale_instance_view_refuses_to_reload_disk(self):
        # New records appear on disk behind the instance's back.  read_view
        # captures the loaded state only — it must not follow the disk, and a
        # later reload must leave the captured view untouched.
        self.seed()
        view=self.vault.read_view()
        other=VersionedVault(self.root)
        other.put("z",99)
        self.assertEqual(view.get("a"),{"v":3})
        with self.assertRaises(KeyError): view.get("z")
        self.assertEqual(len(view.history()),3)
        self.vault.reload()
        self.assertEqual(self.vault.get("z"),99)
        self.assertEqual(len(view.history()),3)
        with self.assertRaises(KeyError): view.get("z")

    def test_external_corruption_after_capture_does_not_break_view(self):
        self.seed()
        good=self.log_bytes()
        view=self.vault.read_view()
        (self.root/"versions.jsonl").write_bytes(good+b'{"broken":true}\n')
        # view keeps serving the complete captured state with no disk access
        self.assertEqual(view.get("a"),{"v":3})
        self.assertEqual(view.versions(),self.vault.versions())
        self.assertEqual(view.snapshot_at(3)[0],
                         {"name":"a","version":3,"value":{"v":3}})
        self.assertEqual([d["name"] for d in view.diff_at(0,3)],["a","b"])
        # the live vault reload now fails as always; the view is unaffected
        with self.assertRaises(ValueError): self.vault.reload()
        self.assertEqual(view.get("b"),[1,2])

    def test_view_survives_vault_destruction(self):
        self.seed()
        view=self.vault.read_view()
        del self.vault
        import gc; gc.collect()
        self.assertEqual(view.get("a"),{"v":3})
        self.assertEqual(view.active_version("b"),2)
        self.assertEqual([r["version"] for r in view.history()],[1,2,3])
        self.assertEqual(view.snapshot_at(2)[0]["name"],
                         "a")  # at point 2 only a@1, b@2 -> sorted a first
        self.assertEqual(len(view.diff_at(0,3)),2)

    def test_concurrent_writers_never_change_a_view(self):
        self.seed()
        views=[self.vault.read_view() for _ in range(3)]
        errors=[]
        def write():
            try:
                w=VersionedVault(self.root)
                for i in range(30):
                    w.put("n-%d"%i,i)
                    if i%5==0:
                        w.put_batch([["b1",i],["b2",i]])
            except Exception as exc:
                errors.append(exc)
        threads=[threading.Thread(target=write) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(60)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors,[])
        for view in views:
            self.assertEqual([r["version"] for r in view.history()],[1,2,3])
            self.assertEqual(view.get("a"),{"v":3})
            self.assertEqual(len(view.versions()),2)
            with self.assertRaises(KeyError): view.get("n-0")

class CaptureSideEffectTest(_ViewCase):
    def test_capture_does_not_follow_disk_or_create_files(self):
        # An instance with nothing loaded from a still-absent root: capturing a
        # view creates neither directory nor log.
        fresh=Path(self._tmp.name)/"never-read-from-disk"
        w=VersionedVault(fresh)
        self.assertFalse(fresh.exists())
        view=w.read_view()
        self.assertFalse(fresh.exists())
        self.assertEqual(view.versions(),[])
        # Records appended by another process/instance stay invisible to the
        # view and to the instance until an explicit reload: read_view is not
        # one.
        VersionedVault(fresh).put("only",1)
        self.assertEqual(view.versions(),[])
        self.assertEqual(w.versions(),[])
        self.assertEqual(w.read_view().versions(),[])
        self.assertEqual(w.reload(),1)

class ViewSurfaceTest(_ViewCase):
    def test_view_exposes_only_reads(self):
        self.seed()
        view=self.vault.read_view()
        for name in ("get","active_version","versions","history",
                     "snapshot_at","diff_at"):
            self.assertTrue(callable(getattr(view,name)),name)
        for name in ("put","put_batch","put_if_version","put_batch_if_versions",
                     "reload","read_view","audit","root","log","_write_lock"):
            self.assertFalse(hasattr(view,name),name)
        # a second capture is an independent frozen object
        self.vault.put("a",{"v":7})
        newer=self.vault.read_view()
        self.assertEqual(newer.get("a"),{"v":7})
        self.assertEqual(view.get("a"),{"v":3})

if __name__=="__main__":
    unittest.main()
