# Auto Bugfix Agent MVP

一个可直接运行的代码库维护 / 自动修 Bug Agent MVP。

它会：

1. 在目标 Git 仓库里运行测试或检查命令。
2. 捕获失败日志。
3. 自动收集相关文件上下文。
4. 调用 OpenAI 兼容模型生成 unified diff 补丁。
5. 校验补丁路径，避免越权修改。
6. 应用补丁。
7. 再次运行测试，最多循环 `--max-iterations` 次。

## 1. 安装

```bash
cd auto-bugfix-agent-mvp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

设置 API Key：

```bash
export OPENAI_API_KEY="your_api_key"
```

## 2. 先跑内置 Demo

初始化示例仓库：

```bash
cd examples/buggy_py_project
git init
git add .
git commit -m "init buggy demo"
cd ../..
```

运行 agent：

```bash
python -m auto_bugfix_agent \
  --repo examples/buggy_py_project \
  --test-cmd "pytest -q" \
  --model gpt-4.1-mini
```

运行成功后，查看它改了什么：

```bash
cd examples/buggy_py_project
git diff
pytest -q
```

## 3. 在你自己的项目里运行

```bash
python -m auto_bugfix_agent \
  --repo /path/to/your/repo \
  --test-cmd "pytest -q" \
  --model gpt-4.1-mini
```

Node 项目示例：

```bash
python -m auto_bugfix_agent \
  --repo /path/to/your/repo \
  --test-cmd "npm test" \
  --model gpt-4.1-mini
```

## 4. Dry Run：只看补丁，不修改文件

```bash
python -m auto_bugfix_agent \
  --repo /path/to/your/repo \
  --test-cmd "pytest -q" \
  --dry-run
```

## 5. 常用参数

```text
--repo                 目标 Git 仓库，默认当前目录
--test-cmd             测试/检查命令，必填
--model                模型名，默认 gpt-4.1-mini
--max-iterations       最多修复循环次数，默认 3
--dry-run              只输出补丁，不应用
--allow-dirty          允许目标仓库有未提交改动
--command-timeout      测试命令超时时间，默认 120 秒
```

## 6. 安全设计

默认行为偏保守：

- 目标必须是 Git 仓库。
- 默认要求工作区干净，避免覆盖你已有改动。
- 只接受 unified diff 补丁。
- 拒绝修改仓库外路径。
- 拒绝修改常见二进制、构建产物、依赖目录、lock 文件。
- 不会自动 commit。

## 7. 项目结构

```text
auto-bugfix-agent-mvp/
  auto_bugfix_agent/
    __init__.py
    __main__.py
    agent.py
  examples/
    buggy_py_project/
      calculator.py
      tests/test_calculator.py
  requirements.txt
  pyproject.toml
  run_agent.sh
  README.md
```
