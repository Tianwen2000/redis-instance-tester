# Redis Instance Tester

这是一个面向云 Redis 实例的可配置 Python 测试工具。它通过 `redis-py` 执行常规数据面测试，支持命令行覆盖、JSON 配置、测试套件组合、JSON 报告和自动清理。

当前示例配置对应：

```text
实例：按量计费开始
实例 ID：crs-8hz033uk
地址：10.0.0.17:6379
架构：Redis 4.x master-slave
副本：1
```

## 为什么使用 Python

相较于 Bash，Python 更适合长期维护这类测试：

- 参数和配置校验更可靠；
- 每个用例都有明确断言；
- 支持并发原子性和轻量性能测试；
- 可以稳定生成结构化 JSON 报告；
- 异常处理中仍能执行测试数据清理；
- 后续容易接入 pytest、CI 和云平台 API。

## 文件结构

```text
redis-instance-tester/
├── redis_instance_test.py     # 主测试程序
├── redis-test.example.json    # 示例配置
├── requirements.txt           # Python 依赖
├── requirements-dev.txt       # 离线测试依赖
├── tests/                     # 脚本自身的单元测试
└── README.md                  # 使用说明
```

## 环境要求

- Python 3.8 或更高版本
- Redis 实例网络可达
- `redis-py 4.5 - 5.x`

建议使用独立虚拟环境：

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

不要使用 CentOS 7 自带的 Python 2.7 运行本工具。

## 上传到朱雀云服务器

在本地 WSL 中执行，路径可按实际位置调整：

```bash
scp -i ~/.ssh/zhuque.pem -r \
  /mnt/c/Users/tianw/Documents/Codex/2026-07-29/w/outputs/019facbc-e25b-7a72-a1f3-0bf8eec006a0/redis-instance-tester \
  root@151.243.153.58:/root/
```

登录服务器并进入目录：

```bash
ssh -i ~/.ssh/zhuque.pem root@151.243.153.58
cd /root/redis-instance-tester
```

## 快速运行

标准测试会隐藏输入 Redis 密码：

```bash
python redis_instance_test.py --config redis-test.example.json
```

只执行冒烟测试：

```bash
python redis_instance_test.py \
  --config redis-test.example.json \
  --profile smoke
```

临时更换目标实例，无需修改配置文件：

```bash
python redis_instance_test.py \
  --config redis-test.example.json \
  --host 10.0.0.39 \
  --port 6379 \
  --profile smoke
```

如果实例明确设计为免密访问：

```bash
python redis_instance_test.py \
  --config redis-test.example.json \
  --host 10.0.0.17 \
  --no-auth
```

不要为了让测试通过而随意使用 `--no-auth`。如果产品要求密码认证，而未认证连接能够执行 `PING/SET/GET`，认证 suite 应保留失败结果。

## 测试模式

### smoke

```text
网络连通性、未认证访问检查、PING、String 基础读写
```

### standard

```text
smoke + Server INFO + Hash/List/Set/ZSet + TTL + 事务 + Lua
+ 错误处理 + 并发原子性 + 主从复制 + 持久化观察 + 健康状态
```

### performance

```text
standard + Python 客户端轻量 SET/GET 性能测试
```

该性能结果用于实例间回归比较，不等同于专业容量评估。正式容量测试应使用专用压测机、固定网络条件和独立测试方案。

### cluster

```text
Cluster 连接、数据结构、TTL、Lua、原子递增和槽位健康状态
```

运行 Cluster 模式：

```bash
python redis_instance_test.py \
  --config redis-test.example.json \
  --profile cluster \
  --architecture cluster \
  --host 10.0.0.6
```

## 动态选择用例

查看全部 profile 和 suite：

```bash
python redis_instance_test.py --list-suites
```

只执行指定 suite：

```bash
python redis_instance_test.py \
  --config redis-test.example.json \
  --suites network,authentication,ping,string,ttl,replication
```

也可以在 JSON 中配置：

