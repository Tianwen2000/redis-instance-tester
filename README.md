# Redis Instance Tester

这是一个面向云 Redis 实例的可配置 Python 测试工具。它通过 `redis-py` 执行常规数据面测试，并提供安全组 TCP 黑盒验证，支持命令行覆盖、JSON 配置、测试套件组合、JSON 报告和自动清理。

> 注意：下面示例中的路径、命令参数、Redis 实例名、VPC 地址、子网地址及端口等，均需根据你的实际环境替换。

> 多实例对比测试：如果要分别测试主从标准架构、Cluster/集群架构或单机架构，以及不同内存、
> 分片数和副本数规格的 Redis 实例，请先在云服务控制台分别创建对应实例，再从同一台 Linux
> 测试服务器逐个运行本脚本。每次只需把命令中的实例内网（子网）IP、端口、架构、版本和副本
> 期望等参数改为当前实例的实际值。内存、分片数和副本数属于创建实例时选择的云平台规格，
> 本脚本负责测试和比较这些实例的数据面表现，不负责创建实例或修改实例规格。

当前示例配置对应：

```text
实例：按量计费实例
实例 ID：crs-8hz033uk
地址：10.0.1.12:6379
架构：Redis 5.2.x master-slave
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
├── .gitignore                     # 忽略缓存、报告和本地敏感文件
├── README.md                      # 部署、配置、测试场景和维护说明
├── reports/                       # 运行时生成的 JSON 报告，默认不提交 Git
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
- `reports/` 和 `__pycache__/`：均为运行时目录，不属于需要上传或提交的源码。

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
apt-get install -y git python3 python3-pip
```

Rocky Linux / AlmaLinux 9：

```bash
dnf install -y git python3 python3-pip
```

安装后再次执行 `python3 --version`，确认版本不低于 3.8。不要使用 CentOS 7 自带的
Python 2.7；较旧发行版如果软件源无法提供 Python 3.8+，应升级系统或使用经过维护的
Python 软件源。

系统 Python 和 Git 只需在服务器首次部署时安装。本项目在专用测试服务器上直接使用系统
`python3`，不创建 Python 虚拟环境。项目依赖必须在 `git clone` 并进入仓库目录后安装，
不要在没有项目目录时执行 `-r requirements.txt`。

以后执行 `git pull` 更新项目时，不需要重新安装系统 Python；只有 `requirements.txt`
发生变化时才需要重新执行依赖安装命令。

## 从公开 GitHub 仓库部署

公开仓库通过 HTTPS 下载不需要 GitHub 账号。服务器首次部署：

```bash
git --version
python3 --version

cd /opt
git clone --depth 1 \
  https://github.com/Tianwen2000/redis-instance-tester.git
cd redis-instance-tester

python3 -m pip install --user \
  --index-url https://pypi.org/simple \
  -r requirements.txt
python3 redis_instance_test.py --list-suites
```

部分云服务器会把 pip 默认配置为内部镜像。如果上面的命令提示
`No matching distribution found for redis`，说明当前镜像没有该包，可以改用国内镜像：

```bash
python3 -m pip install --user \
  --index-url https://mirrors.aliyun.com/pypi/simple/ \
  -r requirements.txt
```

安装完成后验证 `redis-py`：

```bash
python3 -c "import redis; print(redis.__version__)"
```

如果系统 pip 报 `externally-managed-environment`，且该服务器确实按约定直接使用系统
Python，可在确认服务器为专用测试机后追加 `--break-system-packages`：

```bash
python3 -m pip install --user --break-system-packages \
  --index-url https://pypi.org/simple \
  -r requirements.txt
```

如果服务器完全不能访问公网 PyPI，可在其他联网机器、且位于项目目录时下载 wheel，上传
`wheels/` 到服务器项目目录后执行：

```bash
python3 -m pip download --only-binary=:all: \
  -r requirements.txt -d wheels
python3 -m pip install --user --no-index \
  --find-links ./wheels -r requirements.txt
```

以后执行 `git pull` 更新项目时，不需要重新安装系统 Python；只有 `requirements.txt`
发生变化时才需要重新执行上述依赖安装命令。

后续更新不需要重新上传目录：

```bash
cd /opt/redis-instance-tester
git pull --ff-only
```

如果更新内容包含 `requirements.txt` 变更，再执行：

```bash
python3 -m pip install --user \
  --index-url https://pypi.org/simple \
  -r requirements.txt
```

