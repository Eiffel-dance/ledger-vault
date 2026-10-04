import argparse, contextlib, copy, hashlib, json, math, os, threading
from pathlib import Path

if os.name=="nt":
    import errno, msvcrt, time
else:
    import fcntl

# Cross-platform write coordination, standard library only.  Both branches
# offer the same contract: _lock_file blocks until the caller owns the
# vault-wide coordination and _unlock_file releases it, and the OS drops the
# ownership by itself when the owning process dies, so a crashed writer can
# never block later calls forever.  On POSIX this is a whole-file flock on
# the log itself.  Windows has no flock; msvcrt.locking takes a byte-range
# lock instead, so the lock sits at a fixed offset far beyond any real
# record: the locked range then never overlaps record bytes, which keeps the
# separate append handle (and any uncoordinated reader of the data) free of
# ERROR_LOCK_VIOLATION while still giving every cooperative caller one common
# rendezvous on the same file.  LK_NBLCK is retried because the blocking
# LK_LOCK gives up after about ten seconds.
_LOCK_OFFSET=1<<62
if os.name=="nt":
    def _lock_file(handle):
        handle.seek(_LOCK_OFFSET)
        while True:
            try:
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES,errno.EDEADLK):
                    raise
                time.sleep(0.05)
    def _unlock_file(handle):
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
else:
    def _lock_file(handle):
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
    def _unlock_file(handle):
        fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

@contextlib.contextmanager
def _file_lock(handle):
    # Hold the coordination for the duration of the with-block and release it
    # on every exit path — success, ValueError, TypeError,
    # VersionConflictError and underlying I/O errors alike.  If the lock
    # itself cannot be taken there is nothing to release.
    _lock_file(handle)
    try:
        yield
    finally:
        _unlock_file(handle)

class VersionConflictError(Exception):
    # Raised by put_if_version when the active version on disk no longer
    # matches the version the caller based its update on.  When the name has
    # never been written, actual_version is None.
    def __init__(self,name,expected_version,actual_version):
        self.name=name
        self.expected_version=expected_version
        self.actual_version=actual_version
        super().__init__(name,expected_version,actual_version)

class _State:
    __slots__=("snapshot","records","by_version")
    def __init__(self,snapshot,records,by_version):
        self.snapshot=snapshot; self.records=records; self.by_version=by_version

