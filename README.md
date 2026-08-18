# Redis Instance Tester

这是一个面向云 Redis 实例的可配置 Python 测试工具。它通过 `redis-py` 执行常规数据面测试，并提供安全组 TCP 黑盒验证，支持命令行覆盖、JSON 配置、测试套件组合、JSON 报告和自动清理。

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

## 项目目录架构

```text
redis-instance-tester/
├── .github/
│   └── workflows/
│       └── tests.yml              # GitHub Actions：Python 3.8/3.12 自动验证
├── tests/
│   ├── test_runner.py             # 配置、套件、报告、信号及模拟 TCP Redis 测试
│   └── test_integration.py        # 可选的真实 Redis 数据面集成测试
├── redis_instance_test.py         # 主程序：参数、校验、客户端、测试套件、清理和报告
├── redis-test.example.json        # 完整配置示例，不保存真实密码
├── requirements.txt               # 运行依赖：redis-py
├── requirements-dev.txt           # 开发测试依赖：运行依赖 + fakeredis
├── .gitignore                     # 忽略虚拟环境、缓存和测试报告
├── README.md                      # 部署、配置、测试场景和维护说明
├── reports/                       # 运行时生成的 JSON 报告，默认不提交 Git
├── .venv/                         # 本地 Python 虚拟环境，运行时生成
└── __pycache__/                   # Python 字节码缓存，运行时生成
```

项目采用单文件主程序结构，目的是让测试工具可以直接上传到测试服务器运行，不要求把它
安装成 Python 包。核心职责如下：

- `redis_instance_test.py`：合并默认值、JSON 和命令行参数，执行严格配置校验，创建
  Redis 客户端，调度 `network`、`security_group`、数据结构、复制、健康及性能套件，
  最后清理测试 Key、关闭连接并生成报告。
- `redis-test.example.json`：保存可复用的非敏感默认参数。临时测试参数可通过 `--host`、
  `--profile`、`--set` 等命令行选项覆盖，因此无需每次修改并重新上传该文件。
- `tests/test_runner.py`：不连接真实云实例，使用 mock、fakeredis 和本地 TCP fake server
  验证配置、重试、功能断言、清理、报告、中断处理及安全组探测逻辑。
- `tests/test_integration.py`：只有设置 `REDIS_INTEGRATION_*` 环境变量时才连接真实 Redis；
  默认跳过，避免开发机或 CI 意外操作云实例。
- `.github/workflows/tests.yml`：每次 push 和 pull request 执行编译检查、普通单元测试和
  `python -O` 优化模式测试。
- `reports/`、`.venv/` 和 `__pycache__/`：均为运行时目录，不属于需要上传或提交的源码。

程序执行关系：

```text
命令行参数 / JSON 配置
          ↓
合并配置并校验类型、范围和安全限制
          ↓
选择 profile 或 suites
          ↓
执行 TCP/安全组探测及 Redis 数据面测试
          ↓
按策略清理测试 Key，并关闭客户端连接
          ↓
输出控制台结果、退出码和 reports/*.json
```

## 环境要求

- Python 3.8 或更高版本
- Git
- Redis 实例网络可达
- `redis-py 4.5 - 5.x`

先在服务器检查环境：

```bash
python3 --version
git --version
```

如果服务器没有 Python 3 和 Git，需要根据 Linux 发行版一次性安装。

Ubuntu / Debian：

```bash
apt-get update
apt-get install -y git python3 python3-venv python3-pip
```

Rocky Linux / AlmaLinux 9：

```bash
dnf install -y git python3 python3-pip
```

安装后再次执行 `python3 --version`，确认版本不低于 3.8。不要使用 CentOS 7 自带的
Python 2.7；较旧发行版如果软件源无法提供 Python 3.8+，应升级系统或使用经过维护的
Python 软件源。