`git pull --ff-only` 会在服务器源码存在未提交修改时停止，避免覆盖现场文件。因此建议通过
命令行动态覆盖测试参数，不直接编辑仓库内文件。

## 命令速查

进入项目后，最常用的全量功能测试命令如下：

```bash
cd /opt/redis-instance-tester && python3 redis_instance_test.py --host 10.0.1.12 --port 6379 --profile standard --architecture master-slave --set expectations.version_prefix=5. --set expectations.replicas=1
```

清理项目生成的报告和 Python 缓存（不会删除源码、配置或 Redis 数据）：

```bash
cd /opt/redis-instance-tester && mkdir -p reports && find reports -maxdepth 1 -type f -name '*.json' -delete && find . -type d -name '__pycache__' -prune -exec rm -rf {} + && find . -type f -name '*.pyc' -delete
```

参数说明：

- `--host`、`--port`：替换为 Redis 实例地址和端口。
- `--profile standard`：全量功能测试；快速检查改为 `smoke`，性能测试改为 `performance`。
- `standard` 只覆盖 Redis 数据面功能，不自动执行安全组端口测试；安全组请按下面的专项流程单独执行。
- `--architecture master-slave`：主从标准架构。Cluster/集群架构要把这两个参数改为
  `--profile cluster --architecture cluster`，并删除 `--set expectations.replicas=1`；单机只需
  改为 `--architecture standalone`，同样删除副本参数。
- `--set expectations.version_prefix=5.`：要求版本以 `5.` 开头；不校验版本可设为 `null`。
- `--set expectations.replicas=1`：主从实际至少要有 1 个在线副本，命令中的副本数不能超过实际副本数；实际 1 个写 2 会 `FAIL`。
- 密码实例：保留命令不变，运行时输入 `Redis password:`；自动化时加
  `--password-env REDIS_PASSWORD`。免密实例才加 `--no-auth`。
- 上面这条主命令默认按“可能需要认证”的实例运行；README 后面的安全组专项命令已经按免密
  实例写好 `--no-auth`。
- `--report reports/xxx.json`：可选，指定报告文件路径。

主命令没有用到的常用参数，只需按需追加：

```text
--username USER                  Redis 6+ ACL 用户名
--password-env REDIS_PASSWORD    从环境变量读取密码
--no-auth                        明确按免密实例测试
--suites network,ping,string      只执行指定 suite，覆盖 profile
--cleanup on-success             清理策略：always/on-success/never
--requests 5000 --concurrency 10 performance 请求数和并发数
--namespace zhuque:redis-test     自定义测试 Key 前缀
--report reports/result.json      指定 JSON 报告路径
--config redis-test.example.json   从 JSON 配置文件读取默认值
--set section.option=value         临时覆盖配置，可重复使用
--expect-reachable HOST:PORT       安全组检查该端点应可达
--expect-blocked HOST:PORT         安全组检查该端点应阻断
```

`standard` 是受控的全量功能回归，不包含长时间压力或容量测试；需要性能指标时将
`--profile standard` 改为 `--profile performance`，并按需增加 `--requests`、`--concurrency`。

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

Cluster 不需要另写一套命令：直接使用上面的全量测试命令，把
`--profile standard --architecture master-slave` 替换为
`--profile cluster --architecture cluster`，并删除副本参数。Cluster 要求 `connection.db=0`，
`expectations.replicas` 只适用于主从复制检查。

## 动态选择用例

查看全部 profile 和 suite：

```bash
python3 redis_instance_test.py --list-suites
```

只执行指定 suite：

