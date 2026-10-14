"""特征回测框架：对生成的特征进行分组回测。

回测逻辑：
1. 在分组日（formation_date）按特征值排序，分 N 组（默认5组）
2. 计算各组在下一报告期的"收益代理"（默认：净利润同比增速）
3. 输出各组收益、多空收益（最高组 - 最低组）

收益代理说明：本项目仅爬财务数据，无行情数据。
采用"下一报告期净利润同比增速"作为收益代理，纯财务数据驱动。
"""

from dataclasses import dataclass, field

from fin_feature.storage.repository import Repository
from fin_feature.utils.date_utils import parse_date
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.addons.backtest")


@dataclass
class BacktestResult:
    """回测结果。"""

    feature_name: str
    formation_date: str
    n_groups: int
    group_stats: list[dict] = field(default_factory=list)
    long_short: float | None = None
    total_samples: int = 0


def _next_period_profit_yoy(
    repo: Repository, secucode: str, formation_date: str
) -> float | None:
    """获取分组日下一报告期的净利润同比增速（收益代理）。

    查找 formation_date 之后最近的报告期，取其 profit_yoy 特征值。
    """
    sql = (
        "SELECT feature_value FROM features "
        "WHERE secucode = ? AND feature_name = 'profit_yoy' "
        "AND report_date > ? AND feature_value IS NOT NULL "
        "ORDER BY report_date ASC LIMIT 1"
    )
    row = repo.conn.execute(sql, (secucode, formation_date)).fetchone()
    return row[0] if row else None


def run_backtest(
    repo: Repository,
    feature_name: str,
    formation_date: str,
    n_groups: int = 5,
    return_proxy: str = "next_profit_yoy",
) -> BacktestResult:
    """执行分组回测。

    feature_name: 用于分组的特征名
    formation_date: 分组日（报告期）
    n_groups: 分组数
    return_proxy: 收益代理（目前仅支持 next_profit_yoy）
    """
    result = BacktestResult(
        feature_name=feature_name,
        formation_date=formation_date,
        n_groups=n_groups,
    )

    # 1. 获取分组日的特征值
    feature_rows = repo.get_feature_series(feature_name, formation_date)
    if not feature_rows:
        logger.warning("分组日 %s 无特征 %s 的数据", formation_date, feature_name)
        return result

    result.total_samples = len(feature_rows)

    # 2. 按特征值排序
    sorted_rows = sorted(feature_rows, key=lambda r: r["feature_value"])

    # 3. 分 N 组（均分）
    group_size = len(sorted_rows) // n_groups
    if group_size == 0:
        logger.warning("样本数 %d 不足以分 %d 组", len(sorted_rows), n_groups)
        return result

    groups: list[list] = []
    for i in range(n_groups):
        start = i * group_size
        if i == n_groups - 1:
            # 最后一组包含剩余全部
            groups.append(sorted_rows[start:])
        else:
            groups.append(sorted_rows[start:start + group_size])

    # 4. 计算各组收益代理
    group_stats: list[dict] = []
    for i, group in enumerate(groups):
        returns: list[float] = []
        for row in group:
            secucode = row["secucode"]
            if return_proxy == "next_profit_yoy":
                ret = _next_period_profit_yoy(repo, secucode, formation_date)
            else:
                ret = None
            if ret is not None:
                returns.append(ret)

        avg_return = sum(returns) / len(returns) if returns else None
        stat = {
            "group": i + 1,
            "size": len(group),
            "valid_returns": len(returns),
            "avg_return": avg_return,
            "feature_range": (
                group[0]["feature_value"],
                group[-1]["feature_value"],
            ),
        }
        group_stats.append(stat)

    result.group_stats = group_stats

    # 5. 多空收益（最高组 - 最低组）
    if group_stats and group_stats[0]["avg_return"] is not None and group_stats[-1]["avg_return"] is not None:
        result.long_short = group_stats[-1]["avg_return"] - group_stats[0]["avg_return"]

    logger.info(
        "回测完成: 特征=%s 分组日=%s 样本=%d 多空收益=%s",
        feature_name, formation_date, result.total_samples,
        f"{result.long_short:.4f}" if result.long_short is not None else "N/A",
    )
    return result


def format_report(result: BacktestResult) -> str:
    """将回测结果格式化为可读报告。"""
    lines = [
        f"=== 特征回测报告 ===",
        f"特征: {result.feature_name}",
        f"分组日: {result.formation_date}",
        f"分组数: {result.n_groups}",
        f"总样本: {result.total_samples}",
        "",
        f"{'组别':<6}{'样本':<6}{'有效':<6}{'平均收益':<12}{'特征值范围':<24}",
        "-" * 60,
    ]
    for stat in result.group_stats:
        avg = f"{stat['avg_return']:.4f}" if stat["avg_return"] is not None else "N/A"
        fr = stat["feature_range"]
        fr_str = f"[{fr[0]:.4f}, {fr[1]:.4f}]" if fr[0] is not None else "N/A"
        lines.append(
            f"{stat['group']:<6}{stat['size']:<6}{stat['valid_returns']:<6}{avg:<12}{fr_str:<24}"
        )
    lines.append("-" * 60)
    ls = f"{result.long_short:.4f}" if result.long_short is not None else "N/A"
    lines.append(f"多空收益 (最高组-最低组): {ls}")
    lines.append("")
    lines.append("注: 收益代理为「下一报告期净利润同比增速」，纯财务数据驱动，无行情数据。")
    return "\n".join(lines)
