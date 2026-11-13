JUNIT=$GRADE/junit.xml; mkdir -p "${JUNIT%/*}"
# @5 per-phase junit archive: every pytest session appends its outcomes here
# so multi-phase commands grade all phases, not just the last report.
JUNIT_PHASES=$GRADE/junit-phases; mkdir -p "$JUNIT_PHASES"
# @10: no bytecode shadow may survive into grading.
find "$CWD" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$CWD" -name '*.pyc' -delete 2>/dev/null || true
mkdir -p "$GRADE/pycache"

# PYTHONUNBUFFERED so the pytest session header reaches the log even when the
# run is killed import-time (same as @2).
_START_A=$(date +%s)
MIMO_VERIFIER_ARCHIVE_DIR="$JUNIT_PHASES" MIMO_VERIFIER_UNITTEST_JUNIT="$GRADE/unittest-junit.xml" MIMO_VERIFIER_INTEGRITY_DIR="$GRADE" PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$GRADE/pycache" PYTHONPATH="$GRADE/v10shim${PYTHONPATH:+:$PYTHONPATH}" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
echo "agent_sec=$(($(date +%s) - _START_A))" >> "$V/timing.log"
python3 - "$JUNIT" "$RC" /tests/test.patch "$V/test_output.log" /tests/test_command.sh "$CWD" <<'PYEOF' > "$V/reward.txt" 2> "$V/junit-grade.log"
import sys, re, base64, binascii, glob, os, xml.etree.ElementTree as ET
junit_path, rc, patch_path, output_path, cmd_path, cwd = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
patch = open(patch_path, encoding="utf-8", errors="replace").read()
ids = set(re.findall(r'(\S+\.py::[^\s"\']+)', patch.split("mimo_test_command.sh")[-1]))
output = open(output_path, encoding="utf-8", errors="replace").read()
output = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\r", "", output)
cmd_text = open(cmd_path, encoding="utf-8", errors="replace").read()
mimo = ""
for ref in re.findall(r'(\S*mimo_test_command\.sh)', cmd_text):
    try:
        mimo += open(ref.split("/")[-1], encoding="utf-8", errors="replace").read() + "\n"
    except OSError:
        pass
if "build_env/test_command" in cmd_text + mimo:
    for root, _dirs, files in os.walk("/var/lib/mimo-grade/build_env"):
        for fn in files:
            if fn == "test_command.sh":
                try:
                    mimo += open(os.path.join(root, fn), encoding="utf-8", errors="replace").read() + "\n"
                except OSError:
                    pass
