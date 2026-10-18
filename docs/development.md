# 开发指南

Linux Python3.12推荐；Windows使用WSL。进入仓库后运行：

```bash
bash scripts/setup_demo.sh
.venv/bin/python scripts/demo.py
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
```

运行数据、原体、数据库、虚拟环境和私有配置已排除。特征扩展先注册定义与依赖，明确单位/期间/基数规则，再补覆盖实际行为的测试。来源扩展保持证据独立，不混入主结果。

完整验收建议用Git克隆目录；ZIP可以直接演示，但一项原测试需要Git忽略规则上下文。配置及真实采集范围见[运行指南](delivery.md)，离线演示入口不发起财务网络请求。
