from operator_control.worker_container import verify_runtime_attestation

CFG = {"image_digest": "sha256:abc", "container_uid": 1000, "container_gid": 1000,
       "attestation_max_age_days": 30}
GOOD = {"generated_at_ts": 1000.0, "execution_mode": "container", "uid": 1000, "gid": 1000,
        "rootless": True, "no_new_privileges": True, "effective_caps": [],
        "mounts": ["/work:rw", "/home/worker/.claude:ro", "/attest:rw"],
        "image_digest": "sha256:abc", "socket_mounts_present": False, "host_home_mounted": False}
KW = dict(now=1000.0, image_build_ts=900.0, config_mtime=900.0)

def test_good_attestation_passes():
    ok, reasons = verify_runtime_attestation(GOOD, CFG, **KW); assert ok and reasons == []

def test_root_uid_fails():
    ok, r = verify_runtime_attestation({**GOOD, "uid": 0}, CFG, **KW); assert not ok and any("uid" in x.lower() for x in r)

def test_direct_mode_fails():
    ok, r = verify_runtime_attestation({**GOOD, "execution_mode": "direct"}, CFG, **KW); assert not ok

def test_caps_present_fails():
    ok, r = verify_runtime_attestation({**GOOD, "effective_caps": ["NET_ADMIN"]}, CFG, **KW); assert not ok

def test_socket_mount_fails():
    ok, r = verify_runtime_attestation({**GOOD, "socket_mounts_present": True}, CFG, **KW); assert not ok

def test_host_home_fails():
    ok, r = verify_runtime_attestation({**GOOD, "host_home_mounted": True}, CFG, **KW); assert not ok

def test_digest_mismatch_fails():
    ok, r = verify_runtime_attestation({**GOOD, "image_digest": "sha256:zzz"}, CFG, **KW); assert not ok and any("digest" in x for x in r)

def test_no_new_privileges_false_fails():
    ok, r = verify_runtime_attestation({**GOOD, "no_new_privileges": False}, CFG, **KW); assert not ok

def test_stale_older_than_image_build_fails():
    ok, r = verify_runtime_attestation({**GOOD, "generated_at_ts": 850.0}, CFG,
                                       now=1000.0, image_build_ts=900.0, config_mtime=900.0)
    assert not ok and any("stale" in x.lower() for x in r)

def test_stale_older_than_max_age_fails():
    old = {**GOOD, "generated_at_ts": 1000.0}
    ok, r = verify_runtime_attestation(old, CFG, now=1000.0 + 31*86400, image_build_ts=900.0, config_mtime=900.0)
    assert not ok and any("stale" in x.lower() or "age" in x.lower() for x in r)
