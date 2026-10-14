"""全局常量定义：报表名称、表名、报告期类型等。"""

# 东方财富 datacenter-web 接口的 reportName
REPORT_FINANCIAL_INDICATOR = "RPT_LICO_FN_CPD"
REPORT_INCOME_STATEMENT = "RPT_DMSK_FN_INCOME"
REPORT_BALANCE_SHEET = "RPT_DMSK_FN_BALANCE"

# 三张报表 reportName -> 内部表名后缀
REPORT_NAMES = {
    REPORT_FINANCIAL_INDICATOR: "financial_indicator",
    REPORT_INCOME_STATEMENT: "income_statement",
    REPORT_BALANCE_SHEET: "balance_sheet",
}

# 原始表名
RAW_TABLES = {
    "financial_indicator": "raw_financial_indicator",
    "income_statement": "raw_income_statement",
    "balance_sheet": "raw_balance_sheet",
}

# 清洗表名
CLEAN_TABLES = {
    "financial_indicator": "clean_financial_indicator",
    "income_statement": "clean_income_statement",
    "balance_sheet": "clean_balance_sheet",
}

# DATE_TYPE_CODE 报告期类型：001=年报 002=半年报 003=一季报 004=三季报
DATE_TYPE_CODE_MAP = {
    "001": "annual",      # 年报
    "002": "semi_annual", # 半年报
    "003": "q1",          # 一季报
    "004": "q3",          # 三季报
}

# 报告期月份：年报12月、一季报3月、半年报6月、三季报9月
REPORT_MONTH_TO_TYPE = {
    3: "q1",
    6: "semi_annual",
    9: "q3",
    12: "annual",
}

# 预期发布窗口（月份范围）：[最早月, 最晚月]
# 年报次年1-4月、一季报4月、半年报7-8月、三季报10月
DISCLOSURE_WINDOWS = {
    "annual": (1, 4),
    "q1": (4, 4),
    "semi_annual": (7, 8),
    "q3": (10, 10),
}