系统 Python 和 Git 只需安装一次。项目首次下载后，在项目目录创建服务器自己的虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`.venv` 依赖服务器的操作系统和 Python 路径，不应上传到 GitHub，也不能把 Windows 创建的
`.venv` 复制到 Linux 使用。以后执行 `git pull` 更新项目时，服务器上的 `.venv` 会继续保留，
不需要重新安装系统 Python。

## 从公开 GitHub 仓库部署

公开仓库通过 HTTPS 下载不需要 GitHub 账号。服务器首次部署：

```bash
git --version
python3 --version

cd /opt
git clone --depth 1 \
  https://github.com/Tianwen2000/redis-instance-tester.git
cd redis-instance-tester

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

后续更新不需要重新上传目录：

```bash
cd /opt/redis-instance-tester
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`git pull --ff-only` 会在服务器源码存在未提交修改时停止，避免覆盖现场文件。因此建议通过
命令行动态覆盖测试参数，不直接编辑仓库内文件。正式回归测试可切换到已发布的固定 tag，
避免测试期间 `main` 分支变化：

```bash
git fetch --tags
git switch --detach v1.0.0
```

## 命令速查

进入项目并激活环境：

```bash
cd /opt/redis-instance-tester && source .venv/bin/activate
```

测试有密码的 Redis 主从实例基础功能：

```bash
python redis_instance_test.py --host 10.0.0.17 --port 6379 --profile smoke --architecture master-slave
```

测试 Redis 4.x 主从实例完整功能，预期 1 个副本：

```bash
python redis_instance_test.py --host 10.0.0.17 --port 6379 --profile standard --architecture master-slave --set expectations.version_prefix=4. --set expectations.replicas=1
```

测试 Redis 主从实例轻量性能：

```bash
python redis_instance_test.py --host 10.0.0.17 --port 6379 --profile performance --architecture master-slave --requests 5000 --concurrency 10 --set expectations.version_prefix=4. --set expectations.replicas=1
```

测试 Redis Cluster 实例：

```bash
python redis_instance_test.py --host 10.0.0.6 --port 6379 --profile cluster --architecture cluster
```

测试明确配置为免密的 Redis 实例：

```bash
python redis_instance_test.py --host 10.0.0.17 --port 6379 --profile smoke --architecture standalone --no-auth
```

测试关闭 Redis `6379` 安全组端口后是否已阻断：

```bash
python redis_instance_test.py --suites security_group --expect-blocked 10.0.0.17:6379 --set security_group.attempts=3 --set security_group.probe_timeout_seconds=3
```

测试关闭服务器 `22` 后 SSH 已阻断、但 Redis 仍能登录和读写（从另一台观察机执行）：

```bash
python redis_instance_test.py --host 10.0.0.17 --port 6379 --suites security_group,network,authentication,ping,string --expect-blocked 10.0.0.9:22 --expect-reachable 10.0.0.17:6379 --set security_group.attempts=3
```

有密码的命令会提示 `Redis password:` 并隐藏输入；`--no-auth` 只用于明确配置为免密的实例。

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

该性能结果用于实例间回归比较，不等同于专业容量评估。可以配置最低吞吐量、最大 p95
和最大 p99 作为 CI 门禁；未配置阈值时只记录指标。正式容量测试应使用专用压测机、
固定网络条件和独立测试方案。

```json
{
  "performance": {
    "min_throughput": 1000,
    "max_p95_ms": 10,
    "max_p99_ms": 25
  }
}
```

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
重复的 suite 会被拒绝，避免意外重复执行性能或写入测试。

## 安全组生效验证

`security_group` suite 从脚本所在机器发起 TCP 连接，检查配置的端点是否符合
`reachable`（应可达）或 `blocked`（应阻断）预期。它不修改云平台安全组，也不能仅凭
一次 TCP 失败区分安全组、网络 ACL、路由和服务未监听；安全组变更和回滚仍应在云平台侧完成。

配置形式：