class _VaultReader:
    # Read semantics shared by the live vault and by a frozen read view.  A
    # reader works from exactly one published _State on self._state: a tuple of
    # records plus the snapshot and by-version index built from it.  A published
    # _State is never mutated in place — a reload validates a fresh chain and
    # publishes a brand-new _State, leaving the old one intact — so none of
    # these reads takes a lock, and keeping one _State reference freezes a view
    # on that complete chain even while the vault keeps writing, keeps
    # reloading, or is itself discarded.  Nothing here reads or writes disk;
    # every value leaves as a fresh deep copy, so callers can never reach into
    # the captured state through a result.
    __slots__=()
    @staticmethod
    def _is_positive_int(value):
        return isinstance(value,int) and not isinstance(value,bool) and value>0
    @staticmethod
    def _is_nonneg_int(value):
        return isinstance(value,int) and not isinstance(value,bool) and value>=0
    @staticmethod
    def _json_equal(left,right):
        # Structural equality under this vault's JSON semantics: identical
        # container types and scalar values, treating NaN as equal to NaN so
        # an accepted non-finite float still verifies after a reload.  A tuple
        # never equals the list it would reload as, so such a value is rejected.
        if type(left) is not type(right):
            return False
        if isinstance(left,float) and math.isnan(left) and math.isnan(right):
            return True
        if isinstance(left,dict):
            return left.keys()==right.keys() and all(
                _VaultReader._json_equal(left[k],right[k]) for k in left)
        if isinstance(left,list):
            return len(left)==len(right) and all(
                _VaultReader._json_equal(x,y) for x,y in zip(left,right))
        return left==right
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
    def snapshot_at(self,version=None):
        # Read-only whole-repository view as of the first `version` global
        # records: 0 is an empty snapshot, omitting the point (or passing the
        # latest number) reproduces the captured state.  The published _State
        # is captured once and never mutated after publication, so a view stays
        # on its complete chain for its whole lifetime and a concurrent reload
        # of the live vault can only expose a different complete state to that
        # vault, never a mix here.  Nothing here touches disk, the active
        # version or the log, and every returned value is a fresh deep copy.
        state=self._state
        if version is None:
            records=state.records
        else:
            if not isinstance(version,int) or isinstance(version,bool) or version<0:
                raise ValueError("version must be a non-negative integer")
            total=len(state.records)
            if version>total:
                raise ValueError("version must not exceed the number of loaded records")
            records=state.records[:version]
        snapshot={}
        for r in records:
            snapshot[r["name"]]={"version":r["version"],"value":r["value"]}
        return [{"name":k,"version":v["version"],"value":copy.deepcopy(v["value"])}
                for k,v in sorted(snapshot.items())]
    def diff_at(self,from_version,to_version):
        # Deterministic difference between two whole-repository points located
        # by global record ordinal (0 = empty snapshot).  Both bounds must be
        # non-negative ints (booleans rejected), from must not exceed to, and
        # neither may exceed the number of captured records; every violation is
        # a ValueError raised before any result is built, leaving the captured
        # state, memory, disk and the next version number untouched.  The
        # published _State is captured once here and never mutated after
        # publication, so on a frozen view neither a concurrent reload nor
        # external log corruption can mix another state into the result, and
        # nothing touches disk.
        if not self._is_nonneg_int(from_version):
            raise ValueError("from_version must be a non-negative integer")
        if not self._is_nonneg_int(to_version):
            raise ValueError("to_version must be a non-negative integer")
        if from_version>to_version:
            raise ValueError("from_version must not exceed to_version")
        state=self._state
        total=len(state.records)
        if to_version>total:
            raise ValueError("version must not exceed the number of loaded records")
        def point_snapshot(point):
            snapshot={}
            for r in state.records[:point]:
                snapshot[r["name"]]={"version":r["version"],"value":r["value"]}
            return snapshot
        left=point_snapshot(from_version)
        right=point_snapshot(to_version)
        # Unicode codepoint (dictionary) order of the config names.  A name is
        # included only when its active entry differs: a missing side is a real
        # difference, and rewriting an equal value at a new version is one too.
        result=[]
        for name in sorted(set(left)|set(right)):
            a=left.get(name); b=right.get(name)
            if (a is not None and b is not None
                    and a["version"]==b["version"]
                    and self._json_equal(a["value"],b["value"])):
                continue
            result.append({
                "name":name,
                "from":None if a is None else {"version":a["version"],"value":copy.deepcopy(a["value"])},
                "to":None if b is None else {"version":b["version"],"value":copy.deepcopy(b["value"])},
            })
        return result

