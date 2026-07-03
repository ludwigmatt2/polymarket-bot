"""Relayer batch retry on the transient 'wallet busy: active action exists'
rejection — the wrap→approve race that half-failed the first real go-live."""
import pytest


class _Resp:
    def wait(self):
        return {"status": "MINED"}


class _Relay:
    """Fake relay: raise 'wallet busy' for the first `fail_times` submits, then ok
    (or always raise a non-busy error when `other=True`)."""
    def __init__(self, fail_times=0, other=False):
        self.fail_times, self.other, self.calls = fail_times, other, 0

    def execute_deposit_wallet_batch(self, **kw):
        self.calls += 1
        if self.other:
            raise RuntimeError("insufficient funds")
        if self.calls <= self.fail_times:
            raise RuntimeError(
                "RelayerApiException[status_code=400, "
                "error_message={'error': 'wallet busy: active action exists'}]")
        return _Resp()


_WALLET = "0x000000000000000000000000000000000000dEaD"


def _client(monkeypatch, relay):
    import weather.relayer as rl
    rc = rl.RelayerClient(pk="0x" + "1" * 64)
    monkeypatch.setattr(rc, "_relay", lambda: relay)
    monkeypatch.setattr(rl, "onchain_nonce", lambda w: 7)   # fresh nonce, no RPC
    monkeypatch.setattr(rl.time, "sleep", lambda s: None)    # no real backoff
    return rc, rl


def test_retries_then_succeeds(monkeypatch):
    relay = _Relay(fail_times=2)
    rc, rl = _client(monkeypatch, relay)
    res = rc._batch(_WALLET, [(rl.pm.USDCE, "0x1234")])
    assert res == {"status": "MINED"}
    assert relay.calls == 3  # 2 busy + 1 success


def test_non_busy_error_propagates_immediately(monkeypatch):
    relay = _Relay(other=True)
    rc, rl = _client(monkeypatch, relay)
    with pytest.raises(RuntimeError, match="insufficient funds"):
        rc._batch(_WALLET, [(rl.pm.USDCE, "0x1234")])
    assert relay.calls == 1  # a real error is NOT retried


def test_gives_up_after_bounded_retries(monkeypatch):
    relay = _Relay(fail_times=999)
    rc, rl = _client(monkeypatch, relay)
    with pytest.raises(RuntimeError, match="wallet busy"):
        rc._batch(_WALLET, [(rl.pm.USDCE, "0x1234")])
    assert relay.calls == rl.BUSY_RETRIES  # bounded, doesn't loop forever
