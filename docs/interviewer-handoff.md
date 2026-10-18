# 成果查看与运行指南

[返回文档总索引](../README.md#文档索引)

项目固定版本为[v0.4.2](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v0.4.2)。源码和真实迭代历史在仓库；Release资产绑定固定提交，后续main变化不影响本次交付。

| 下载物 | 面向用途 | 如何开始 |
|---|---|---|
| delivery.zip（完整包） | 一次下载，查看与复现兼备 | 解压，打开START_HERE.html；代码位于code/ |
| results.zip（免安装结果） | 只想看图表、CSV、JSON与说明 | 解压，打开START_HERE.html；不需要Python、Git或WSL |
| source.zip（源码快照） | 本地复算、看代码及测试 | Linux运行bash run.sh；Windows用下方原生Python命令，或用WSL快捷入口 |
| project-brief PDF | 快速理解项目范围与结果 | 一页说明；实际测试及边界仍以完整文档为准 |
| SHA256SUMS.txt / release-manifest.json | 下载校验与版本身份 | 比较SHA-256；完整包另附逐文件MANIFEST.json |

文件名带fin-feature-framework-v0.4.2前缀；无需同时下载三个ZIP。仅Git克隆保留完整提交历史，源码ZIP是固定提交快照。

## 查看与复现

免安装结果包的HTML无外部脚本或CDN；公司/期间筛选和净值图可直接在现代浏览器中使用。附CSV、JSON、来源URL和PDF摘要。不含数据库、原始HTTP响应、虚拟环境或私有配置；要重建演示证据请运行源码。

Linux/WSL：Python≥3.12，含venv/pip，首次安装需包源网络。

```bash
git clone --branch v0.4.2 https://github.com/GGGGGG-Bond/fin_feature_framework.git
cd fin_feature_framework
bash run.sh
```

Windows原生Python≥3.12（PowerShell，在源码目录执行）：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -c requirements-tested.txt -e .
.venv\Scripts\python.exe -X utf8 scripts/demo.py --open
```

完整包先进入`code/`目录；首次安装需包源网络，默认演示不访问金融接口。使用支持硬链接的本地文件系统（如NTFS）。普通演示无需管理员或符号链接权限；安全测试若创建符号链接，另需开发者模式或相应权限。Windows跳过目录fsync，适用于本地演示，不提供与Linux相同的断电持久性保证。

Windows/WSL：已装好WSL及其中的Python≥3.12时，源码目录双击`run-demo.cmd`，完整包根目录双击`RUN_DEMO.cmd`。脚本自动选择有bash和Python≥3.12的已安装WSL分发版（可用FIN_DEMO_DISTRO指定），不更改默认分发版或执行策略；再把公开运行代码复制到WSL的独立临时目录，在Linux文件系统中运行，以满足证据目录fsync语义；不改源目录和Git历史。查看用文件导出到源目录`data/windows-demo/`，成功后尝试用默认浏览器打开入口。运行及依赖目录保留在终端打印的Linux路径，便于排查；不会自动安装WSL或修改系统Python。没有WSL时可使用上述原生Python命令，或直接打开免安装结果。

macOS可直接查看结果包；本次不承诺macOS重算。Codespaces配置存在但尚未创建实测，不能替代已验证的本地入口。ZIP源码可演示，完整测试中的Git上下文检查建议使用克隆目录。

## 验收与问题定位

```bash
.venv/bin/python -m pip install -c requirements-tested.txt -e '.[dev]'
.venv/bin/python scripts/verify_offline.py --output-dir /tmp/new-fin-acceptance
```

所有输出目录须是新目录。默认运行8公司展示、三公司正式财报完整特征及滚动成本研究，`--only all`另含独立公开事实、修订和人工流程。程序失败会非零退出，日志位于打印的输出目录；不可计算的原因随结果保留。

校验下载：Linux用`sha256sum -c SHA256SUMS.txt`；Windows可用`Get-FileHash 文件名 -Algorithm SHA256`对照。完整包可执行`python code/scripts/verify_bundle.py .`核对内部全部文件；它只用Python标准库，不访问网络。校验用于检测传输变化，不是第三方签名或独立认证。

本次固定验收8公司、8季度，真实采样和正式财报事实重建分别标注。供应商视图768结果/232有值；正式财报三公司目标36/36有值，核验144/144重放一致，两个分母分别统计；96个双源位置的3处后续调整差异和12处缺口保留。回测是手选小样本、前复权价格收益，不构成策略有效性证据。完整范围见progress/acceptance/showcase文档。

发送时可直接复制[邮件与即时消息模板](submission-message.md)。最新文档导航在[根README](../README.md#文档索引)，固定交付资产及包内文档仍对应v0.4.2发布提交，持续整理main不会覆盖已发文件。
