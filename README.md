# Mihomo Auto Health · 自动健康隔离与恢复

本项目为 Mihomo 客户端提供第三方本地健康检测、失败节点隔离和恢复，不是客户端内置插件，也不是独立代理客户端。两个客户端共用健康判定算法，使用各自的配置、覆写和安装适配。

| 客户端 | 平台与入口 | 状态 |
| --- | --- | --- |
| [Clash Party](https://github.com/mihomo-party-org/clash-party) | macOS：`install_macos.sh`；Windows：`install_windows.ps1` | 原有适配保留 |
| [FlClash](https://github.com/chen08209/FlClash) | Windows：`install_flclash_windows.ps1`；[完整说明](FLCLASH.md) | 新增适配，按 v0.9.2 数据结构实现，已验证自带内核；完整 GUI 联调待验证 |

持续超时或失败的节点会暂时从 `url-test` 自动组排除，恢复后自动加回。节点仍保留在原始节点列表和手动选择组中。后台读取 Mihomo 健康检查记录、维护本地覆写，并定期复测隔离节点，不依赖 Sub-Store 或共享服务器。

### FlClash 快速入口（Windows）

安装 Python 3（包含 `pythonw.exe`），在本项目目录运行：

```powershell
py -3 -m pip install -r .\requirements-flclash.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1
# 预检查通过后，从托盘完全退出 FlClash，再安装：
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_flclash_windows.ps1 -Install
```

安装器会为当前订阅绑定专用脚本，启用带密钥的本机控制接口，并注册每分钟运行的后台任务。支持已有覆写脚本组合；安装前备份，失败回滚。启动 FlClash 后应用一次当前订阅。路径参数、限制、状态和卸载方法见 [FLCLASH.md](FLCLASH.md)。

### 项目与仓库命名

项目名称为 **Mihomo Auto Health**，仓库为 `FingerCaster/mihomo-auto-health`。Clash Party 和 FlClash 共用一个仓库，共享检测算法和测试，分别维护客户端适配。Clash Party 原有安装入口继续保留；FlClash 使用独立安装入口。

## Clash Party 项目与下载

- 官方项目：[mihomo-party-org/clash-party](https://github.com/mihomo-party-org/clash-party)
- 安装包下载：[GitHub Releases](https://github.com/mihomo-party-org/clash-party/releases)
- 官网与使用文档：[clashparty.org](https://clashparty.org/)

## Clash Party 安装前准备

1. 安装并启动 Clash Party，导入订阅并选中需要绑定的订阅。
2. 生成的配置须包含内联 `proxies` 和至少一个 `url-test` 自动组；只使用 `proxy-providers` 的配置暂不支持。
3. 安装 Python 3 和 Node.js，然后完全退出 Clash Party。

## Clash Party macOS 安装

需要 macOS、Python 3、Node.js 和 Clash Party。默认应用路径为 `/Applications/Clash Party.app`，默认数据目录为 `~/Library/Application Support/mihomo-party`。

下载本仓库 ZIP 并解压，或运行：

```sh
git clone https://github.com/FingerCaster/mihomo-auto-health.git
cd mihomo-auto-health
```

退出 Clash Party，然后执行：

```sh
./install_macos.sh
```

非标准目录可显式传入 Clash Party `Contents/Resources` 和应用数据目录：

```sh
./install_macos.sh \
  --resources-dir "/Applications/Clash Party.app/Contents/Resources" \
  --data-dir "$HOME/Library/Application Support/mihomo-party"
```

安装完成后启动 Clash Party，后台每分钟检查一次。查看状态和日志：

```sh
cat "$HOME/Library/Application Support/mihomo-party/adaptive-auto-selection/status.json"
tail -n 30 "$HOME/Library/Application Support/mihomo-party/adaptive-auto-selection/agent.log"
```

卸载后台任务：

```sh
./uninstall_macos.sh
```

然后在 Clash Party 当前订阅的覆写设置里解除绑定“自动选择健康隔离与恢复”，并重新加载订阅。卸载脚本保留覆写文件和健康历史，避免在解除订阅绑定前破坏现有配置。

## Clash Party Windows 10 / 11 安装

需要当前 Windows 用户下安装 Clash Party、带 `pythonw.exe` 的 Python（推荐 3.13）和 Node.js LTS。解压仓库 ZIP，在 PowerShell 中进入该目录，先运行只读预检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1
```

预检查通过后，从系统托盘完全退出 Clash Party，再执行安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -Install
```

便携版或自定义安装路径可传 `-AppDir`、`-DataDir`、`-PythonPath` 和 `-NodePath`。详细步骤、状态检查和卸载方式见 [Windows 安装说明](WINDOWS.md)。

## 检测规则

- 探测 `https://www.gstatic.com/generate_204`，要求在超时内收到 HTTP 204。
- 连续失败 3 次，或至少有 8 个有效样本且最近最多 10 次中至少失败 3 次，就隔离节点。
- 隔离节点约每 2–3 分钟复测一次，连续通过 3 次后恢复到符合既有筛选条件的自动组。
- 只修改 `url-test` 自动组；原节点和手动组不变。自动组没有候选时使用 `REJECT`。
- 可修改 `adaptive-auto-selection/policy.json` 调整规则参数，下一轮检测时生效。

探测成功只说明指定探测地址返回了 HTTP 204，不代表所有网站都可访问。切换到其他订阅时，检测会暂停；切回安装时绑定的订阅后继续运行。

## 数据与隐私

状态、日志和安装备份保存在对应客户端的本机数据目录。备份可能包含订阅凭据、控制接口密钥，请勿分享或提交。本仓库不包含任何用户订阅、节点或检测状态。

## 开发与测试

```sh
python3 -m pip install -r requirements-flclash.txt
python3 -m unittest discover -s . -p 'test_*.py' -v
```

原 Clash Party 的 YAML/JS 测试需要 Node.js 及其已安装的 YAML 库。FlClash 使用 PyYAML，运行时不依赖 Node.js；脚本组合测试使用 Node.js。

可选的真实 FlClash 内核集成测试（只在临时目录和本机随机端口运行，不修改实际订阅、系统代理或 TUN）：

```powershell
$env:FLCLASH_TEST_CORE = 'C:\Path\To\FlClashCore.exe'
py -3 -m unittest test_flclash_core -v
```

完整 FlClash GUI 的订阅更新、后台任务和 TUN 生命周期仍需在目标安装上验证；单独内核测试不等同于所有客户端版本实测。

## 许可证

本项目使用 GNU Affero 通用公共许可证第 3 版，见 [LICENSE](LICENSE)。
