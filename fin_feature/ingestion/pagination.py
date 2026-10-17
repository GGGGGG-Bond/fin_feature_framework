"""2B-2：带原始证据的财务分页。

契约依据：docs/stage-2/2b-contract.md 第 5 节，验收矩阵 B19-B22。

规则：
- 首响应声明 pages；完整取完 1..pages 才 complete=True。
- 合法空 = 仅第一页、pages 为 0 或 1、data=[]、count 为 0 或缺失。
- pages>1 的首空页、任何非首空页、页数/count 变化、行数合计与 count 不符
  均为结构不完整，抛 SchemaFailure；失败不返回部分 records。
- 预知页数超过 max_pages_per_unit 时保留首响应证据，抛 IncompleteScope，
  不把截断结果当成功。
"""

from __future__ import annotations

from fin_feature.ingestion.capture import (
    CapturedPage,
    FetchResult,
    IncompleteScope,
    SchemaFailure,
    SourcedRecord,
)


def _page_reference(page: CapturedPage) -> dict:
    return {
        "page_number": page.page_number,
        "attempt_id": page.attempt_id,
        "snapshot_id": page.snapshot_id,
        "declared_pages": page.declared_pages,
        "source_rows": page.source_rows,
    }


def _sourced_records(page: CapturedPage) -> list[SourcedRecord]:
    return [
        SourcedRecord(
            value=row,
            snapshot_id=page.snapshot_id,
            row_index=index,
            page_number=page.page_number,
            attempt_id=page.attempt_id,
            fetched_at=page.fetched_at,
        )
        for index, row in enumerate(page.rows)
    ]


def fetch_financial_pages(
    executor,
    *,
    unit,
    params_builder,
    max_pages_per_unit: int,
) -> FetchResult:
    """请求一个财务单元的全部页；失败抛类型化异常（不返回部分结果）。

    params_builder(page_number) -> dict，供执行器构造每页请求参数。
    """
    if isinstance(max_pages_per_unit, bool) or not isinstance(max_pages_per_unit, int) \
            or max_pages_per_unit < 1:
        raise ValueError(
            f"max_pages_per_unit 必须为正整数，当前 {max_pages_per_unit!r}"
        )

    first = executor.fetch_page(
        unit, page_number=1, params=params_builder(1)
    )
    declared = first.declared_pages

    # 预知页数超过安全上限：首响应已留存，按 incomplete_scope 失败
    if declared > max_pages_per_unit:
        raise IncompleteScope(
            f"源端声明 {declared} 页，超过 max_pages_per_unit={max_pages_per_unit}："
            "不返回截断成功",
            unit_id=unit["unit_id"], page_number=1,
            attempt_id=first.attempt_id, snapshot_id=first.snapshot_id,
            references=(first.snapshot_id,),
        )

    # 合法空：pages 为 0/1、单页、data=[]、count 为 0 或缺失
    if first.source_rows == 0:
        if declared == 0 or (declared == 1 and first.legal_empty):
            return FetchResult(
                records=(), pages=(first,), complete=True,
                source_row_count=0, declared_pages=declared,
            )
        raise SchemaFailure(
            f"第 1 页 data 为空但声明 pages={declared} 且不满足合法空条件"
            "（合法空要求单页、data=[]、count 为 0 或缺失）",
            unit_id=unit["unit_id"], page_number=1,
            attempt_id=first.attempt_id, snapshot_id=first.snapshot_id,
        )

    pages = [first]
    records = _sourced_records(first)
    total_rows = first.source_rows
    known_count = first.count

    for page_number in range(2, declared + 1):
        page = executor.fetch_page(
            unit, page_number=page_number, params=params_builder(page_number)
        )
        if page.declared_pages != declared:
            raise SchemaFailure(
                f"第 {page_number} 页声明 pages={page.declared_pages}，"
                f"与首响应 pages={declared} 不一致（页数变化）",
                unit_id=unit["unit_id"], page_number=page_number,
                attempt_id=page.attempt_id, snapshot_id=page.snapshot_id,
            )
        if page.source_rows == 0:
            raise SchemaFailure(
                f"第 {page_number} 页 data 为空：多页场景下空页不满足完整性",
                unit_id=unit["unit_id"], page_number=page_number,
                attempt_id=page.attempt_id, snapshot_id=page.snapshot_id,
            )
        if page.count is not None:
            if known_count is None:
                known_count = page.count
            elif page.count != known_count:
                raise SchemaFailure(
                    f"第 {page_number} 页 count={page.count}，与已出现 count="
                    f"{known_count} 不一致",
                    unit_id=unit["unit_id"], page_number=page_number,
                    attempt_id=page.attempt_id, snapshot_id=page.snapshot_id,
                )
        pages.append(page)
        records.extend(_sourced_records(page))
        total_rows += page.source_rows

    if known_count is not None and known_count != total_rows:
        raise SchemaFailure(
            f"源端声明 count={known_count}，但全部页 data 行数合计 {total_rows}",
            unit_id=unit["unit_id"],
            references=tuple(p.snapshot_id for p in pages),
        )

    return FetchResult(
        records=tuple(records),
        pages=tuple(pages),
        complete=True,
        source_row_count=total_rows,
        declared_pages=declared,
    )


def captured_pages_reference(result: FetchResult) -> list[dict]:
    """失败/汇总时使用的页引用（不含业务行）。"""
    return [_page_reference(page) for page in result.pages]