```json
{
  "security_group": {
    "probe_timeout_seconds": 2,
    "attempts": 3,
    "interval_seconds": 0.25,
    "checks": [
      {
        "name": "redis-port-open",
        "host": "10.0.0.17",
        "port": 6379,
        "expected": "reachable"
      },
      {
        "name": "ssh-port-blocked",
        "host": "10.0.0.9",
        "port": 22,
        "expected": "blocked"
      }
    ]
  },
  "execution": {
    "suites": ["security_group"]
  }
}
```

也可以不改 JSON，直接在测试服务器上动态执行。关闭 Redis 端口后，只检查它是否被阻断：

```bash
python redis_instance_test.py \
  --suites security_group \
  --expect-blocked 10.0.0.17:6379 \
  --set security_group.attempts=3 \
  --set security_group.probe_timeout_seconds=3 \
  --report reports/redis-port-blocked.json
```

恢复 Redis 端口后，验证端口、认证和基础读写全部恢复：

```bash
read -rsp "Redis password: " REDIS_PASSWORD && echo
export REDIS_PASSWORD
python redis_instance_test.py \
  --host 10.0.0.17 \
  --port 6379 \
  --suites security_group,network,authentication,ping,string \
  --expect-reachable 10.0.0.17:6379 \
  --password-env REDIS_PASSWORD \
  --report reports/redis-port-restored.json
unset REDIS_PASSWORD
```

关闭测试服务器的 `22` 端口时，应得到两项独立结论：从另一台观察机新建到测试服务器
`22` 的连接失败；测试服务器到 Redis `6379` 的连接和 Redis 读写仍成功。不要在测试服务器
本机探测自己的入方向 `22` 规则，这不能代表外部流量。已有 SSH 会话也可能因连接跟踪继续存活，
因此不能作为“22 仍开放”的依据。

执行关闭 `22` 的测试前，必须准备云控制台/VNC/堡垒机等独立管理通道和自动回滚规则，
避免把唯一管理入口永久关闭。

## 配置覆盖顺序

```text
专用命令行参数（如 --host） > --set > JSON 配置 > 程序默认值
```

配置文件只接受程序声明的 section 和 option。未知字段、错误类型以及不安全的
namespace 会在连接 Redis 前被拒绝，不会带着不确定配置开始测试。

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
--set                  覆盖任意已声明配置，可重复使用
--expect-reachable     声明预期可达的 HOST:PORT，可重复使用
--expect-blocked       声明预期阻断的 HOST:PORT，可重复使用
```

`--set` 使用 `section.option=value`。数字、`true`、`false`、`null`、数组和对象按 JSON
类型解析，其他内容按字符串处理。示例：

```bash
read -rsp "Redis password: " REDIS_PASSWORD && echo
export REDIS_PASSWORD
python redis_instance_test.py \
  --host 10.0.0.17 \
  --port 6379 \
  --profile standard \
  --architecture master-slave \
  --password-env REDIS_PASSWORD \
  --set expectations.version_prefix=4. \
  --set expectations.replicas=1 \
  --set expectations.max_replica_lag_seconds=3 \
  --set atomicity.requests=2000 \
  --set atomicity.concurrency=20 \
  --set health.max_memory_ratio=0.85 \
  --report reports/standard-dynamic.json
