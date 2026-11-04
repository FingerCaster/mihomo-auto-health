# Mihomo Auto Health

**适用于 Clash Party 和 FlClash 的本地节点健康检测、自动隔离与恢复工具。**

持续超时或失败的节点会暂时从 `url-test` 自动选择组排除，恢复后自动加回。后台读取 Mihomo 健康检查记录，并定期复测已隔离节点，减少自动组反复选中不可用节点的情况。

本项目是独立安装的第三方辅助脚本，需要配合代理客户端使用，不是客户端内置插件。Clash Party 和 FlClash 共用健康判定算法，分别使用各自的安装器与配置适配。

## 功能

- **自动隔离**：根据连续失败次数和近期失败次数判定节点状态，保留已有的节点筛选条件。
- **自动恢复**：隔离后继续复测，连续成功达到阈值后重新加入自动组。
- **保留原始节点**：隔离规则只作用于 `url-test` 自动组，不从订阅节点列表或手动组中删除节点。
- **保留 FlClash 图钉首选**：重载配置后恢复首选，首选节点被隔离时保留选择记录，恢复回组后再设回。
- **本地运行**：配置、健康历史和日志保存在本机，无需 Sub-Store 或共享服务器。

## 支持范围

| 客户端 | 平台 | 运行依赖 | 安装入口 |
| --- | --- | --- | --- |
| Clash Party | Windows 10 / 11 | Python 3（含 `pythonw.exe`）、Node.js | `install_windows.ps1` · [详细说明](WINDOWS.md) |
| Clash Party | macOS | Python 3、Node.js | `install_macos.sh` |
| FlClash | Windows 10 / 11 | Python 3（含 `pythonw.exe`）、PyYAML 6.x | `install_flclash_windows.ps1` · [详细说明](FLCLASH.md) |

FlClash 适配按 **v0.9.2** 的配置和数据库结构实现，运行时不需要 Node.js。其他版本请先运行预检查；暂不提供 FlClash 的 Android、macOS 或 Linux 安装器。

两个客户端都要求配置中包含内联 `proxies` 和至少一个 `url-test` 自动组。仅使用 `proxy-providers` 的配置暂不支持；FlClash 的自定义配置类型也暂不支持。

客户端官方项目与下载：

