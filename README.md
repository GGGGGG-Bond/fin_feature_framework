# 财务数据处理与特征工程框架

Python / SQLite / CLI 项目，覆盖财务采集、原始证据、版本管理、清洗、12个特征、双源核验和离线重放。

**只看结果，无需安装：**[演示结果](showcase/README.md) · [特征 CSV](showcase/features.csv) · [文档导航](docs/README.md)。

## 运行离线演示

支持 Linux，Windows 用户使用 WSL。推荐 Python 3.12。首次安装需要软件包网络；演示使用本仓库公开样例，不请求真实金融接口。

```bash
git clone https://github.com/GGGGGG-Bond/fin_feature_framework.git
cd fin_feature_framework
bash scripts/setup_demo.sh
.venv/bin/python scripts/demo.py
```

生成三组结果：公开事实闭环、修订前后时点、人工主流程。输出位于 `data/demo/` 的新时间戳目录，包括数据库、证据、特征 CSV、报告和重放摘要。可加 `--only public`、`--only revision`、`--only artificial` 单独运行。

## 在浏览器中运行

提供 [Codespaces 配置](.devcontainer/devcontainer.json)。发布后可从仓库 Code → Codespaces 创建环境，准备完成后运行：

```bash
.venv/bin/python scripts/demo.py
```

Codespaces 需要登录 GitHub，受使用额度与计费规则约束。配置已提供，云端容器尚未实际创建验证；不是任何访客免登录的在线服务。见 [GitHub 说明](https://docs.github.com/en/codespaces/developing-in-a-codespace/creating-a-codespace-for-a-repository)。

## 能力与边界

- 东方财富主来源、新浪独立核验入口；第二来源不静默混算。
- 原始响应、逐次观察、内容版本、清洗与特征输入可追溯；失败业务单元回滚。
- 显式单位、累计转单季、负基数与不可比诊断；无法计算保留空值和原因。
- 保存历史的 system/public 截至日查询、修订刷新、当前代码离线重放。
- 真实样本96个位置：34已对正式财报核实一致、50有数值待人工核查、12来源字段/定义缺失。
- 公开演示采用32项已核对财报事实重建接口信封，不冒充原始供应商响应。
- 无法恢复未保存的历史版本或未知修订公开时间；现有盈利增长分组不是投资收益回测。

## 测试与技术文档

基础实现已在 Linux 验证770项离线测试通过，11项联网测试未执行。公开候选版的新验证另见[验收记录](docs/acceptance.md)。

```bash
.venv/bin/python scripts/verify_offline.py --output-dir /tmp/new-fin-acceptance
```

完整测试需要 Git 工作目录；ZIP 下载可直接演示，完整验收建议使用 git clone。

阅读：[运行指南](docs/delivery.md) · [架构](docs/architecture.md) · [字段与特征定义](docs/stage-1/README.md) · [版本和血缘](docs/stage-2/README.md) · [来源核验](docs/stage-3/README.md) · [历史查询](docs/stage-4.md)。

本仓库保留经脱敏的96个真实迭代提交和18个合并关系，提交时间与作者姓名保留，SHA因隐私清理而改变。见[历史说明](HISTORY.md)。不包含内部机器配置、私有运行数据库或凭据。尚未指定开源许可证。