hay = cmd_text + "\n" + mimo + "\n" + patch.split("mimo_test_command.sh")[-1]
for tok in re.findall(r'[A-Za-z0-9+/]{40,}={0,2}', hay):
    try:
        hay += base64.b64decode(tok, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        pass
order = [
    ("go-test", r"(?<![\w])go\s+test\b|GO_BIN\"\s+test\b|\$GO_BIN\"\s+test"),
    ("jest", r"(?<![\w])jest\b"),
    ("vitest", r"(?<![\w])vitest\b"),
    ("mocha", r"(?<![\w])mocha\b|_mocha\b"),
    ("ava", r"(?<![\w])ava\b"),
    ("tap", r"(?<![\w])tap\b"),
    ("karma", r"(?<![\w])karma\b"),
    ("jasmine", r"(?<![\w])jasmine\b"),
    ("node-test", r"node\s+--test\b"),
    ("cargo-test", r"cargo\s+test\b"),
    ("rspec", r"(?<![\w])rspec\b"),
    ("phpunit", r"(?<![\w])phpunit\b"),
    ("mvn", r"(?<![\w])mvn\b|surefire|failsafe"),
    ("gradle", r"(?<![\w])gradlew?\b"),
    ("pytest", r"(?<![\w])pytest\b"),
    ("unittest", r"-m\s+unittest\b"),
    ("forge", r"(?<![\w])forge\s+test\b"),
    ("node-run", r"(?<![\w])node\b"),
    ("make", r"(?<![\w])(make|ctest|cmake)\b"),
    ("bats", r"(?<![\w])bats\b"),
]
runner = "custom"
for name, rx in order:
    if re.search(rx, hay):
        runner = name
        break
if runner == "tap" and (re.search(r"(^|\s)-R\s+\S*terse\b|reporters?\s*[=:]\s*\S*terse\b", hay) is not None or (re.search(r"Asserts:\s+[^\n]*\d+\s+passed", output) is not None and re.search(r"^(not )?ok\b", output, re.M) is None and re.search(r"\d+\s+passing", output) is None)):
    runner = "terse"
if runner == "tap" and (re.search(r"(^|\s)-R\s+\S*spec\b|reporters?\s*[=:]\s*\S*spec\b", hay) is not None or (re.search(r"\d+\s+passing", output) is not None and re.search(r"^(not )?ok\b", output, re.M) is None)):
    runner = "mocha"
sys.stderr.write("runner=%s rc=%d\n" % (runner, rc))
_v10_integrity = ""
for _v10_name in ("integrity.txt", "integrity-unittest.txt"):
    try:
        with open(os.path.join(os.path.dirname(junit_path), _v10_name), encoding="utf-8", errors="replace") as _v10f:
            _v10_text = _v10f.read().strip()
        if _v10_text.startswith("CHANGED"):
            _v10_integrity = _v10_text
            break
    except Exception:
        pass
if _v10_integrity.startswith("CHANGED"):
    print(0)
    sys.stderr.write("runner integrity changed: %s\n" % _v10_integrity)
    sys.exit()
blank = len(output.strip()) == 0
def fallback():
    print(1 if rc == 0 else 0)
    sys.stderr.write("rc=%d %s: exit-code grading\n" % (rc, runner))
    sys.exit()
if runner == "pytest":
    cleared = re.search(r'unset\s+[^\n]*PYTEST_ADDOPTS|PYTEST_ADDOPTS\s*=|env\s+[^\n]*-u\s+PYTEST_ADDOPTS', hay) is not None
    has_pytest = "pytest" in hay.lower()
    started = has_pytest and ("test session starts" in output or re.search(r'collected \d+ items?', output) is not None)
    flag = os.path.exists("/var/lib/mimo-grade/hook_installed") or ((not cleared) and (started or (len(output.strip()) == 0 and has_pytest)))
    def grade_noreport():
        if flag:
            sys.stderr.write("rc=%d @@JUNIT_MISSING_REASON@@\n" % rc)
            print(0)
        else:
            print(1 if rc == 0 else 0)
            sys.stderr.write("rc=%d no junit: exit-code grading\n" % rc)
        sys.exit()
    arch = []
    for phase in sorted(glob.glob(os.path.join(os.path.dirname(junit_path), "junit-phases", "junit-hook-*.xml"))):
        try:
            praw = open(phase, "rb").read()
        except Exception:
            continue
        if praw:
            arch.append(praw)
    try:
        raw = open(junit_path, "rb").read()
    except OSError:
        raw = b""
    try:
        cases = list(ET.fromstring(raw).iter("testcase")) if raw else []
    except Exception:
        grade_noreport()
    if not raw and not arch:
        grade_noreport()
    for rep in arch:
        try:
            cases.extend(list(ET.fromstring(rep).iter("testcase")))
        except Exception:
            pass
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    # Option values (e.g. --deselect=path.py::test_x) are not selected node IDs.
    ids = {i for i in ids if not i.startswith("-")}
    def _comps(p):
        p = p.replace("\\", "/").strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p.endswith(".py"):
            p = p[:-3]
        return [c for c in re.split(r"[/.]+", p) if c]
    def _parse_node(n):
        parts = n.split("::")
        return (_comps(parts[0]), parts[1:-1], parts[-1])
    def _test_ok(seen_t, exp_t):
        if "[" in exp_t:
            return seen_t == exp_t
        return seen_t == exp_t or seen_t.startswith(exp_t + "[")
    def _tail(shorter, longer):
        return bool(shorter) and len(shorter) <= len(longer) and shorter == longer[len(longer) - len(shorter):]
    def _aligned(a, b):
        if not a or not b:
            return False
        s, l = (a, b) if len(a) <= len(b) else (b, a)
        return _tail(s, l)
    def _hit(cn, nm, fl, exp):
        em, ec, et = _parse_node(exp)
        if not em or not et:
            return False
        np = (nm or "").split("::")
        st = np[-1]
        extra = np[:-1]
        cp = (cn or "").split(".") if cn else []
        cands = []
        for cut in range(1, len(cp) + 1):
            cands.append((cp[:cut], cp[cut:] + extra, st))
        if fl:
            sm = _comps(fl)
            for cut in range(len(cp) + 1):
                if cut == 0 or _tail(cp[:cut], sm):
                    cands.append((sm, cp[cut:] + extra, st))
        class_level = not et.startswith("test_")
        for mod, cls, seen_t in cands:
            if not _aligned(mod, em):
                continue
            if cls == ec and _test_ok(seen_t, et):
                return True
            if class_level and cls == ec + [et]:
                return True
        return False
    missing = [i for i in ids if not any(_hit(c.get("classname"), c.get("name"), c.get("file"), i) for c in cases)]
    base_union = os.path.join(os.path.dirname(junit_path), "baseline")
    base_cases = []
    try:
        with open(os.path.join(base_union, "junit.xml"), "rb") as _bf:
            _braw = _bf.read()
    except Exception:
        _braw = b""
    if _braw:
        try:
            base_cases.extend(ET.fromstring(_braw).iter("testcase"))
        except Exception:
            base_cases = []
    for _bphase in sorted(glob.glob(os.path.join(base_union, "junit-phases", "junit-hook-*.xml"))):
        try:
            _bpraw = open(_bphase, "rb").read()
        except Exception:
            continue
        if _bpraw:
            try:
                base_cases.extend(ET.fromstring(_bpraw).iter("testcase"))
            except Exception:
                pass
    _v7bout = ""
    try:
        with open(os.path.join(base_union, "output.log"), encoding="utf-8", errors="replace") as _v7bf:
            _v7bout = _v7bf.read()
    except Exception:
        pass
    # @7 G8: an INTERNALERROR pristine baseline proves nothing (measured
    # 000585): skip fail-to-pass and keep the structured @5 rules below.
    _v7unreliable = "INTERNALERROR" in _v7bout
    ftp_bad = False
    if base_cases and not _v7unreliable:
        def _v6st(_c):
            if _c.find("failure") is not None or _c.find("error") is not None:
                return "bad"
            if _c.find("skipped") is not None:
                return "skipped"
            return "passed"
        def _v6key(_c):
            return (_c.get("classname") or "", _c.get("name") or "", _c.get("file") or "")
        _v6rank = {"passed": 0, "skipped": 1, "bad": 2}
        _bm = {}
        for _c in base_cases:
            _k = _v6key(_c)
            _s = _v6st(_c)
            if _k not in _bm or _v6rank[_s] > _v6rank[_bm[_k]]:
                _bm[_k] = _s
        _am = {}
        for _c in cases:
            _k = _v6key(_c)
            _s = _v6st(_c)
            if _k not in _am or _v6rank[_s] > _v6rank[_am[_k]]:
                _am[_k] = _s
        _bs = set(_k for _k, _s in _bm.items() if _s == "skipped")
        _v7errmod = {}
        _v7errfile = {}
        _v7hasfail = set()
        for _c in base_cases:
            _ck = _v6key(_c)
            if _c.find("failure") is not None:
                _v7hasfail.add(_ck)
            if _c.find("error") is None or _c.find("failure") is not None:
                continue
            _nm = _c.get("name") or ""
            if "::" in _nm:
                continue
            for _cand in ((_c.get("classname") or ""), _nm):
                _ct = _cand.strip()
                if _ct.endswith(".py"):
                    _ct = _ct[:-3]
                if re.fullmatch(r"[A-Za-z_][\w.-]*", _ct or ""):
                    _v7errmod.setdefault(_ck, set()).add(_ct)
            _cf = (_c.get("file") or "").strip()
            if _cf:
                _v7errfile.setdefault(_ck, set()).add(_cf)
        def _v7modok(_k):
            for _mc in cases:
                if _mc.find("failure") is not None or _mc.find("error") is not None or _mc.find("skipped") is not None:
                    continue
                _af = (_mc.get("file") or "").strip()
                if _k in _v7errfile and _v7errfile[_k]:
                    for _ef in _v7errfile[_k]:
                        if _af and (_af == _ef or _af.endswith("/" + _ef) or _ef.endswith("/" + _af)):
                            return True
                _acn = (_mc.get("classname") or "").strip()
                _amods = [_acn]
                if _af:
                    _afm = _af.replace("/", ".").replace(chr(92), ".")
                    if _afm.endswith(".py"):
                        _afm = _afm[:-3]
                    _amods.append(_afm)
                for _m in _v7errmod.get(_k, ()):
                    for _am2 in _amods:
                        if _am2 == _m or _am2.startswith(_m + "."):
                            return True
            return False
        _v9satisfied = []
        for _k, _s in _bm.items():
            if _s != "skipped" and _am.get(_k) != "passed":
                if (_k in _v7errmod or _k in _v7errfile) and _k not in _v7hasfail and _v7modok(_k):
                    _v9satisfied.append((_v7errmod.get(_k, set()), _v7errfile.get(_k, set())))
                    continue
                ftp_bad = True
        for _k, _s in _am.items():
            if _s == "skipped" and _k not in _bs:
                _v9exc = False
                for (_smods, _sfiles) in _v9satisfied:
                    _kcn = (_k[0] or "").strip()
                    _kf = (_k[2] or "").strip()
                    if _kf:
                        for _ef in _sfiles:
                            if _kf == _ef or _kf.endswith("/" + _ef) or _ef.endswith("/" + _kf):
                                _v9exc = True
                                break
                    if not _v9exc:
                        _kamods = [_kcn]
                        if _kf:
                            _kafm = _kf.replace("/", ".").replace(chr(92), ".")
                            if _kafm.endswith(".py"):
                                _kafm = _kafm[:-3]
                            _kamods.append(_kafm)
                        for _kam in _kamods:
                            for _sm in _smods:
                                if _kam == _sm or _kam.startswith(_sm + "."):
                                    _v9exc = True
                                    break
                            if _v9exc:
                                break
                    if _v9exc:
                        break
                if not _v9exc:
                    ftp_bad = True
    if _v7unreliable:
        sys.stderr.write("baseline unreliable (INTERNALERROR): fail-to-pass skipped\n")
    if ftp_bad:
        sys.stderr.write("fail-to-pass: pristine outcomes not preserved\n")
    print(1 if rc == 0 and cases and not bad and not missing and not ftp_bad else 0)
    sys.stderr.write("rc=%d cases=%d bad=%d skipped=%d named=%d missing=%s\n" % (rc, len(cases), len(bad), len(skipped), len(ids), missing))
    sys.exit()
if runner in ("go-test", "jest", "vitest", "mocha", "ava", "tap", "node-test", "karma", "jasmine", "cargo-test", "rspec", "phpunit", "unittest", "terse"):
    if blank:
        sys.stderr.write("rc=%d blank %s output: no test evidence\n" % (rc, runner))
        print(0)
        sys.exit()
    if rc != 0:
        print(0)
        sys.stderr.write("rc=%d %s nonzero exit\n" % (rc, runner))
        sys.exit()
    ok = False
    if runner == "go-test":
        _go_ok = [ln for ln in output.splitlines() if re.match(r"^ok\s+\S+", ln) is not None and "no tests to run" not in ln]
        _go_bare = re.search(r"^PASS\s*$", output, re.M) is not None
        ok = (_go_ok or (_go_bare and "no tests to run" not in output)) and re.search(r"^(FAIL|--- FAIL|panic:)", output, re.M) is None
    elif runner == "jest":
        ok = re.search(r"Tests:\s+[^\n]*\d+\s+passed", output) is not None and re.search(r"Tests:\s+.*failed|Test Suites:\s+.*failed", output) is None
    elif runner == "vitest":
        ok = re.search(r"Tests\s+[^\n]*\d+\s+passed", output) is not None and re.search(r"(Test Files|Tests)\s+.*failed", output) is None
    elif runner == "mocha":
        ok = re.search(r"\d+\s+passing", output) is not None and re.search(r"[1-9]\d*\s+failing", output) is None
    elif runner == "terse":
        ok = re.search(r"Asserts:\s+[^\n]*\d+\s+passed", output) is not None and re.search(r"Asserts:\s+[^\n]*failed|Suites:\s+[^\n]*failed", output) is None
    elif runner in ("tap", "ava", "node-test"):
        ok = re.search(r"^ok\b", output, re.M) is not None and re.search(r"^not ok\b", output, re.M) is None
    elif runner == "karma":
        ok = "SUCCESS" in output and "FAILED" not in output
    elif runner == "jasmine":
        ok = re.search(r"[1-9]\d*\s+failures?|failed expectations", output) is None
    elif runner == "cargo-test":
        ok = re.search(r"test result:\s+ok", output) is not None and re.search(r"test result:\s+FAILED|panicked", output) is None
    elif runner == "rspec":
        ok = re.search(r"0\s+failures", output) is not None and re.search(r"[1-9]\d*\s+failures", output) is None
    elif runner == "phpunit":
        ok = re.search(r"^OK\b", output, re.M) is not None and re.search(r"FAILURES!|ERRORS!|No tests executed", output) is None
    elif runner == "unittest":
        def _v10load(_p):
            try:
                with open(_p, "rb") as _f:
                    _raw = _f.read()
            except Exception:
                return []
            try:
                return list(ET.fromstring(_raw).iter("testcase")) if _raw else []
            except Exception:
                return []
        _v10dir = os.path.dirname(junit_path)
        _ucases = _v10load(os.path.join(_v10dir, "unittest-junit.xml"))
        _ubcases = _v10load(os.path.join(_v10dir, "baseline", "unittest-junit.xml"))
        def _v10map(_cases):
            _rank = {"passed": 0, "skipped": 1, "bad": 2}
            _out = {}
            for _c in _cases:
                _s = "bad" if (_c.find("failure") is not None or _c.find("error") is not None) else ("skipped" if _c.find("skipped") is not None else "passed")
                _k = (_c.get("classname") or "", _c.get("name") or "", _c.get("file") or "")
                if _k not in _out or _rank[_s] > _rank[_out[_k]]:
                    _out[_k] = _s
            return _out
        _umap = _v10map(_ucases)
        _ubmap = _v10map(_ubcases)
        ok = bool(_umap) and all(_s != "bad" for _s in _umap.values())
        if ok and _ubmap:
            _ubskip = {_k for _k, _s in _ubmap.items() if _s == "skipped"}
            for _k, _s in _ubmap.items():
                if _s != "skipped" and _umap.get(_k) != "passed":
                    ok = False
            for _k, _s in _umap.items():
                if _s == "skipped" and _k not in _ubskip:
                    ok = False
        sys.stderr.write("unittest junit cases=%d baseline=%d ok=%s\n" % (len(_ucases), len(_ubcases), ok))
    print(1 if ok else 0)
    sys.stderr.write("rc=%d %s markers=%s\n" % (rc, runner, ok))
    sys.exit()
if runner in ("mvn", "gradle"):
    paths = []
    for pat in ("target/surefire-reports/*.xml", "target/failsafe-reports/*.xml", "build/test-results/test/*.xml", "build/test-results/**/*.xml"):
        paths.extend(glob.glob(os.path.join(cwd, pat), recursive=True))
    if not paths:
        if blank:
            sys.stderr.write("rc=%d %s blank with no surefire reports\n" % (rc, runner))
            print(0)
            sys.exit()
        fallback()
    cases = []
    try:
        for p in paths:
            cases.extend(ET.parse(p).getroot().iter("testcase"))
    except Exception:
        print(0)
        sys.stderr.write("rc=%d %s unparsable surefire report\n" % (rc, runner))
        sys.exit()
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    print(1 if rc == 0 and cases and not bad else 0)
    sys.stderr.write("rc=%d %s surefire=%d cases=%d bad=%d skipped=%d\n" % (rc, runner, len(paths), len(cases), len(bad), len(skipped)))
    sys.exit()
fallback()
PYEOF
echo "REWARD=$(cat "$V/reward.txt") rc=$RC runner=$(cat "$V/runner.txt")" | tee -a "$V/test_output.log"
