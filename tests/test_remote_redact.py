from grid.remote import Remote, redact


def test_redact_masks_bearer_tokens_and_sk_keys():
    assert redact("curl -H 'Authorization: Bearer sk-9ZccdG5Mz_Vq2_V0t-4x7Q' http://x") \
        == "curl -H 'Authorization: Bearer ***' http://x"
    assert redact("key=sk-abc123DEF and nothing else") == "key=*** and nothing else"
    assert redact("no secret here") == "no secret here"


def test_run_never_logs_the_key():
    logged = []

    def runner(cmd, **kw):
        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()
    r = Remote("debain2", runner=runner, log=logged.append)
    r.run("curl -sf -H 'Authorization: Bearer sk-9ZccdG5Mz_Vq2_V0t-4x7Q' http://gw/key/info")
    assert logged and "sk-9ZccdG5" not in logged[0] and "Bearer ***" in logged[0]
    # the real command still carries the key (it must reach the remote); only the log is masked
    assert "sk-9ZccdG5Mz_Vq2_V0t-4x7Q" in r.commands[0]