```bash
python3 redis_instance_test.py \
  --config redis-test.example.json \
  --no-auth \
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

`security_group` suite 只发起 TCP 连接，不登录 Redis，也不修改云平台安全组。你需要先在
云服务控制台关闭或恢复规则，再从观察机运行命令验证结果。

只执行 `security_group` 时不会产生 Redis 测试 Key，也不会执行认证、PING 或读写操作；报告中
出现 `cleanup: No authenticated Redis commands were executed` 属于正常结果。

先区分两个角色：

- **观察机**：运行 `python3 redis_instance_test.py` 的服务器，例如 `VM-1-13-ubuntu`。
- **目标端点**：被探测的 `HOST:PORT`。Redis 端点是 `10.0.1.12:6379`；Linux 服务器
  的 SSH 端点通常是 `<linux-server-ip>:22`。

命令中的 `--host`、`--port` 是默认目标地址，`--expect-blocked` 或 `--expect-reachable`
是本次安全组检查的实际目标。安全组专项命令也建议始终写出 `--host` 和 `--port`，避免报告
显示默认的 `127.0.0.1:6379`。`--no-auth` 对只执行 `security_group` 没有实际作用，但示例
统一保留，表示按免密环境执行。

记住这两个测试流程：

```text
测试 Redis：控制台关闭 6379 -> 观察机执行 blocked 命令 -> 恢复 6379 -> 执行 restored 命令
测试 SSH：  控制台关闭 Linux 服务器 22 -> 外部观察机执行 blocked 命令 -> 恢复 22 -> 执行 restored 命令
```

截图中使用的 `10.0.1.12:6379` 命令属于第一种流程，不能用来判断 SSH `22` 是否关闭。

### 场景一：关闭和恢复 Redis 6379

以下命令都在观察机上执行，控制台操作的是 Redis 实例的 TCP `6379` 入方向规则。

1. 关闭规则前，先确认端口可达：

```bash
python3 redis_instance_test.py \
  --host 10.0.1.12 \
  --port 6379 \
  --no-auth \
  --suites security_group \
  --expect-reachable 10.0.1.12:6379 \
  --set security_group.attempts=3 \
  --set security_group.probe_timeout_seconds=3 \
  --report reports/redis-port-before.json
```

2. 在云控制台关闭允许观察机访问 Redis 的 TCP `6379` 规则，等待规则生效。

3. 回到观察机，检查端口是否被阻断：

```bash
python3 redis_instance_test.py \
  --host 10.0.1.12 \
  --port 6379 \
  --no-auth \
  --suites security_group \
  --expect-blocked 10.0.1.12:6379 \
  --set security_group.attempts=3 \
  --set security_group.probe_timeout_seconds=3 \
  --report reports/redis-port-blocked.json
```

预期是 `expected=blocked observed=blocked` 和 `[PASS] security_group`。如果是
`expected=blocked observed=reachable`，说明从这台观察机访问时端口仍然放行，安全组测试不通过。

4. 在云控制台恢复 TCP `6379` 规则，等待规则生效。

5. 在同一台观察机验证端口和 Redis 基础读写：

```bash
python3 redis_instance_test.py \
  --host 10.0.1.12 \
  --port 6379 \
  --no-auth \
  --suites security_group,network,authentication,ping,string \
  --expect-reachable 10.0.1.12:6379 \
  --report reports/redis-port-restored.json
```

预期为安全组、网络、PING 和 String 测试通过；`authentication` 在免密模式下显示 `SKIP`。

`--set security_group.attempts=3` 表示连接失败时最多探测 3 次。连接第一次成功时会显示
`attempts=1`，这是正常的，不表示参数没有生效。

### 场景二：关闭和恢复 Linux 服务器 SSH 22

这个场景测试的是 Linux 服务器的入方向 TCP `22`，不是 Redis 的 `10.0.1.12:6379`。
如果 `10.0.1.12` 是托管 Redis 地址，不能把它的 `22` 当作 SSH 端口测试。

被测试的 Linux 服务器必须使用另一台观察机发起新连接。不要在被测试服务器本机探测自己的
入方向 `22`，也不要把已有 SSH 会话当作端口仍开放的依据。

关闭规则前，在外部观察机确认 `22` 可达：

```bash
python3 redis_instance_test.py \
  --host <linux-server-ip> \
  --port 22 \
  --no-auth \
  --suites security_group \
  --expect-reachable <linux-server-ip>:22 \
  --report reports/ssh-port-before.json
```

然后在云控制台关闭该 Linux 服务器的 TCP `22` 入方向规则，再从外部观察机执行：

```bash
python3 redis_instance_test.py \
  --host <linux-server-ip> \
  --port 22 \
  --no-auth \
  --suites security_group \
  --expect-blocked <linux-server-ip>:22 \
  --report reports/ssh-port-blocked.json
```

恢复 TCP `22` 规则后，再从外部观察机执行：

```bash
python3 redis_instance_test.py \
  --host <linux-server-ip> \
  --port 22 \
  --no-auth \
  --suites security_group \
  --expect-reachable <linux-server-ip>:22 \
  --report reports/ssh-port-restored.json