- Clash Party：[项目主页](https://github.com/mihomo-party-org/clash-party) · [发行版本](https://github.com/mihomo-party-org/clash-party/releases) · [使用文档](https://clashparty.org/)
- FlClash：[项目主页](https://github.com/chen08209/FlClash) · [发行版本](https://github.com/chen08209/FlClash/releases)

## 获取项目

下载本仓库 ZIP 并解压，或使用 Git：

```sh
git clone https://github.com/FingerCaster/mihomo-auto-health.git
cd mihomo-auto-health
```

以下命令均在项目目录中执行。先在对应客户端中导入订阅或本地配置，选中并应用一次，再按客户端选择安装入口。

## 安装

### FlClash · Windows

安装依赖并运行只读预检查：

```powershell
py -3 -m pip install -r .\requirements-flclash.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1
```

预检查通过后，从系统托盘**完全退出 FlClash**，再安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1 -Install
```

安装器会备份相关文件和数据库，为当前订阅绑定健康脚本，启用带密钥的本机控制接口，并注册每分钟运行的后台任务。已有覆写脚本会先执行，再应用健康规则；安装失败时尝试回滚。

安装后重新启动 FlClash，应用当前订阅，等待约 60–90 秒查看状态。自定义数据目录可通过 `-DataDir` 指定，Python 路径可通过 `-PythonPath` 指定。完整参数和卸载步骤见 [FlClash 使用说明](FLCLASH.md)。

### Clash Party · Windows

准备 Python 3（含 `pythonw.exe`）和 Node.js，先运行只读预检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1
```

预检查通过后，从系统托盘**完全退出 Clash Party**，再安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -Install
```

安装后启动 Clash Party，等待约一分钟查看状态。便携版或自定义路径可传 `-AppDir`、`-DataDir`、`-PythonPath` 和 `-NodePath`，详见 [Clash Party Windows 使用说明](WINDOWS.md)。

### Clash Party · macOS

准备 Python 3 和 Node.js，完全退出 Clash Party 后执行：

```sh
./install_macos.sh
```

默认应用路径为 `/Applications/Clash Party.app`，默认数据目录为 `~/Library/Application Support/mihomo-party`。非标准目录可显式指定：

```sh
./install_macos.sh \
  --resources-dir "/Applications/Clash Party.app/Contents/Resources" \
  --data-dir "$HOME/Library/Application Support/mihomo-party"
```

安装完成后启动 Clash Party，后台默认每分钟检查一次。

## 检测与首选节点行为

### 默认健康规则

| 项目 | 默认行为 |
| --- | --- |
| 探测地址 | `https://www.gstatic.com/generate_204`，要求返回 HTTP 204 |
| 探测超时 | 3 秒 |
| 自动组健康检查间隔 | 60 秒 |
| 连续失败隔离 | 连续失败 3 次 |
| 近期失败隔离 | 最近最多 10 个样本中，至少有 8 个有效样本且失败至少 3 次 |
| 隔离后复测 | 约每 2–3 分钟 |
| 恢复条件 | 连续复测成功 3 次 |
| 自动组无候选节点 | 使用 `REJECT` |

探测成功只说明指定探测地址可用，不代表所有网站都能访问。切换到其他订阅时，后台检测暂停；切回安装时绑定的订阅后继续运行。

### FlClash 图钉首选

图钉表示节点可用时优先使用。脚本重载配置时，会恢复手动组的当前选择，以及 `url-test` / `fallback` 组的首选：

- **首选节点仍在组内**：恢复首选设置，避免因健康规则重载而丢失图钉。
- **首选节点被隔离**：暂时使用其他可用节点，保留用户在 FlClash 中保存的选择，节点恢复回组后再设回首选。
- **用户更换或取消首选**：以新的选择为准，不把自动组临时选出的节点误设为首选。

无需重载配置的检测周期，也会尝试恢复旧版脚本丢失的首选。自动隔离只修改 `url-test` 组；`fallback` 组仅参与首选恢复。

## 查看状态与调整参数

程序及其 `policy.json`、`state.json`、`status.json` 和 `agent.log` 保存在对应客户端的数据目录内：

| 客户端 | 数据目录内的子目录 | Windows 默认数据目录 |
| --- | --- | --- |
| Clash Party | `adaptive-auto-selection` | `%APPDATA%\mihomo-party` |
| FlClash | `adaptive-auto-selection-flclash` | `%APPDATA%\com.follow\clash` |

Windows 下先按客户端选择路径：

```powershell
# FlClash
$health = Join-Path $env:APPDATA 'com.follow\clash\adaptive-auto-selection-flclash'

# Clash Party 则使用下面这一行
# $health = Join-Path $env:APPDATA 'mihomo-party\adaptive-auto-selection'

Get-Content (Join-Path $health 'status.json') -Encoding UTF8
Get-Content (Join-Path $health 'agent.log') -Encoding UTF8 -Tail 30
```

便携版请改为安装时使用的数据目录。macOS 的 Clash Party 可运行：

```sh
cat "$HOME/Library/Application Support/mihomo-party/adaptive-auto-selection/status.json"
tail -n 30 "$HOME/Library/Application Support/mihomo-party/adaptive-auto-selection/agent.log"
```

正常运行时，`updated_at` 随检测周期更新，`rules_applied: true` 表示本轮已应用规则，`quarantined_count` 是当前隔离节点数，为 0 也可能正常。详细状态和错误含义见对应客户端的使用说明。

编辑对应目录的 `policy.json` 可调整超时、失败判定和恢复阈值，下一轮检测生效。不要修改 `profile_id`，也不要直接复制其他电脑的状态文件。

## 更新、暂停与卸载

获取项目更新后，完全退出对应客户端，再重新执行其安装命令。同一订阅的重新安装会保留健康策略和历史。**只执行 `git pull` 不会更新已经复制到客户端数据目录的后台程序。** 更新时请保留原安装使用的数据目录和路径参数。

- **FlClash · Windows**：暂停、恢复任务及 `-Uninstall` 卸载命令见 [FlClash 使用说明](FLCLASH.md#暂停恢复卸载)。
- **Clash Party · Windows**：暂停、恢复和删除任务见 [Windows 使用说明](WINDOWS.md#暂停与卸载)。
- **Clash Party · macOS**：执行 `./uninstall_macos.sh` 删除后台任务，然后在当前订阅的覆写设置中解除绑定“自动选择健康隔离与恢复”，重新加载订阅。

卸载后台任务后，需要按对应说明解除覆写或重新应用配置，才能移除已经生效的隔离规则。

## 验证范围

- 自动化测试覆盖共享健康算法、配置内容保留、安装与回滚、隔离与恢复，以及 FlClash 首选保留和取消。
- 已使用 FlClash v0.9.2 官方 Windows 发行包所带内核验证实际的隔离、恢复和首选恢复行为。
- 已在本机 Windows FlClash 中验证后台任务、配置重载及图钉首选保留。
- 完整 GUI 操作、自动订阅更新和 TUN 切换尚未全部完成端到端验证；这些结果不代表所有客户端版本都已实测。

开发测试：

```sh
python3 -m pip install -r requirements-flclash.txt
python3 -m unittest discover -s . -p 'test_*.py' -v
```

Windows 可将 `python3` 替换为 `py -3`。Clash Party 的 YAML/JS 测试需要 Node.js 及可用的 YAML 库，FlClash 脚本组合测试也使用 Node.js。

可选的真实 FlClash 内核集成测试：

```powershell
$env:FLCLASH_TEST_CORE = 'C:\Path\To\FlClashCore.exe'
py -3 -m unittest test_flclash_core -v
```

该测试使用临时配置和本机随机端口，不修改实际订阅、系统代理或 TUN。

## 数据与隐私

状态、日志和安装备份保存在本机。备份和安装记录可能包含订阅凭据、节点信息或控制接口密钥，请勿上传、分享或提交。本仓库不包含用户的订阅配置、节点列表和检测状态。

## 许可证

本项目使用 GNU Affero 通用公共许可证第 3 版，见 [LICENSE](LICENSE)。
