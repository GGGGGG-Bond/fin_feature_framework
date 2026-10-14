"""增量处理测试：水位记录、仅获取新数据、水位更新。"""

from fin_feature.addons.incremental import crawl_incremental
from fin_feature.storage.repository import Repository


def test_watermark_persistence(repo):
    """水位应持久化并可读取。"""
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") is None
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"


def test_watermark_update(repo):
    """水位应可更新为更新的值。"""
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-10-30")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-10-30"


def test_watermark_per_source(repo):
    """不同数据源的水位应独立。"""
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    repo.update_watermark("RPT_DMSK_FN_INCOME", "002501.SZ", "2025-04-30")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"
    assert repo.get_watermark("RPT_DMSK_FN_INCOME", "002501.SZ") == "2025-04-30"


def test_watermark_per_company(repo):
    """不同公司的水位应独立。"""
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    repo.update_watermark("RPT_LICO_FN_CPD", "600000.SH", "2025-04-28")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"
    assert repo.get_watermark("RPT_LICO_FN_CPD", "600000.SH") == "2025-04-28"