class VersionedVault(_VaultReader):
    # In-process users of the same root are serialized through one lock per
    # log file, shared by every instance of this process: on some local
    # filesystems the OS file lock only arbitrates between processes, and a
    # Windows byte-range lock is not a reliable mutual exclusion between two
    # handles of the same process either, so threads of one process must never
    # rely on the file lock alone.
    _registry_lock=threading.Lock()
    _write_locks={}
    def __init__(self, root="vault"):
        self.root=Path(root); self.log=self.root/"versions.jsonl"
        self._state=_State({},(),{})
        self._write_lock=self._lock_for_log(self.log)
        self.reload()
    @staticmethod
    def _lock_for_log(log):
        # The process-wide per-log coordination lock shared by every instance
        # (and by the instance-free audit entry) for the same root.
        key=os.path.normcase(os.path.abspath(log))
        with VersionedVault._registry_lock:
            lock=VersionedVault._write_locks.get(key)
            if lock is None:
                lock=threading.RLock()
                VersionedVault._write_locks[key]=lock
        return lock
    @staticmethod
    def _digest(item):
        body={k:item[k] for k in ("version","name","value")}
        return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    @staticmethod
    def _ensure_storable(value):
        # A value is only accepted if it can round-trip through the on-disk
        # JSONL record and digest rules with unchanged JSON semantics.  This is
        # pure and side-effect free so callers can run it before creating any
        # directory or opening the log; every failure is a TypeError.
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            # Circular references surface as ValueError from json; unify them
            # with every other encoding failure as TypeError.
            raise TypeError("value is not JSON serializable")
        stack=[value]
        while stack:
            node=stack.pop()
            if isinstance(node,dict):
                tokens=set()
                for key,item in node.items():
                    # json.dumps silently coerces non-string keys (1 -> "1",
                    # None -> "null", (1,) -> "[1]"), and distinct float NaN
                    # keys even collapse onto one duplicated "NaN" key.  The
                    # reloaded object would differ, so reject up front.
                    if not isinstance(key,str):
                        raise TypeError("object keys must be strings")
                    token=json.dumps(key)
                    if token in tokens:
                        raise TypeError("duplicate object key after JSON encoding")
                    tokens.add(token)
                    stack.append(item)
            elif isinstance(node,(list,tuple)):
                # Tuples share JSON array semantics with lists and reload as
                # lists; descend either way so nested keys are checked too.
                stack.extend(node)
    @staticmethod
    def _object_from_pairs(pairs):
        # object_pairs_hook fires for every JSON object at every depth,
        # including the record itself, and hands us every member before
        # json.loads collapses same-named members onto one dict entry.  Names
        # are already decoded, so "a" and "a" compare equal; two members
        # decoding to the same name make the text ambiguous and corrupt the
        # record, even though last-wins parsing would still reproduce a
        # matching digest.
        seen=set()
        for key,_item in pairs:
            if key in seen:
                raise ValueError("invalid vault record")
            seen.add(key)
        return dict(pairs)
    @staticmethod
    def _parse_record(line,previous):
        try:
            item=json.loads(line,object_pairs_hook=VersionedVault._object_from_pairs)
        except ValueError:
            raise ValueError("invalid vault record")
        if not isinstance(item,dict) or set(item)!={"version","name","value","digest"}:
            raise ValueError("invalid vault record")
        version=item["version"]
        if not VersionedVault._is_positive_int(version) or version!=previous+1:
            raise ValueError("invalid vault record")
        name=item["name"]
        if not isinstance(name,str) or not name:
            raise ValueError("invalid vault record")
        digest=item["digest"]
        if not isinstance(digest,str) or digest!=VersionedVault._digest(item):
            raise ValueError("invalid vault record")
        return {"version":version,"name":name,"value":item["value"]}
    @staticmethod
    def _load_log(log):
        # Pure, side-effect-free read of one complete chain using the exact
        # record format, chaining and digest rules every loader shares.  A
        # missing or empty log is the empty vault; a log that fails UTF-8
        # decoding is as corrupt as a record that fails validation.
        snapshot,records,by_version={},[],{}
        previous=0
        if log.exists():
            try:
                text=log.read_text(encoding="utf-8")
            except ValueError:
                raise ValueError("invalid vault record")
            for line in text.splitlines():
                item=VersionedVault._parse_record(line,previous)
                previous=item["version"]
                records.append(item)
                by_version[item["version"]]=item
                snapshot[item["name"]]={"version":item["version"],"value":item["value"]}
        return _State(snapshot,tuple(records),by_version)
    def reload(self):
        # Build the complete new state first; only replace the snapshot once
        # the whole chain has validated, so readers never see an intermediate
        # state and a corrupt log leaves the previous snapshot untouched.  The
        # read runs under the same coordination as the writers: a reload that
        # arrives while another thread or process is mid-commit waits for the
        # append to finish, so it can only ever read the complete pre-commit
        # or post-commit chain, never half a record or the middle of a batch.
        # A vault without a log is simply empty, and a read never creates a
        # file or directory.
        with self._write_lock:
            if self.log.exists():
                lock_handle=self.log.open("rb")
                try:
                    with _file_lock(lock_handle):
                        state=self._load_log(self.log)
                finally:
                    lock_handle.close()
            else:
                state=self._load_log(self.log)
            self._state=state
            return len(state.snapshot)
    @classmethod
    def audit(cls,root="vault"):
        # Read-only, instance-free integrity audit for maintenance and offline
        # acceptance.  Independently reads versions.jsonl under the same
        # coordination as writers and reload — the shared per-root lock plus
        # the cross-platform file lock — so it always observes one complete
        # pre-commit or post-commit chain, never a torn record or a half-written
        # batch.  Nothing is created, repaired, versioned or snapshotted: no
        # directory, no log, no bytes and no instance state change, and a log
        # already corrupt on disk merely raises without touching anything.  The
        # result reports the single complete state captured when the audit
        # began: the number of valid records, the number of names in the final
        # state, and the highest global version (all zero for an absent root or
        # an empty log).  Any malformed record — JSON syntax error, duplicated
        # member, wrong field set, broken version continuity, illegal name,
        # mismatched digest or failed text decoding — is reported as
        # ValueError("invalid vault record"); underlying read failures surface
        # as the existing I/O exceptions.
        log=Path(root)/"versions.jsonl"
        with cls._lock_for_log(log):
            if not log.exists():
                return {"record_count":0,"active_names":0,"last_version":0}
            lock_handle=log.open("rb")
            try:
                with _file_lock(lock_handle):
                    state=cls._load_log(log)
            finally:
                lock_handle.close()
        records=state.records
        last=records[-1]["version"] if records else 0
        return {"record_count":len(records),
                "active_names":len(state.snapshot),
                "last_version":last}
    def _append_locked(self,name,value,expected_version):
        # Shared body of put and put_if_version.  Every argument has already
        # passed its side-effect-free validation; the in-process per-root lock
        # plus the cross-platform file lock serializes the whole
        # revalidate-check-append sequence so that two conditional writers can
        # never append against the same base version, as threads or as
        # separate processes, on POSIX and on Windows alike.
        with self._write_lock:
            if self.log.exists():
                # A binary read-only handle is enough to take the lock (flock
                # contends on the inode, LockFile only needs read access, and
                # the Windows lock range sits past the end of the data), and
                # leaves a conditional miss free of any filesystem side effect.
                lock_handle=self.log.open("rb")
            else:
                # The vault does not exist yet.  Rehearse against the empty
                # chain BEFORE creating anything, preserving put's rule that an
                # unstorable value never creates a directory; a conditional
                # update then necessarily misses (no active version on disk).
                self._build_line(name,value,1)
                if expected_version is not None:
                    raise VersionConflictError(name,expected_version,None)
                self.root.mkdir(parents=True,exist_ok=True)
                lock_handle=self.log.open("ab")
            try:
                with _file_lock(lock_handle):
                    # Revalidate the complete chain from disk under the lock so
                    # a new record is only appended when it attaches to the
                    # valid chain and the caller's base version is still
                    # active.  Another writer may have populated a just-created
                    # log before this lock was taken, so the version is always
                    # derived here.
                    state=self._load_log(self.log)
                    if expected_version is not None:
                        active=state.snapshot.get(name)
                        actual_version=active["version"] if active is not None else None
                        if actual_version!=expected_version:
                            # No bytes written and self._state is left untouched.
                            raise VersionConflictError(name,expected_version,actual_version)
                    version=len(state.records)+1
                    line=self._build_line(name,value,version)
                    with self.log.open("a",encoding="utf-8") as f:
                        f.write(line)
            finally:
                # Closing the lock handle (after _file_lock has unlocked it)
                # happens on every exit path, and the append handle above is
                # already closed — its bytes flushed — before the lock is
                # released, so any waiter revalidates against a chain that
                # already includes the new record.
                lock_handle.close()
            self.reload(); return version
    def _append_batch_locked(self,pairs,expected=None):
        # Shared append body of put_batch (expected is None) and
        # put_batch_if_versions (expected maps every pair name to the active
        # version the caller based its update on, 0 meaning the name must not
        # exist).  Every pair has already passed its side-effect-free
        # validation (including the _build_line rehearsal), so nothing here can
        # fail with TypeError; a corrupt chain still surfaces as ValueError from
        # the locked revalidation before any byte is written, and a mismatched
        # condition surfaces as VersionConflictError, likewise before any
        # directory is created or any byte is written.  The in-process
        # per-root lock plus the cross-platform file lock serializes the whole
        # revalidate-check-append sequence against every other
        # put/put_batch/put_if_version, as threads or as separate processes, on
        # POSIX and on Windows alike, so no record can interleave into the
        # batch's consecutive version range.
        with self._write_lock:
            if self.log.exists():
                # Same binary read-only lock handle as _append_locked: the
                # lock contends on the file itself, and a corrupt chain is
                # detected without any filesystem side effect.
                lock_handle=self.log.open("rb")
            else:
                # Validation already rehearsed every record above.  For a plain
                # batch reaching this point cannot fail; for a conditional
                # batch a non-existent log means every name is actually absent,
                # so the whole commit can only proceed when every expected
                # version is 0.  Check that against the empty chain BEFORE
                # creating anything, preserving the rule that a conditional
                # miss never creates a directory.
                if expected is not None:
                    for name,_value in pairs:
                        if expected[name]!=0:
                            raise VersionConflictError(name,expected[name],None)
                self.root.mkdir(parents=True,exist_ok=True)
                lock_handle=self.log.open("ab")
            try:
                with _file_lock(lock_handle):
                    # Revalidate the complete chain from disk under the lock;
                    # the batch's versions are always derived from the chain
                    # found here because another process may have appended
                    # before the lock was taken.  A corrupt chain raises
                    # ValueError before any write.  A conditional batch then
                    # compares every name, in input order, against the active
                    # version found on disk: the first mismatch raises before
                    # any byte is written and leaves self._state untouched.
                    state=self._load_log(self.log)
                    if expected is not None:
                        for name,_value in pairs:
                            active=state.snapshot.get(name)
                            actual_version=active["version"] if active is not None else None
                            if (actual_version if actual_version is not None else 0)!=expected[name]:
                                raise VersionConflictError(name,expected[name],actual_version)
                    base=len(state.records)
                    text="".join(self._build_line(name,value,base+index+1)
                                 for index,(name,value) in enumerate(pairs))
                    with self.log.open("a",encoding="utf-8") as f:
                        f.write(text)
            finally:
                # The append handle above is already closed — its bytes
                # flushed — before the coordination is released on every exit
                # path, so any waiter revalidates against a chain that already
                # includes the whole batch.
                lock_handle.close()
            self.reload()
            return [base+index+1 for index in range(len(pairs))]
    def _build_line(self,name,value,version):
        item={"version":version,"name":name,"value":value}; item["digest"]=self._digest(item)
        line=json.dumps(item,sort_keys=True)+"\n"
        # Rehearse the append: the exact bytes about to be written must parse
        # as the next record in the chain, reproduce the digest, and reload to
        # a value equal to the submitted one.  Only then are they written.
        try:
            parsed=self._parse_record(line[:-1],version-1)
        except ValueError:
            raise TypeError("value is not storable under the vault JSON rules")
        original={"version":version,"name":name,"value":value}
        if not self._json_equal(parsed,original):
            raise TypeError("value is not storable under the vault JSON rules")
        return line
    def put(self,name,value):
        if not isinstance(name,str) or not name:
            raise ValueError("name required")
        # Validate round-trip semantics before touching the filesystem: a value
        # that json.dumps accepts but which reloads as a different object
        # (coerced or duplicated keys, NaN/Infinity, tuple arrays) must fail as
        # TypeError without creating a directory, writing bytes, or touching state.
        self._ensure_storable(value)
        return self._append_locked(name,value,None)
    def put_batch(self,items):
        # Append several named configs as one commit.  `items` must be a
        # non-empty list or tuple of (name, value) pairs, each itself a
        # two-element list or tuple; names must be non-empty strings and may
        # not repeat within the batch.  Every structural problem is a
        # ValueError, every unstorable value a TypeError, and ALL of these
        # checks run before any directory is created, any log is opened or any
        # in-memory state changes, so a rejected batch leaves disk bytes, the
        # snapshot and the next version number untouched (a fresh root is not
        # created either).
        if not isinstance(items,(list,tuple)) or not items:
            raise ValueError("batch must be a non-empty list or tuple")
        pairs=[]; seen=set()
        for element in items:
            if not isinstance(element,(list,tuple)) or len(element)!=2:
                raise ValueError("batch elements must be (name, value) pairs")
            name,value=element
            if not isinstance(name,str) or not name:
                raise ValueError("name required")
            if name in seen:
                raise ValueError("duplicate name in batch")
            seen.add(name)
            pairs.append((name,value))
        # Same round-trip gate as put, applied to every value up front: first
        # the pure serializability/key checks, then a full rehearsal of each
        # record's exact bytes so a tuple array or other reload-mismatch fails
        # here as TypeError instead of surfacing after the log was opened.
        for name,value in pairs:
            self._ensure_storable(value)
        for index,(name,value) in enumerate(pairs):
            self._build_line(name,value,index+1)
        return self._append_batch_locked(pairs)
    def put_batch_if_versions(self,items,expected_versions):
        # Conditional counterpart of put_batch: append the batch as one commit
        # only when every name's active version on disk still matches the
        # version the caller based its update on; 0 means the name must
        # currently not exist.  `items` obeys exactly the same shape rules as
        # put_batch's (a non-empty list or tuple of unique (name, value)
        # pairs), and `expected_versions` must be a dict whose string keys are
        # exactly the batch names — no missing name, no extra name — each value
        # a non-negative integer that is not a bool.  Every structural problem
        # is a ValueError, every unstorable value a TypeError, and ALL of these
        # checks — including a full JSON round-trip rehearsal of each record —
        # run before any directory is created, any log is opened or any
        # in-memory state changes, so a rejected batch leaves disk bytes, the
        # snapshot and the next version number untouched (a fresh root is not
        # created either).  The chain is then re-read and revalidated under
        # the shared write coordination: only when every name matches are the
        # records appended consecutively in input order and the same-ordered
        # list of new global versions returned; the first mismatching name
        # (compared in input order) raises the existing
        # VersionConflictError(name, expected_version, actual_version) with
        # actual_version None for a name that does not exist, writing nothing,
        # creating no directory, and replacing neither the caller's snapshot
        # nor the next version number.
        if not isinstance(items,(list,tuple)) or not items:
            raise ValueError("batch must be a non-empty list or tuple")
        pairs=[]; seen=set()
        for element in items:
            if not isinstance(element,(list,tuple)) or len(element)!=2:
                raise ValueError("batch elements must be (name, value) pairs")
            name,value=element
            if not isinstance(name,str) or not name:
                raise ValueError("name required")
            if name in seen:
                raise ValueError("duplicate name in batch")
            seen.add(name)
            pairs.append((name,value))
        if not isinstance(expected_versions,dict):
            raise ValueError("expected_versions must be a name to version mapping")
        if set(expected_versions)!=seen:
            raise ValueError("expected_versions must cover exactly the batch names")
        for name,_value in pairs:
            if not self._is_nonneg_int(expected_versions[name]):
                raise ValueError("expected versions must be non-negative integers")
        # Same round-trip gate as put_batch, for every value up front: pure
        # serializability/key checks first, then a full rehearsal of each
        # record's exact bytes (versions are only placeholders here — the real
        # ordinals are derived under the lock — but the storable rules are
        # independent of them), so a reload-mismatching value fails here as
        # TypeError before the filesystem is touched.
        for name,value in pairs:
            self._ensure_storable(value)
        for index,(name,value) in enumerate(pairs):
            self._build_line(name,value,index+1)
        expected={name:expected_versions[name] for name,_value in pairs}
        return self._append_batch_locked(pairs,expected)
    def put_if_version(self,name,expected_version,value):
        if not isinstance(name,str) or not name:
            raise ValueError("name required")
        if not self._is_positive_int(expected_version):
            raise ValueError("expected_version must be a positive integer")
        # Same round-trip gate as put, run before any filesystem access.
        self._ensure_storable(value)
        return self._append_locked(name,value,expected_version)
    def read_view(self):
        # Freeze one consistent, read-only view of the complete state currently
        # loaded in THIS instance.  The capture is only the already-published
        # _State — itself the output of a fully validated reload or initial
        # load — grabbed once by reference: it neither reads nor writes disk,
        # runs no validation, takes no coordination, triggers no reload, moves
        # the active version, consumes version numbers or disturbs any
        # existing snapshot.  Because every later reload validates a fresh
        # chain and then publishes a brand-new _State, the captured object is
        # never mutated afterwards, so the view keeps answering from that
        # complete record sequence while this instance or any other instance,
        # thread or process keeps doing put/put_batch/put_if_version/
        # put_batch_if_versions/reload, and even while versions.jsonl is
        # externally corrupted; none of those writes becomes visible through
        # the view.  The view exposes exactly the shared read semantics
        # (get, active_version, versions, history, snapshot_at, diff_at with
        # the same KeyError/ValueError rules and deep copies) and nothing that
        # can write or reload, and dropping the vault does not invalidate a
        # view the caller kept.  An empty vault yields a usable view whose
        # snapshot_at(0) and diff_at(0,0) are the existing empty results.
        return _VaultView(self._state)

