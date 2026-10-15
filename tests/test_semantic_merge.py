"""语义合并测试：字段级权威、空值不覆盖、冲突诊断、回退诊断。

对应契约 T10、T11、T12。
"""

from fin_feature.features.semantic import (
    merge_three_tables_semantic,
    merge_three_tables_semantic_with_fallback_flag,
)
from fin_feature.storage.repository import Repository


def _seed_clean(repo, table_suffix, secucode, report_date, **fields):
    """写入一条清洗记录。"""
    record = {"secucode": secucode, "report_date": report_date}
    record.update(fields)
    repo.upsert_clean(table_suffix, record)


class TestFieldAuthority:
    """T10: 非权威表空值不覆盖权威表有效值。"""

    def test_authoritative_value_used(self, repo):
        """权威表有值时使用权威值。"""
        _seed_clean(repo, "income_statement", "002501.SZ", "2024-12-31",
                    total_operate_income=110.0, parent_netprofit=20.0)
        _seed_clean(repo, "financial_indicator", "002501.SZ", "2024-12-31",
                    total_operate_income=100.0, parent_netprofit=15.0,
                    weightavg_roe=0.16, gross_margin=0.30)

        merged, diags = merge_three_tables_semantic(repo, "002501.SZ", "2024-12-31")

        # IS 权威：用 110 而非 FI 的 100
        assert merged["total_operate_income"] == 110.0
        assert merged["parent_netprofit"] == 20.0
        # FI 权威
        assert merged["weightavg_roe"] == 0.16
        assert merged["gross_margin"] == 0.30

    def test_non_authoritative_none_doesnt_overwrite(self, repo):
        """T10: 非权威表 None 不覆盖权威表有效值。"""
        _seed_clean(repo, "income_statement", "002501.SZ", "2024-12-31",
                    total_operate_income=110.0, parent_netprofit=20.0)
        # FI 有营收但为 None
        _seed_clean(repo, "financial_indicator", "002501.SZ", "2024-12-31",
                    total_operate_income=None, parent_netprofit=None,
                    weightavg_roe=0.16)

        merged, diags = merge_three_tables_semantic(repo, "002501.SZ", "2024-12-31")

        # IS 有效值保留，不被 FI 的 None 覆盖
        assert merged["total_operate_income"] == 110.0
        assert merged["parent_netprofit"] == 20.0


class TestSourceConflict:
    """T11: 权威表与回退表均有效时记录 source_conflict。"""

    def test_source_conflict_logged(self, repo):
        """IS=110、FI=100 均有效 → 用 110，记录 source_conflict。"""
        _seed_clean(repo, "income_statement", "002501.SZ", "2024-12-31",
                    total_operate_income=110.0, parent_netprofit=20.0,
                    notice_date="2025-04-30")
        _seed_clean(repo, "financial_indicator", "002501.SZ", "2024-12-31",
                    total_operate_income=100.0, parent_netprofit=15.0,
                    weightavg_roe=0.16, notice_date="2025-04-29")

        merged, diags = merge_three_tables_semantic(repo, "002501.SZ", "2024-12-31")

        # 使用权威值 110
        assert merged["total_operate_income"] == 110.0
        # 有 source_conflict 诊断
        conflicts = [d for d in diags if d["reason_code"] == "source_conflict"]
        assert len(conflicts) >= 1
        # 诊断包含双方值
        conflict = conflicts[0]
        assert conflict["auth_value"] == 110.0
        assert conflict["fallback_value"] == 100.0


class TestFallback:
    """T12: 权威表缺失时的回退诊断。"""

    def test_no_fallback_when_incomparable(self, repo):
        """T12: IS 缺失、FI 有效但可比性未知 → None + incomparable_basis。"""
        # FI 有营收（作为回退源）
        _seed_clean(repo, "financial_indicator", "002501.SZ", "2024-12-31",
                    total_operate_income=100.0, parent_netprofit=15.0,
                    weightavg_roe=0.16)
        # IS 缺失（不写入）

        merged, diags = merge_three_tables_semantic(repo, "002501.SZ", "2024-12-31")

        # 真实数据默认不可比 → 拒绝回退
        assert merged["total_operate_income"] is None
        incomparable = [d for d in diags if d["reason_code"] == "incomparable_basis"]
        assert len(incomparable) >= 1

    def test_fallback_when_comparable(self, repo):
        """T12: IS 缺失、FI 有效且 allow_fallback=True → 回退 + fallback_used。"""
        _seed_clean(repo, "financial_indicator", "002501.SZ", "2024-12-31",
                    total_operate_income=100.0, parent_netprofit=15.0,
                    weightavg_roe=0.16)

        merged, diags = merge_three_tables_semantic_with_fallback_flag(
            repo, "002501.SZ", "2024-12-31", allow_fallback=True
        )

        # 允许回退 → 使用 FI 值
        assert merged["total_operate_income"] == 100.0
        fallbacks = [d for d in diags if d["reason_code"] == "fallback_used"]
        assert len(fallbacks) >= 1


class TestNoticeDateNotOverwritten:
    """notice_date 等元信息随各自来源表，不被另一表覆盖。"""

    def test_notice_date_preserved_per_source(self, repo):
        """各字段保留其来源表的 notice_date。"""
        _seed_clean(repo, "income_statement", "002501.SZ", "2024-12-31",
                    total_operate_income=110.0, parent_netprofit=20.0,
                    notice_date="2025-04-30")
        _seed_clean(repo, "financial_indicator", "002501.SZ", "2024-12-31",
                    weightavg_roe=0.16, gross_margin=0.30,
                    notice_date="2025-04-29")
        _seed_clean(repo, "balance_sheet", "002501.SZ", "2024-12-31",
                    total_assets=1000.0, total_liabilities=600.0,
                    notice_date="2025-02-01")

        merged, diags = merge_three_tables_semantic(repo, "002501.SZ", "2024-12-31")

        sources = merged.get("__sources__", {})
        # IS 字段的 notice_date 应为 IS 的 2025-04-30
        is_source = sources.get("total_operate_income")
        assert is_source is not None
        assert is_source.notice_date == "2025-04-30"
        # BS 字段的 notice_date 应为 BS 的 2025-02-01
        bs_source = sources.get("total_assets")
        assert bs_source is not None
        assert bs_source.notice_date == "2025-02-01"