unset REDIS_PASSWORD
```

数组或对象应使用单引号保护，例如
`--set 'execution.suites=["network","ping","string"]'`。未知配置项和错误类型仍会在连接前被拒绝。
Redis 密码不能通过 `--set` 设置，也不应出现在命令行历史中。

为避免错误参数对测试机或 Redis 实例造成过大压力，程序执行以下硬限制：

```text
execution.ttl_seconds       1 - 60
atomicity.requests          1 - 100000
atomicity.concurrency       1 - 128
atomicity.max_duration_seconds  1 - 600 秒
performance.requests        1 - 100000
performance.concurrency     1 - 128
performance.value_size      1 - 1048576 bytes
performance.keyspace        1 - 100000
performance.max_duration_seconds 1 - 3600 秒
connection.retry_attempts   1 - 5
security_group.attempts     1 - 5
Redis socket timeout        大于 0 且不超过 60 秒
```

`execution.namespace` 长度必须为 1-128，只能包含字母、数字、点、下划线、冒号和连字符。
该限制同时保护 Cluster hash tag 和清理时的 SCAN 匹配范围。Cluster 只支持 `db=0`。

健康检查阈值位于 `health` section。内存比例、碎片率和阻塞客户端超过阈值时产生
WARN。`rejected_connections` 是 Redis 启动以来的历史计数，默认只记录；只有设置
`warn_on_historical_rejections: true` 时才会据此产生 WARN。

## TLS 与连接重试

TCP 连通性和认证后的 PING 默认最多尝试 3 次，并采用指数退避。可以通过
`connection.retry_attempts` 和 `connection.retry_backoff_seconds` 调整。

TLS 实例可在配置中启用证书校验：

```json
{
  "connection": {
    "ssl": true,
    "ssl_cert_reqs": "required",
    "ssl_ca_certs": "/etc/ssl/certs/redis-ca.pem",
    "ssl_certfile": null,
    "ssl_keyfile": null,
    "ssl_check_hostname": true,
    "client_name": "redis-instance-tester"
  }
}
```

双向 TLS 必须同时配置 `ssl_certfile` 和 `ssl_keyfile`。只有明确的隔离测试环境才应使用
`ssl_cert_reqs: "none"`，此时必须同时关闭 hostname 校验。

## 密码处理

默认 `authentication.mode` 为 `prompt`，密码不会显示，也不会写入配置和报告。
报告配置使用允许字段清单，配置中意外出现的 `password`、`secret` 等未知字段会在运行前被拒绝。

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

使用 `always` 时，用户按 `Ctrl+C` 或进程收到 `SIGTERM` 后仍会尝试清理并生成报告。
Redis 客户端连接池会在清理完成后显式关闭。

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

报告默认写入 `reports/redis-test-时间-随机ID.json`，采用临时文件原子写入，不包含 Redis 密码。
报告额外记录 schema 版本、Python/redis-py 运行环境、`run_id`、实际选择的 suites、
总耗时、退出码和是否被中断。退出码：

```text
0    没有 FAIL
1    至少一个 FAIL
2    依赖或运行环境错误
130  用户通过 Ctrl+C 中断测试
143  进程收到 SIGTERM 后终止
```

## 新增用例

在 `RedisTestRunner` 中添加一个独立方法，例如：

```python
def test_bitmap(self) -> str:
    key = self.key("bitmap")
    require(self.client.setbit(key, 7, 1) == 0, "SETBIT returned an unexpected value")
    require(self.client.getbit(key, 7) == 1, "GETBIT did not return the stored bit")
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
- 包年包月或按量计费账单；
- 安全组规则的创建、修改和自动回滚（本工具只验证生效后的 TCP 行为）。

这些场景应由云 API 或 Playwright 自动化单独覆盖。

## 开发验证

修改脚本后运行离线单元测试：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -O -m unittest discover -s tests -v
```

离线测试使用模拟 Redis，不会访问或修改真实云实例。
第二条测试命令用于确认 Python 优化模式不会跳过功能断言。

`tests/test_integration.py` 默认跳过。设置目标环境变量后可以执行真实数据面集成测试：

```bash
export REDIS_INTEGRATION_HOST=10.0.0.17
export REDIS_INTEGRATION_PORT=6379
export REDIS_INTEGRATION_ARCHITECTURE=master-slave
export REDIS_INTEGRATION_PASSWORD='从安全输入或密钥系统获得'
python -m unittest tests.test_integration -v
unset REDIS_INTEGRATION_PASSWORD
```

可选变量包括 `REDIS_INTEGRATION_USERNAME`、`REDIS_INTEGRATION_SSL`、
`REDIS_INTEGRATION_SSL_CA_CERTS`、`REDIS_INTEGRATION_VERSION_PREFIX`、
`REDIS_INTEGRATION_REPLICAS` 和 `REDIS_INTEGRATION_PERFORMANCE`。真实集成测试同样使用唯一
Key 前缀并在 `tearDown` 中清理。
