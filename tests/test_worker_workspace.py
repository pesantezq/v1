import subprocess, pytest
from pathlib import Path
from operator_control import worker_workspace as ws

def _run(cwd, *a): subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True)

def _prod_repo(tmp_path):
    repo = tmp_path / "prod"; repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "t@t"); _run(repo, "git", "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n"); _run(repo, "git", "add", "."); _run(repo, "git", "commit", "-qm", "base")
    return repo

def test_create_isolated_clone_has_own_git(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    p = ws.create_isolated_workspace(str(repo), str(wsr), "wo_abc")
    assert Path(p).is_dir() and (Path(p) / ".git").exists()
    # the clone's gitdir is INSIDE the workspace, not the prod repo
    gitdir = subprocess.run(["git","-C",p,"rev-parse","--absolute-git-dir"],
                            capture_output=True, text=True).stdout.strip()
    assert str(wsr) in gitdir and str(repo) not in gitdir

def test_writes_to_clone_do_not_touch_prod_refs(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    p = ws.create_isolated_workspace(str(repo), str(wsr), "wo_abc")
    (Path(p) / "new.txt").write_text("x\n")
    _run(p, "git", "add", "."); _run(p, "git", "commit", "-qm", "wt change")
    prod_log = subprocess.run(["git","-C",str(repo),"log","--oneline"],capture_output=True,text=True).stdout
    assert "wt change" not in prod_log   # prod untouched

def test_malicious_id_rejected(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    with pytest.raises(ValueError):
        ws.create_isolated_workspace(str(repo), str(wsr), "../../etc")

def test_destroy_refuses_outside_workspace_root(tmp_path):
    with pytest.raises(ValueError):
        ws.destroy_workspace("/etc", str(tmp_path / "wsroot"))

def test_destroy_removes_clone(tmp_path):
    repo = _prod_repo(tmp_path); wsr = tmp_path / "wsroot"
    p = ws.create_isolated_workspace(str(repo), str(wsr), "wo_abc")
    ws.destroy_workspace(p, str(wsr)); assert not Path(p).exists()
