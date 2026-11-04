# FlClash 自动健康隔离（Windows）

这是 Mihomo Auto Health 的独立 FlClash 适配，与 Clash Party 版本共用失败判定、隔离和恢复算法。不要使用 `install_windows.ps1` 安装到 FlClash，它属于 Clash Party。

## 支持范围

- Windows 10/11、标准 Python 3（建议 3.13，需包含 `pythonw.exe`）、PyYAML 6.x；运行时不需要 Node.js。
- 按 FlClash **v0.9.2** 的 `shared_preferences.json`、SQLite 订阅/脚本表和 `scripts/<id>.js` 布局实现。其他版本先运行预检查；不兼容的数据结构会拒绝安装，不自动迁移数据库。
- 支持导入的订阅和本地文件配置；配置中须有内联 `proxies` 和至少一个 `url-test` 组。
- 暂不支持仅有 `proxy-providers` 的配置、FlClash 自定义配置类型、Android、macOS/Linux 安装。
- 已通过自动化测试和 v0.9.2 官方 Windows 发行包所带 `FlClashCore.exe` 的真实隔离/恢复测试。完整 GUI、自动订阅更新和 TUN 切换尚未完成端到端实测。

## 安装

先在 FlClash 导入并选中要绑定的订阅，应用一次。然后在本项目目录安装依赖：

```powershell
py -3 -m pip install -r .\requirements-flclash.txt
```

只读预检查（不修改 FlClash 配置、不注册任务）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1
```

若提示无法定位数据目录，通过 FlClash 的数据目录入口找到包含 `database.sqlite`、`shared_preferences.json` 和 `profiles` 的目录，再显式指定。标准 Windows 路径通常是 `%APPDATA%\com.follow\clash`；便携安装以实际数据目录为准：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1 `
  -DataDir 'D:\FlClashData'
```

预检查通过后，从系统托盘**完全退出 FlClash**，再安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1 -Install
# 自定义目录需要继续传 -DataDir；多个 Python 可加 -PythonPath 'C:\Python313\python.exe'
```

重新启动 FlClash，应用当前订阅，等待 60–90 秒查看状态。FlClash 与安装器使用同一个 Windows 用户。

### 安装器会做什么

1. 备份相关文件、SQLite 数据库和已有同名计划任务。
2. 为当前订阅创建并绑定“自动选择健康隔离与恢复 (Auto Health)”脚本。
3. 若原订阅使用脚本，先调用原脚本，再应用健康规则；保留原脚本文件和记录。后台会读取原脚本文件的更新，通常下一轮写入组合脚本；请不要编辑生成的组合脚本。
4. 在 FlClash 设置中启用 `127.0.0.1:9090` 控制接口。保留现有密钥；没有密钥则生成随机密钥。不会开放局域网地址。
5. 将程序复制到数据目录下的 `adaptive-auto-selection-flclash`，注册当前用户的每分钟任务；注册或文件校验失败时回滚。

安装后 Python 环境必须保留，不能卸载 PyYAML 或移动 Python。项目源码目录可以移动，因为任务运行的是复制后的程序。

## 查看状态

将 `$data` 替换为安装时的数据目录：

```powershell
$data = Join-Path $env:APPDATA 'com.follow\clash'
$health = Join-Path $data 'adaptive-auto-selection-flclash'
Get-Content (Join-Path $health 'status.json') -Encoding UTF8
Get-Content (Join-Path $health 'agent.log') -Encoding UTF8 -Tail 30
$installation = Get-Content (Join-Path $health 'installation.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Get-ScheduledTask -TaskName $installation.task_name | Get-ScheduledTaskInfo
```

