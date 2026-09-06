from grid.remote import Remote, RemoteError


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_run_uses_batch_mode_ssh_and_returns_output():
    calls = []
    def runner(cmd, **kw):
        calls.append((cmd, kw)); return Proc(0, "v3\n")
    r = Remote("debain2", runner=runner)
    res = r.run("cat /srv/rogue/current-model")
    assert res.stdout == "v3\n" and res.returncode == 0
    assert calls[0][0][:4] == ["ssh", "-o", "BatchMode=yes", "-o"] and calls[0][0][-2:] == ["debain2", "cat /srv/rogue/current-model"]
    assert r.commands == ["cat /srv/rogue/current-model"]


def test_dry_run_records_but_never_runs():
    def runner(cmd, **kw): raise AssertionError("must not run")
    r = Remote("debain2", runner=runner, dry_run=True)
    assert r.run("ollama cp a b").returncode == 0 and r.commands == ["ollama cp a b"]
    assert r.read("/x") == "" and r.commands[-1] == "cat /x"


def test_read_raises_on_failure_and_copy_uses_scp():
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd); return Proc(1, "", "No such file")
    r = Remote("debain2", runner=runner)
    import pytest
    with pytest.raises(RemoteError, match="No such file"):
        r.read("/missing")
    r2 = Remote("debain2", runner=lambda cmd, **kw: (calls.append(cmd), Proc(0))[1])
    r2.copy_from("/srv/rogue/x.json", "/tmp/x.json")
    assert calls[-1][:3] == ["scp", "-q", "-o"] and calls[-1][-2:] == ["debain2:/srv/rogue/x.json", "/tmp/x.json"]
