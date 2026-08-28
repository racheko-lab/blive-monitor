# 快手监控完整解决方案 — 自托管 Runner 安装指南

## 问题背景

快手自 2026-08-14 起对所有云数据中心 IP（GitHub Actions 的 Azure/macOS runner）实施硬封锁，
作品接口 `live_api/profile/public` 恒返回 `result=2`。代码已内置**中国 CDN 自动降级**
（通过 AliDNS DoH 解析中国 CDN 边缘 IP，写入 /etc/hosts），但 CDN 边缘对海外云 IP
只返回缓存的少量旧作品，且会被限流。

**完整解决方案**：在你的电脑（西安住宅 IP）上安装 GitHub Actions 自托管 runner。
住宅 IP 不被快手封锁，直连即可拿全量作品列表。一次安装，永久自动运行，无需配置代理或 IP。

---

## 安装步骤（Windows / macOS / Linux 通用）

### 第 1 步：打开 Runner 设置页

1. 浏览器打开仓库：https://github.com/racheko-lab/blive-monitor
2. 点击顶部菜单 **Settings**（设置）
3. 左侧栏找到 **Actions** → 点击展开
4. 点击 **Runners**
5. 点击绿色按钮 **New self-hosted runner**

### 第 2 步：选择操作系统

在页面上选择你电脑的系统：
- Windows 电脑 → 点 **Windows**
- Mac 电脑 → 点 **macOS**（注意选对芯片：Intel 选 x64，M1/M2/M3 选 ARM64）
- Linux 电脑 → 点 **Linux**（选 x64 或 ARM64）

### 第 3 步：按页面指令安装

页面会显示一段命令，**逐行复制执行**。以 macOS/Linux 为例：

```bash
# 1. 创建 runner 目录（随便放哪里，比如家目录）
mkdir actions-runner && cd actions-runner

# 2. 下载 runner（页面会给最新下载链接，直接复制）
curl -o actions-runner-osx-x64-2.XXX.tar.gz -L https://...

# 3. 解压
tar xzf ./actions-runner-*.tar.gz

# 4. 配置（页面会给带 token 的命令，直接复制）
./config.sh --url https://github.com/racheko-lab/blive-monitor --token XXXXXXXXXXXXX
```

配置过程中会问：
- `Enter the name of the runner group` → 直接回车（用默认）
- `Enter the name of your runner` → 直接回车（用电脑名）或输入 `xian-runner`
- `Enter any additional labels` → 直接回车
- `Enter name of work folder` → 直接回车（用默认 `_work`）

### 第 4 步：安装系统依赖（Linux 专用）

如果是 Linux（Ubuntu/Debian），还需要安装浏览器依赖：

```bash
sudo apt-get update
sudo apt-get install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2
```

Windows/macOS 通常不需要额外依赖。

### 第 5 步：启动 Runner

**临时启动**（测试用，关掉终端就停）：
```bash
./run.sh   # macOS/Linux
# 或 run.cmd  # Windows
```

**后台服务**（推荐，开机自启，关掉终端也不停）：

macOS/Linux：
```bash
sudo ./svc.sh install
sudo ./svc.sh start
```

Windows（以管理员身份运行 PowerShell）：
```powershell
./svc.ps1 install
./svc.ps1 start
```

### 第 6 步：切换 Workflow 到自托管 Runner

> ⚠️ **2026-08-28 起已改版**：不再使用 `RUNNER_LABEL` 仓库变量，改为直接改 workflow。原因见文末「常见问题」。

1. 先确认 runner **在线**（本机终端里能看到 `Listening for Jobs`）
2. 编辑 `.github/workflows/check.yml`，把 `check` 作业的 runner 改成自托管：

   ```yaml
   jobs:
     check:
       runs-on: self-hosted   # 原来是 macos-latest
   ```

3. 提交并推送到 master

完成！下一次定时检测（每 5 分钟）就会自动在你的电脑上运行，快手作品监控将从你的住宅 IP 直连，拿全量数据。

> ⚠️ **务必在 runner 在线时再切**。若电脑关机状态下切成自托管，`check` 作业会无限 Queued，
> `deploy`（`needs: check`）永不执行 → 版本角标不刷新、状态停更，且**失败完全静默**。

---

## 验证是否生效

1. 打开 https://github.com/racheko-lab/blive-monitor/actions
2. 等下一次 "直播状态检测" 运行（最多 5 分钟）
3. 点击该运行 → 看 **check** 任务
4. 如果日志里看到 `Runner: self-hosted` 且没有 "出口 IP 在封锁段" 警告，说明成功
5. 前端 https://racheko-lab.github.io/blive-monitor/ 右下角版本号更新后，
   "通知健康" 面板中快手账号的风控状态会消失

---

## 常见问题

**Q: 电脑关机了怎么办？**
A: 关机期间检测暂停，开机后 runner 自动重连，下一轮检测恢复。不会丢数据。

**Q: 可以随时切回 GitHub 托管 runner 吗？**
A: 可以。把 `.github/workflows/check.yml` 里 `jobs.check.runs-on` 改回 `macos-latest` 并提交即可
（会走中国 CDN 降级模式）。

**Q: 为什么不用 `RUNNER_LABEL` 仓库变量了？**
A: 2026-08-28 事故：变量被设为 `self-hosted`，而 runner 所在电脑离线 → `check` 作业无限 Queued →
`deploy`（`needs: check`）永不执行 → 前端版本号不刷新、`status.json` 停更超 24 小时，且无人收到告警。
根因是「默认可用、自托管需改代码」被反转成了「默认依赖一台可能关机的电脑」。改回静态写死后，
电脑关机只会让快手走降级模式，不会让整条监控停摆。

**Q: runner 安全吗？**
A: runner 只运行你自己仓库的 workflow，不会执行第三方代码。token 只用于注册，不存储密码。

**Q: Windows 上怎么安装？**
A: 在 Settings → Actions → Runners → New self-hosted runner 选 Windows，
   页面会给 PowerShell 命令，以管理员身份逐行执行即可。