- `cycle_complete` 且 `rules_applied: true`：本轮已完成；`quarantined_count: 0` 可以是正常情况。
- `inactive_profile`：选中了其他订阅，或解除了专用脚本绑定，检测暂停。
- `awaiting_profile_apply`：运行配置没有本安装的标记；在 FlClash 应用绑定的订阅。不要通过手动修改标记绕过检查。
- `core_not_running`：控制接口无法连接；检查 FlClash 是否启动。
- `runtime_mismatch`：磁盘配置与内核节点/组不一致；等待应用订阅完成，再重试。
- `cycle_error`：查看日志。HTTP 401/403 通常是密钥或控制接口发生变化；先在 FlClash 重新应用订阅。若处于 FlClash 安全模式，控制接口可能关闭。

立即运行一轮：

```powershell
py -3 (Join-Path $health 'flclash_health.py') --data-dir $data
```

`--force-probe` 会提前复测已隔离节点。编辑 `$health\policy.json` 调整规则，下一轮生效。不要修改 `profile_id`，也不要把另一台电脑的状态直接复制过来。

## 检测与重载行为

- 默认要求探测地址返回 HTTP 204。连续失败 3 次，或至少 8 个样本中最近最多 10 次失败至少 3 次，进入隔离。
- 隔离节点约每 2–3 分钟复测，连续成功 3 次恢复。恢复后仍会持续检测。
- 只改变 `url-test` 组的健康设置和排除规则；保留原有排除条件。手动组与原始节点仍存在，重载后恢复手动组的当前选择，以及 `url-test` / `fallback` 组的图钉首选。
- 图钉首选优先读取当前订阅的 `selected_map`，不会把自动组选出的临时节点误设为首选。首选节点被隔离时暂不恢复图钉，保留 FlClash 中的原始选择；节点恢复回组后自动设回。取消首选也会被尊重。无需重载的检测周期同样能修复旧版脚本丢失的图钉。
- 后台通过带认证的本机 HTTP API 读取健康记录和重载完整候选配置。内核拒绝候选时保留旧文件；通信结果不明确时尝试回滚。
- 使用 `force=false` 重载，避免因后台维护开启用户已停止的监听端口。配置重载仍可能短暂影响健康历史或连接，请以目标客户端的实际行为为准。
- 运行配置带有安装标记，并在修改前后检查当前订阅、脚本绑定和文件内容；遇到并发切换/更新会延后。外部 API 与 GUI 无法共享原子事务，请避免在配置频繁切换时强制执行多轮检测。
- GUI 重新生成配置时会调用绑定的覆写脚本，带上最近一次隔离名单。仅复制某个 JS 快照不能替代后台检测和自动恢复。

## 暂停、恢复、卸载

```powershell
Disable-ScheduledTask -TaskName $installation.task_name
Enable-ScheduledTask -TaskName $installation.task_name
Start-ScheduledTask -TaskName $installation.task_name
```

彻底卸载：先从托盘退出 FlClash，再运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1 -Uninstall
# 非默认目录同样传 -DataDir
```

卸载会删除任务和生成脚本，恢复该订阅原来的脚本绑定；若控制接口设置仍与安装时相同，也恢复安装前的设置。用户后来修改过的控制接口设置保留。若其他订阅也手动绑定了生成脚本，先在界面解除那些额外绑定。

最后重新启动 FlClash 并应用订阅，移除运行配置中的隔离规则。健康历史与备份保留在本地。备份和 `installation.json` 可能包含订阅凭据或控制接口密钥，请勿上传、分享或提交。

## 实现依据

对照 FlClash v0.9.2 源码核对：

- `lib/common/path.dart`：配置、数据库和脚本路径。
- `lib/common/preferences_store.dart` / `preferences.dart`：`flutter.config` 的序列化。
- `lib/database/profiles.dart` / `scripts.dart`：订阅与覆写脚本关联。
- `lib/providers/actions/setup.dart`：脚本优先执行及完整配置生成。
- `plugins/rust_api/rust/src/script/mod.rs`：`main(config)` 和 Promise 返回值。
- `core/Clash.Meta/hub/route/configs.go`：候选配置解析、HTTP 重载与 `force` 参数。

源码入口：<https://github.com/chen08209/FlClash/tree/v0.9.2>。对应 Mihomo 子模块提交：`c5062921054902b25215a6a933e72ccd714c9bfa`。
