# 开发指南

[返回文档总索引](../README.md#文档索引)

Linux/WSL Python>=3.12。快速演示：`bash run.sh`。开发及完整验收需额外安装pytest：

```bash
bash scripts/setup_demo.sh
.venv/bin/python -m pip install -c requirements-tested.txt -e '.[dev]'
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
.venv/bin/python scripts/verify_offline.py --output-dir /tmp/new-fin-acceptance
```

演示默认只安装运行依赖；重复启动复用已匹配的环境。依赖或Python版本变化会重新检查。首次安装需要软件包网络，演示不需要财务接口网络。

运行数据、原体、数据库、虚拟环境和私有配置已排除。特征扩展先注册定义与依赖，明确单位/期间/基数规则，再补覆盖实际行为的测试。来源扩展保持证据独立，不混入主结果。

完整验收建议git clone；ZIP可演示，但部分原测试需要Git上下文。真实采集范围见[运行指南](delivery.md)，完整特征样例口径见[面试指南](interview.md)。

## 构建固定交付包

使用干净Git checkout、Linux/WSL的已安装运行环境；先逐页检查一页PDF，再执行：

```bash
.venv/bin/python scripts/package_release.py --brief-pdf /path/project-brief-v0.3.1.pdf --output-dir dist/new-v0.3.1
```

输出源码ZIP、免安装结果ZIP、完整交付ZIP、PDF、release-manifest.json及SHA256SUMS.txt。脚本不会创建标签、推送或发布Release；这些步骤需由发布者核对仓库、身份和固定提交后执行。打包从Git提交读取源码，拒绝脏工作树；运行数据仅在临时目录生成，原体和DB不进入公开资产。下载后的完整包可用Python标准库的scripts/verify_bundle.py校验；发布前以--strict检查全部不可变文件，复算产生的新文件不在原清单中。

## 当前main的文档检查

文档总入口是根README，维护职责见[documentation.md](documentation.md)。执行`python scripts/check_docs.py`检查本地路径和Markdown标题锚点；完整验收脚本已接入。此工具自v0.3.1加入；冻结的v0.3.0源码包不含该脚本。打包版本自动读取pyproject，已有资产保持原身份。
