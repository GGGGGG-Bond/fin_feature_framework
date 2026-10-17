"""Optional online verifier bounds fail before creating output or opening a session."""
import pytest
from scripts.verify_live import validate_scope,run_live


@pytest.mark.parametrize('company,start,end',[
    ('600519.SH','2024-09-30','2024-12-31'),('000001.SZ','2024-12-31','2025-03-31'),
    ('600519.SH','2024-12-31','2024-12-31')])
def test_bounded_scope_accepts_at_most_adjacent_quarters(company,start,end):
    validate_scope(company,start,end)


@pytest.mark.parametrize('company,start,end',[
    ('all','2024-09-30','2024-12-31'),('600519.SH','2023-12-31','2024-12-31'),
    ('600519.SH','2024-12-31','2024-09-30'),('600519.SH','2024-09-01','2024-12-31'),
    ('600519.SH','2024/09/30','2024-12-31')])
def test_invalid_or_large_live_scope_rejected_before_io(tmp_path,monkeypatch,company,start,end):
    import requests
    def forbidden(*args,**kwargs):raise AssertionError('session must not be created')
    monkeypatch.setattr(requests,'Session',forbidden)
    with pytest.raises(ValueError):run_live(tmp_path/'out',company,start,end)
    assert not (tmp_path/'out').exists()
