"""字段映射：东方财富 API 大写字段名 -> 规范 snake_case 字段名。

三个报表各有独立映射表。映射集中管理，便于新增数据源时统一字段命名。
"""

# 通用字段（三表共有）
_COMMON_FIELDS = {
    "SECUCODE": "secucode",
    "SECURITY_CODE": "security_code",
    "SECURITY_NAME_ABBR": "security_name",
    "NOTICE_DATE": "notice_date",
    "EITIME": "entry_time",
    "ORG_CODE": "org_code",
    "TRADE_MARKET": "trade_market",
    "SECURITY_TYPE": "security_type",
}

# 主要财务指标 RPT_LICO_FN_CPD
FINANCIAL_INDICATOR_FIELDS = {
    **_COMMON_FIELDS,
    "REPORTDATE": "report_date",
    "QDATE": "qdate",
    "DATATYPE": "datatype",
    "DATAYEAR": "data_year",
    "DATEMMDD": "date_mmdd",
    "BASIC_EPS": "basic_eps",
    "DEDUCT_BASIC_EPS": "deduct_basic_eps",
    "TOTAL_OPERATE_INCOME": "total_operate_income",
    "PARENT_NETPROFIT": "parent_netprofit",
    "WEIGHTAVG_ROE": "weightavg_roe",
    "YSTZ": "revenue_yoy",          # 营收同比增速
    "SJLTZ": "profit_yoy",          # 净利润同比增速
    "BPS": "bps",                   # 每股净资产
    "MGJYXJJE": "ocf_per_share",    # 每股经营现金流
    "XSMLL": "gross_margin",        # 毛利率
    "YSHZ": "revenue_qoq",          # 营收环比增速
    "SJLHZ": "profit_qoq",          # 净利润环比增速
    "ISNEW": "is_new",
    "PAYYEAR": "pay_year",
    "ASSIGNDSCRPT": "assign_dscrpt",
    "UPDATE_DATE": "update_date",
}

# 利润表 RPT_DMSK_FN_INCOME
INCOME_STATEMENT_FIELDS = {
    **_COMMON_FIELDS,
    "REPORT_DATE": "report_date",
    "DATE_TYPE_CODE": "date_type_code",
    "REPORT_TYPE_CODE": "report_type_code",
    "DATA_STATE": "data_state",
    "INDUSTRY_NAME": "industry_name",
    "TOTAL_OPERATE_INCOME": "total_operate_income",
    "TOTAL_OPERATE_COST": "total_operate_cost",
    "OPERATE_COST": "operate_cost",
    "OPERATE_EXPENSE": "operate_expense",
    "SALE_EXPENSE": "sale_expense",
    "MANAGE_EXPENSE": "manage_expense",
    "FINANCE_EXPENSE": "finance_expense",
    "OPERATE_PROFIT": "operate_profit",
    "TOTAL_PROFIT": "total_profit",
    "INCOME_TAX": "income_tax",
    "OPERATE_INCOME": "operate_income",
    "INVEST_INCOME": "invest_income",
    "PARENT_NETPROFIT": "parent_netprofit",
    "DEDUCT_PARENT_NETPROFIT": "deduct_parent_netprofit",
    "TOE_RATIO": "toe_ratio",
    "OPERATE_EXPENSE_RATIO": "operate_expense_ratio",
    "OPERATE_PROFIT_RATIO": "operate_profit_ratio",
    "PARENT_NETPROFIT_RATIO": "parent_netprofit_ratio",
}

# 资产负债表 RPT_DMSK_FN_BALANCE
BALANCE_SHEET_FIELDS = {
    **_COMMON_FIELDS,
    "REPORT_DATE": "report_date",
    "DATE_TYPE_CODE": "date_type_code",
    "REPORT_TYPE_CODE": "report_type_code",
    "DATA_STATE": "data_state",
    "INDUSTRY_NAME": "industry_name",
    "TOTAL_ASSETS": "total_assets",
    "FIXED_ASSET": "fixed_asset",
    "MONETARYFUNDS": "monetary_funds",
    "ACCOUNTS_RECE": "accounts_receivable",
    "INVENTORY": "inventory",
    "TOTAL_LIABILITIES": "total_liabilities",
    "ACCOUNTS_PAYABLE": "accounts_payable",
    "TOTAL_EQUITY": "total_equity",
    "CURRENT_RATIO": "current_ratio",
    "DEBT_ASSET_RATIO": "debt_asset_ratio",
    "TOTAL_LIAB_RATIO": "total_liab_ratio",
    "TOTAL_ASSETS_RATIO": "total_assets_ratio",
    "TOTAL_EQUITY_RATIO": "total_equity_ratio",
}

# reportName -> 字段映射表
FIELD_MAPPINGS = {
    "RPT_LICO_FN_CPD": FINANCIAL_INDICATOR_FIELDS,
    "RPT_DMSK_FN_INCOME": INCOME_STATEMENT_FIELDS,
    "RPT_DMSK_FN_BALANCE": BALANCE_SHEET_FIELDS,
}

# 表后缀 -> 字段映射表（用于存储层按表名映射）
TABLE_FIELD_MAPPINGS = {
    "financial_indicator": FINANCIAL_INDICATOR_FIELDS,
    "income_statement": INCOME_STATEMENT_FIELDS,
    "balance_sheet": BALANCE_SHEET_FIELDS,
}


def map_fields(raw_record: dict, report_name: str) -> dict:
    """将原始 API 记录的大写字段映射为 snake_case 字段。

    未在映射表中的字段会被忽略（丢弃），保证入库字段统一。
    """
    mapping = FIELD_MAPPINGS.get(report_name)
    if mapping is None:
        raise ValueError(f"未知的 reportName: {report_name}，无字段映射")
    mapped = {}
    for raw_key, value in raw_record.items():
        snake_key = mapping.get(raw_key)
        if snake_key is not None:
            mapped[snake_key] = value
    return mapped


def map_fields_by_table(raw_record: dict, table_suffix: str) -> dict:
    """按表后缀（financial_indicator/income_statement/balance_sheet）映射字段。"""
    mapping = TABLE_FIELD_MAPPINGS.get(table_suffix)
    if mapping is None:
        raise ValueError(f"未知的表后缀: {table_suffix}，无字段映射")
    mapped = {}
    for raw_key, value in raw_record.items():
        snake_key = mapping.get(raw_key)
        if snake_key is not None:
            mapped[snake_key] = value
    return mapped


def get_mapped_columns(table_suffix: str) -> list[str]:
    """返回某张表映射后的所有字段名（snake_case）。"""
    mapping = TABLE_FIELD_MAPPINGS.get(table_suffix)
    if mapping is None:
        raise ValueError(f"未知的表后缀: {table_suffix}")
    return list(mapping.values())
