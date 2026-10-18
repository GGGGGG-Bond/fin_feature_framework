# 交付时发送的消息与文件链接

[返回文档总索引](../README.md#文档索引)

适用范围：已发布v0.4.1。以下是可复制的文案；本文不代表已向任何人发送消息。首次交付推荐“固定发布页 + 完整ZIP”，源码仓库用于阅读文档和迭代历史。

## 邮件或正式消息

主题：财务数据与特征工程框架项目交付（v0.4.1）

```text
您好，现提交“财务数据与特征工程框架”项目，交付版本为 v0.4.1。

项目包含公开财务采集、SQLite存储、清洗、12项盈利/成长/偿债特征、双源与正式财报核验，以及带成本、基准和净值的价格收益分组回测及滚动形成/成本敏感性研究。

固定交付页：
https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v0.4.1

完整交付包：
https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/fin-feature-framework-v0.4.1-delivery.zip

源码、文档索引及迭代历史：
https://github.com/GGGGGG-Bond/fin_feature_framework

解压完整包后打开 START_HERE.html，即可免安装查看八家公司、八个季度的结果、双源对照、净值图和一页说明。需要复现时，Linux/WSL Python≥3.12运行 bash run.sh；已配置WSL及其中Python的Windows可双击 RUN_DEMO.cmd。首次安装需要包源网络，默认演示不访问金融接口。

本次限定范围通过离线回归及GitHub Python3.12/3.14验收，主样本768/768冻结输入重放一致。可计算值与拒算原因同时保留；另有三公司正式财报视图验证36/36目标结果、144/144重放一致。固定展示使用真实采样字段，离线API信封为重建；小样本回测用于验证计算流程。

感谢审阅，期待就数据口径、修订处理和可复现性进一步交流。
```

## 即时消息简版

```text
您好，财务数据与特征工程框架已整理为固定交付版本 v0.4.1，包含源码、八公司结果、三公司完整特征、双源核验、价格收益、滚动与成本研究和一页说明。

交付页：https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v0.4.1
源码与文档：https://github.com/GGGGGG-Bond/fin_feature_framework

建议下载交付页中的delivery.zip完整交付包，解压打开 START_HERE.html 即可免安装查看。需要复现时，Linux/WSL运行 bash run.sh；Windows配好WSL和Python后双击 RUN_DEMO.cmd。包内有详细前提、测试记录及范围说明。
```

## 文件链接备查

| 文件 | 链接及用途 |
|---|---|
| 固定发布页 | [v0.4.1](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v0.4.1)，包含全部文件与版本说明 |
| 完整交付包 | [delivery.zip](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/fin-feature-framework-v0.4.1-delivery.zip)，完整ZIP，首选，仅需这一个ZIP |
| 免安装结果包 | [results.zip](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/fin-feature-framework-v0.4.1-results.zip)，便携结果，仅看结果时选用 |
| 源码快照 | [source.zip](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/fin-feature-framework-v0.4.1-source.zip)，固定快照；Git历史通过仓库查看 |
| 一页说明 | [PDF](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/project-brief-v0.4.1.pdf)，约58KB，完整包内已包含 |
| 校验与身份 | [SHA256SUMS](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/SHA256SUMS.txt)、[版本资产清单](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.1/release-manifest.json) |

若以附件发送，附完整ZIP即可；一页PDF可另附以便快速阅读，不需要再同时附results/source两个ZIP。不要发送本机file路径、内部GitLab、原始响应、数据库、虚拟环境或私有配置。交付主体始终固定为v0.4.1，main后续文档更新不改变已有资产身份。