```json
{
  "execution": {
    "profile": "standard",
    "suites": ["network", "ping", "string", "ttl"],
    "cleanup": "always"
  }
}
```

只要 `execution.suites` 不是 `null`，它就会覆盖 profile 默认套件。

## 配置覆盖顺序

```text
命令行参数 > JSON 配置 > 程序默认值
```

常用动态参数：

```text
--host                 Redis 地址
--port                 Redis 端口
--profile              smoke/standard/performance/cluster
--suites               逗号分隔的 suite 名称
--architecture         standalone/master-slave/cluster
--username             Redis 6+ ACL 用户名
--password-env         密码环境变量名称
--no-auth              明确按免密实例测试
--cleanup              always/on-success/never
--requests             performance 请求数
--concurrency          performance 并发数
--namespace            测试 Key 前缀
--report               指定 JSON 报告路径
```

## 密码处理

默认 `authentication.mode` 为 `prompt`，密码不会显示，也不会写入配置和报告。

自动化环境可以使用环境变量：

```json
{
  "authentication": {
    "mode": "environment",
    "password_env": "REDIS_PASSWORD",
    "required": true
  }
}
```

在终端安全输入：

```bash
read -rsp "Redis password: " REDIS_PASSWORD
echo
export REDIS_PASSWORD
python redis_instance_test.py --config redis-test.example.json
unset REDIS_PASSWORD
```

不要把真实密码写进 JSON、脚本、Git 仓库或命令行参数。

## 清理策略

每次运行使用类似以下的唯一前缀：

```text
zhuque:redis-test:{20260817153000-a1b2c3d4}:*
```

所有 Cluster 测试 Key 使用相同 hash tag，避免跨槽事务问题。工具只扫描和删除本次运行的前缀，不执行 `FLUSHDB`、`FLUSHALL`、`SHUTDOWN` 或 `CONFIG SET`。

策略说明：

```text
always       无论测试成功或失败都清理，默认值
on-success   仅全部测试无失败时清理
never        保留测试数据，用于人工排查
```

## 结果与退出码

控制台结果示例：

```text
[PASS] network          1.20 ms  TCP reachable at 10.0.0.17:6379
[FAIL] authentication   0.83 ms  Unauthenticated PING succeeded while authentication is required
[PASS] ping             0.55 ms  PONG
[WARN] persistence      0.92 ms  aof_enabled=0, save=''; verify platform-level backup policy
[PASS] cleanup          1.40 ms  deleted=15, remaining=0
```

状态含义：

```text
PASS    断言通过
FAIL    功能或期望不满足，程序退出码为 1
WARN    测试完成，但存在需要确认的风险
SKIP    当前架构或配置不适用
```

报告默认写入 `reports/redis-test-时间.json`，不包含 Redis 密码。退出码：

```text
0    没有 FAIL
1    至少一个 FAIL
2    依赖或运行环境错误
```

## 新增用例

在 `RedisTestRunner` 中添加一个独立方法，例如：

```python
def test_bitmap(self) -> str:
    key = self.key("bitmap")
    assert self.client.setbit(key, 7, 1) == 0
    assert self.client.getbit(key, 7) == 1
    return "SETBIT/GETBIT passed"
```

然后将它注册到 `suites` 属性和需要的 `PROFILES` 中。用例必须：

- 只使用 `self.key()` 创建测试 Key；
- 有明确断言和可读结果；
- 不执行破坏性管理命令；
- 能被默认清理逻辑完整删除。

## 测试边界

本工具覆盖 Redis 数据面常规测试，不负责以下朱雀云控制面场景：

- 创建、销毁、扩缩容和副本变更；
- 主从强制切换和故障注入；
- 备份恢复和回收站；
- 控制台监控展示；
- 包年包月或按量计费账单。

这些场景应由云 API 或 Playwright 自动化单独覆盖。

## 开发验证

修改脚本后运行离线单元测试：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

离线测试使用模拟 Redis，不会访问或修改真实云实例。