class _VaultView(_VaultReader):
    # Read-only consistent snapshot handed out by VersionedVault.read_view.
    # It owns nothing but the captured _State: no root, log, coordination or
    # back-reference to the vault, so nothing on it can write, reload, create a
    # file or follow later disk state, and the captured chain stays alive with
    # the view even after the originating vault reloads or is discarded.
    __slots__=("_state",)
    def __init__(self,state):
        self._state=state

if __name__=="__main__":
    p=argparse.ArgumentParser(description="VersionedVault command line")
    p.add_argument("command",choices=("put","get","versions","active","history","snapshot","diff","verify","batch-if"))
    p.add_argument("--root",default="vault")
    p.add_argument("--name")
    p.add_argument("--value",help="write the argument verbatim as a string; mutually exclusive with --value-json")
    p.add_argument("--value-json",dest="value_json",
                   help="parse the argument as one complete JSON document and write the parsed "
                        "value (objects, arrays, numbers, booleans and null keep their type); "
                        "mutually exclusive with --value: giving both, or passing text that is "
                        "not a complete JSON document, exits with status 1 without appending a record")
    p.add_argument("--items-json",dest="items_json",
                   help="only for batch-if: one complete JSON document holding a non-empty "
                        "array of [name, value] pairs with unique names")
    p.add_argument("--expected-versions-json",dest="expected_versions_json",
                   help="only for batch-if: one complete JSON document holding an object "
                        "mapping every batch name to its expected active version, a "
                        "non-negative integer (0 means the name must not exist)")
    p.add_argument("--version",
                   help="record/global sequence number: selects one named version for get, "
                        "or a whole-repository point in time (0 = empty snapshot) for snapshot")
    p.add_argument("--if-version",dest="if_version",
                   help="only for put: append only when the named config's active "
                        "version equals N; a missing name or a stale N exits with "
                        "status 1 without writing or printing a version")
    p.add_argument("--from-version",dest="from_version",
                   help="only for diff: global record ordinal of the starting point (0 = empty snapshot)")
    p.add_argument("--to-version",dest="to_version",
                   help="only for diff: global record ordinal of the ending point (0 = empty snapshot)")
    p.add_argument("--json",dest="json_output",action="store_true",
                   help="only affects get: print the value as a single JSON document on stdout "
                        "(sorted object keys, unescaped Unicode), accepted by json.loads; "
                        "without this flag get keeps its default output")
    a=p.parse_args()
    try:
        if a.command=="verify":
            # verify is the read-only audit front end: it takes --root and
            # nothing else.  Any of the other commands' options is rejected the
            # same way every other semantic option failure is (status 1,
            # nothing on stdout), before the audit reads anything.  The audit
            # itself never prints a partial result: its single object is
            # serialized only after the whole chain validates, and a corrupt
            # log's ValueError takes the shared failure path below.
            if (a.name is not None or a.value is not None or a.value_json is not None
                    or a.version is not None or a.if_version is not None
                    or a.from_version is not None or a.to_version is not None
                    or a.items_json is not None or a.expected_versions_json is not None
                    or a.json_output):
                raise SystemExit(1)
            print(json.dumps(VersionedVault.audit(a.root),sort_keys=True))
            raise SystemExit(0)
        parsed_items=parsed_expected=None
        if a.command=="batch-if":
            # Both JSON documents are required; validate their presence and
            # parse each as exactly one complete document BEFORE the vault is
            # constructed (constructing it reads the log), so a missing option
            # or an incomplete/ambiguous document takes the unified failure
            # path without touching disk.  The mapping additionally rejects a
            # JSON object that repeats a member, which plain parsing would
            # silently collapse.  Every remaining rule — pair shape, unique
            # names, exactly matching name sets, non-negative integer
            # versions, value storability, precondition conflicts and a
            # corrupt chain — is enforced by put_batch_if_versions itself
            # below, whose ValueError, TypeError and VersionConflictError all
            # reach the shared handler (status 1, empty stdout).
            if a.items_json is None or a.expected_versions_json is None:
                raise SystemExit(1)
            def reject_duplicate_keys(pairs):
                seen=set()
                for key,_item in pairs:
                    if key in seen:
                        raise ValueError("duplicate object key")
                    seen.add(key)
                return dict(pairs)
            try:
                parsed_items=json.loads(a.items_json)
                parsed_expected=json.loads(a.expected_versions_json,
                                           object_pairs_hook=reject_duplicate_keys)
            except (ValueError,TypeError):
                raise SystemExit(1)
        v=VersionedVault(a.root)
        if a.command=="put":
            # Checked by hand instead of an argparse mutually-exclusive group so
            # the conflict reports the same unified status 1 as every other
            # failure, and is rejected before parsing or writing anything.
            if a.value is not None and a.value_json is not None:
                raise SystemExit(1)
            if a.value_json is not None:
                try:
                    value=json.loads(a.value_json)
                except (ValueError,TypeError):
                    # Not a complete JSON document: no record, no version output.
                    raise SystemExit(1)
            else:
                value=a.value
            if a.if_version is not None:
                # Parsed by hand so every malformed, zero, negative or
                # fractional condition exits 1 through the same path as every
                # other rejected put, without appending or printing anything.
                try:
                    expected=int(a.if_version,10)
                except (ValueError,TypeError):
                    raise SystemExit(1)
                if expected<=0:
                    raise SystemExit(1)
                print(v.put_if_version(a.name,expected,value))
            else:
                print(v.put(a.name,value))
        elif a.command=="batch-if":
            # On success stdout gets exactly one line accepted by json.loads —
            # the new global versions in input order — and the process exits 0.
            # Every failure (option, JSON document, duplicate or mismatching
            # names, boolean or fractional versions, unstorable values,
            # VersionConflictError, corrupt vault) reaches the shared handler:
            # status 1 with nothing printed, no partial commit and the
            # in-process snapshot left as it was.
            result=v.put_batch_if_versions(parsed_items,parsed_expected)
            print(json.dumps(result))
        elif a.command=="get":
            if a.version is None:
                value=v.get(a.name)
            else:
                # argparse-typed integer semantics are kept for get: malformed
                # input reports argument failure (status 2), not the unified 1.
                try:
                    get_version=int(a.version,10)
                except ValueError:
                    p.error("argument --version: invalid int value: %r"%a.version)
                value=v.get(a.name,get_version)
            if a.json_output:
                print(json.dumps(value,ensure_ascii=False,sort_keys=True))
            else:
                print(value)
        elif a.command=="versions":
            print(json.dumps(v.versions(),ensure_ascii=False,sort_keys=True))
        elif a.command=="active":
            print(v.active_version(a.name))
        elif a.command=="snapshot":
            if a.version is None:
                point=None
            else:
                # Parsed by hand, like put's --if-version: malformed text,
                # fractions and negative numbers all take the unified failure
                # path (status 1, nothing printed) instead of argparse's 2.
                try:
                    point=int(a.version,10)
                except ValueError:
                    raise SystemExit(1)
                if point<0:
                    raise SystemExit(1)
            # An out-of-range point raises ValueError from snapshot_at and is
            # handled below, again with no partial output.
            print(json.dumps(v.snapshot_at(point),ensure_ascii=False,sort_keys=True))
        elif a.command=="diff":
            # Both points are required and parsed by hand (like snapshot's
            # --version): missing options, text that is not a decimal
            # non-negative integer, fractions and signs all take the unified
            # failure path (status 1, nothing on stdout) before diff_at runs.
            if a.from_version is None or a.to_version is None:
                raise SystemExit(1)
            def parse_point(text):
                if not text or not all("0"<=ch<="9" for ch in text):
                    raise SystemExit(1)
                return int(text,10)
            from_point=parse_point(a.from_version)
            to_point=parse_point(a.to_version)
            # Ordering and range violations raise ValueError from diff_at and
            # are handled with every other failure below: status 1, no output,
            # and the command never appends or creates a record.
            print(json.dumps(v.diff_at(from_point,to_point),
                             ensure_ascii=False,sort_keys=True))
        else:
            print(json.dumps(v.history(a.name),ensure_ascii=False,sort_keys=True))
    except (KeyError,ValueError,TypeError,VersionConflictError):
        raise SystemExit(1)
