# 开发指南

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
