# 财务数据处理与特征工程框架

Python / SQLite / CLI：财务采集 → 清洗 → 12项特征 → 质量与来源核验。

**无需安装先看结果：**[12项完整样例](showcase/interview.md) · [三公司公开结果](showcase/README.md) · [当前完成度与边界](docs/progress.md)。

## 克隆后运行

需要 **Linux + Python 3.12或更新版本（含venv/pip）**；Windows在WSL中执行。首次安装需要软件包网络，演示本身不请求金融接口。

```bash
git clone https://github.com/GGGGGG-Bond/fin_feature_framework.git
cd fin_feature_framework
bash run.sh
```

一条命令完成环境准备、默认演示和HTML结果总览。终端会打印 `Overview: .../index.html`，用浏览器打开该文件即可；每次在 `data/demo/` 创建新目录，不覆盖前次结果。

```bash
bash run.sh --open         # 请求本机浏览器打开结果；WSL/无桌面环境可手动打开
bash run.sh --only all     # 完整样例、三公司公开事实、修订、人工流程四组演示
```

已安装多个Python时可用 `PYTHON=python3.12 bash run.sh` 指定。重复运行会复用依赖环境；测试环境另按[开发指南](docs/development.md)安装pytest。原生Windows暂不支持证据文件所需的目录fsync。

## 默认演示看什么

- 茅台三份正式财报的27项固定事实，覆盖2023年末、2024半年、三季、年末。
- **2024年末12/12特征有值**，包含ROE变化、营收/利润同比和单季环比、流动比率变化。
- 走实际证据文件、SQLite版本、清洗、特征和重放路径；全范围48条结果中19条有值，其余期间保留空值原因。
- HTML展示中文名称、百分比/百分点/倍数、拒算案例、CSV、质量报告和来源文件链接。

输入信封由正式财报事实重建，**不是供应商原始响应或本次真实采集**。流动比率由披露的流动资产/流动负债计算，毛利率由营业收入/营业成本计算；固定输入附有内容绑定的人工口径声明，默认真实采集的未知单位与可比性仍拒算。详情见[演示口径](docs/interview.md)。

## 能力与限制

采集限频与分页、失败单元回滚、原始证据、修订版本、清洗诊断、特征血缘、质量报告和离线重放均已实现。东方财富主来源，新浪独立核验，第二来源不静默拼接。

原双源样本96个位置：34已对正式财报核实一致、50有值待人工核查、12字段/定义缺失。新增面试样例不改变这组统计。未知修订公开时间、未保存历史与全市场覆盖仍有边界；**投资收益回测属于未完成附加项**，现有盈利增长分组不作为替代。

## 更多入口

[文档导航](docs/README.md) · [安装与CLI](docs/delivery.md) · [测试与验收](docs/acceptance.md) · [财务定义](docs/stage-1/README.md)。阶段契约保留历史设计背景，当前状态以progress/acceptance为准。

提供[Codespaces配置](.devcontainer/devcontainer.json)，但云端实例尚未实测。它需要GitHub登录及可用额度，不是免登录网页演示；见[GitHub说明](https://docs.github.com/en/codespaces/developing-in-a-codespace/creating-a-codespace-for-a-repository)。

保留经脱敏的96个原始迭代提交和18个合并关系；后续公开改动继续正常提交。[历史说明](HISTORY.md)。尚未指定开源许可证。
