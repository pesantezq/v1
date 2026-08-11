"""Apply (or roll back) the bounded EW-0A A1 promotion in the trusted authority
state, then prove the eight A1 authority boundaries against the LIVE state.

Only run after EW-0A certified with FALSE_CERTIFICATIONS=0, AUTHORITY_VIOLATIONS=0,
NEW_RELEVANT_FAILURES=0. If any A1 authority check fails, this rolls the authority
state back to A0 and reports EW_A1_ACTIVATION_FAILED.
"""
from __future__ import annotations
import datetime
import subprocess
import sys

REPO = "/home/pesan/stockbot-lab/repo/v1"
sys.path.insert(0, REPO)
from portfolio_automation.engineer_worker import ew0a_authority as A          # noqa: E402
from portfolio_automation.engineer_worker.ew0a import RiskClass, Executor      # noqa: E402

now = datetime.datetime.now(datetime.timezone.utc).isoformat()
VENV_PY = REPO + "/.venv/bin/python"

# 1) APPLY the bounded promotion in the authoritative state.
A.set_authority_level(REPO, A.EngineerAuthorityLevel.A1_ASSISTED_ENGINEERING,
                      actor="ew0a-certification", now=now)
lvl = A.read_authority_level(REPO)
print(f"active_authority_level = {lvl.value}")

# 2) Tie the eight A1 authority behaviours to the LIVE state.
checks: dict[str, bool] = {}
checks["1_E1_engineer_admitted"] = A.admit_engineer_task(lvl, RiskClass.E1_ROUTINE) is Executor.ENGINEER
checks["2_E2_engineer_strict_admitted"] = A.admit_engineer_task(lvl, RiskClass.E2_MODERATE) is Executor.ENGINEER_STRICT
for r, k in ((RiskClass.E3_HIGH, "3_E3_denied"), (RiskClass.E4_CONSEQUENTIAL, "4_E4_denied")):
    try:
        A.admit_engineer_task(lvl, r); checks[k] = False
    except A.AuthorityError:
        checks[k] = True
for op, k in (("MAIN_WRITE", "5_main_write_denied"), ("PRODUCTION_WRITE", "6_production_write_denied"),
              ("SELF_PROMOTION", "8_self_promotion_denied")):
    try:
        A.assert_operation_allowed(lvl, op); checks[k] = False
    except A.AuthorityError:
        checks[k] = True
# 7 protected-path write denied — via policy
from portfolio_automation.engineer_worker import policy                        # noqa: E402
checks["7_protected_path_denied"] = (policy.is_protected("decision_engine.py")
                                     and policy.is_protected("config/ew0a_authority.json")
                                     and not policy.is_repair_allowed("config/ew0a_authority.json"))

print("live_authority_checks =", checks)

# 3) Formal gate: run the hermetic A1 authority suite.
p = subprocess.run([VENV_PY, "-m", "pytest", "tests/test_ew0a_authority.py", "-q",
                    "-p", "no:cacheprovider", "-o", "addopts="], cwd=REPO,
                   capture_output=True, text=True)
suite_line = next((l for l in reversed(p.stdout.splitlines()) if "passed" in l or "failed" in l), "")
suite_ok = p.returncode == 0
print(f"a1_authority_suite = {suite_line.strip()}  (rc={p.returncode})")

activated = (lvl is A.EngineerAuthorityLevel.A1_ASSISTED_ENGINEERING
             and all(checks.values()) and suite_ok)

if activated:
    print("\nVERDICT: EW_A1_ASSISTED_ENGINEERING_ENABLED")
    sys.exit(0)
else:
    # rollback to A0 and preserve evidence
    A.set_authority_level(REPO, A.EngineerAuthorityLevel.A0_DIAGNOSTIC,
                          actor="ew0a-certification-rollback", now=now)
    print(f"\nrolled back to {A.read_authority_level(REPO).value}")
    print("VERDICT: EW_A1_ACTIVATION_FAILED")
    sys.exit(1)