```

关闭 `22` 前必须准备云控制台、VNC 或堡垒机等备用管理通道和自动回滚方案，避免关闭唯一
登录入口。安全组测试只能说明 TCP 当前可达或阻断，不能仅凭一次 TCP 失败区分安全组、网络
ACL、路由、防火墙或服务未监听；规则变更和原因定位仍需结合云平台配置及服务器状态。

如果要测试的是当前观察机自己的 `22` 端口，当前观察机不能同时作为观察机，必须再准备第三台
机器从外部发起新连接。关闭 `22` 后，仍可从独立机器测试 Redis `6379` 是否可达；这与 SSH
端口测试是两个独立结论。

配置形式（仅示例 Redis `6379`，SSH `22` 请按上面的独立场景执行）：

```json
{
  "security_group": {
    "probe_timeout_seconds": 2,
    "attempts": 3,
    "interval_seconds": 0.25,
    "checks": [
      {
        "name": "redis-port",
        "host": "10.0.1.12",
        "port": 6379,
        "expected": "reachable"
      }
    ]
  },
  "execution": {
    "suites": ["security_group"]
  }
}
```

JSON 中 `expected` 表示当前这次运行的预期状态：关闭规则前或恢复后写 `reachable`，关闭规则
后改为 `blocked`。不想编辑 JSON 时，直接使用上面的 `--expect-reachable` 或 `--expect-blocked`
命令行参数即可。

## 配置覆盖顺序

```text
专用命令行参数（如 --host） > --set > JSON 配置 > 程序默认值
```

配置文件只接受程序声明的 section 和 option。未知字段、错误类型以及不安全的
namespace 会在连接 Redis 前被拒绝，不会带着不确定配置开始测试。

`--set` 使用 `section.option=value`。数字、`true`、`false`、`null`、数组和对象按 JSON
类型解析，其他内容按字符串处理。需要临时修改阈值、请求数或报告路径时，直接把对应的
`--set`、`--report` 参数追加到“命令速查”中的主命令即可。

数组或对象应使用单引号保护，例如
`--set 'execution.suites=["network","ping","string"]'`。未知配置项和错误类型仍会在连接前被拒绝。
Redis 密码不能通过 `--set` 设置，也不应出现在命令行历史中。

关键参数按以下规则判定：主从标准架构实际至少要有 1 个在线副本，命令中的
`expectations.replicas` 不能超过实际副本数（实际 1、配置 2 会 FAIL；实际 2、配置 1 会 PASS）；
`version_prefix` 是版本前缀；`max_replica_lag_seconds` 是最大允许延迟；
`min_throughput` 是最低吞吐量，`max_p95_ms`/`max_p99_ms` 是最大延迟；`requests` 是总请求数，
`concurrency` 是并发数；安全组 `attempts` 是探测重试次数，不是端口数量。健康阈值超出时默认
产生 WARN，不直接判定 FAIL。

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

README 中的专项测试命令按免密实例编写，使用配置文件时同样追加 `--no-auth`：

```bash
python3 redis_instance_test.py --config redis-test.example.json --no-auth
```

有密码实例应删除 `--no-auth`，交互运行时由程序隐藏输入密码；自动化环境使用上面的
`REDIS_PASSWORD` 环境变量配置。不要把真实密码写进 JSON、脚本、Git 仓库或命令行参数。

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
[PASS] network          1.20 ms  TCP reachable at 10.0.1.12:6379
[SKIP] authentication   0.00 ms  Authentication is not required by configuration
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
python3 -m pip install --user \
  --index-url https://pypi.org/simple \
  -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 -O -m unittest discover -s tests -v
```

离线测试使用模拟 Redis，不会访问或修改真实云实例。
第二条测试命令用于确认 Python 优化模式不会跳过功能断言。

`tests/test_integration.py` 默认跳过。设置目标环境变量后可以执行真实数据面集成测试：

```bash
export REDIS_INTEGRATION_HOST=10.0.1.12
export REDIS_INTEGRATION_PORT=6379
export REDIS_INTEGRATION_ARCHITECTURE=master-slave
unset REDIS_INTEGRATION_PASSWORD
python3 -m unittest tests.test_integration -v
```

可选变量包括 `REDIS_INTEGRATION_USERNAME`、`REDIS_INTEGRATION_SSL`、
`REDIS_INTEGRATION_SSL_CA_CERTS`、`REDIS_INTEGRATION_VERSION_PREFIX`、
`REDIS_INTEGRATION_REPLICAS` 和 `REDIS_INTEGRATION_PERFORMANCE`。真实集成测试同样使用唯一
Key 前缀并在 `tearDown` 中清理。免密实例不要设置 `REDIS_INTEGRATION_PASSWORD`；有密码
实例再通过安全方式设置该变量。
