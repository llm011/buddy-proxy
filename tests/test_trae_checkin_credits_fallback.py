"""Tests for TraeProvider.checkin_claim() daily-credit fallback via status API."""
from __future__ import annotations

from types import SimpleNamespace

from buddy_proxy.trae import provider as trae_prov


def test_trae_checkin_claim_falls_back_to_status_credits(monkeypatch):
    accts = [
        SimpleNamespace(id="a1", alias="A1", nickname="", uid="u1"),
        SimpleNamespace(id="a2", alias="A2", nickname="", uid="u2"),
    ]
    monkeypatch.setattr(trae_prov.TraeProvider, "_checkin_accounts", lambda self: accts)
    monkeypatch.setattr(trae_prov, "ensure_account_token", lambda aid: (f"tok-{aid}", {}))
    monkeypatch.setattr(trae_prov.failover, "display_index", lambda: {"a1": 1, "a2": 2})
    monkeypatch.setattr(trae_prov.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        trae_prov,
        "claim_checkin_credits",
        lambda **_kw: {"code": 0, "message": "ok"},
    )
    monkeypatch.setattr(
        trae_prov,
        "fetch_checkin_status",
        lambda **kw: {"credits": 300, "extra_credits": 0}
        if kw["account_id"] == "a1"
        else {"credits": 200, "extra_credits": 0},
    )

    res = trae_prov.TraeProvider().checkin_claim()
    assert res is not None
    assert res["checked_in"] is True
    assert res["extra_credits"] == 500.0
    assert [a["credits"] for a in res["accounts"]] == [300.0, 200.0]


def test_trae_checkin_claim_fallback_error_is_nonfatal(monkeypatch):
    accts = [SimpleNamespace(id="a1", alias="A1", nickname="", uid="u1")]
    monkeypatch.setattr(trae_prov.TraeProvider, "_checkin_accounts", lambda self: accts)
    monkeypatch.setattr(trae_prov, "ensure_account_token", lambda aid: ("tok", {}))
    monkeypatch.setattr(
        trae_prov,
        "claim_checkin_credits",
        lambda **_kw: {"code": 0, "message": "ok"},
    )

    def _boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(trae_prov, "fetch_checkin_status", _boom)

    res = trae_prov.TraeProvider().checkin_claim()
    assert res is not None
    assert res["checked_in"] is True
    assert res["extra_credits"] is None
