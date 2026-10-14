<p align="center"><img src="custom_components/tpac/brand/icon.png" alt="TPAC 图标" width="128" height="128"></p>

# TPAC

**中文** | [English](README.en.md)

TPAC 是 TP-LINK 无线 AP 的软件控制器（AC），以 Home Assistant 自定义集成的形式提供。它接管局域网中的 AP，让你不用 TP-LINK 硬件 AC，直接在 Home Assistant 里管理 AP。

TPAC 是独立项目，与 TP-LINK 公司没有任何关联，也未获得其认可。

## 为什么用 TPAC

TP-LINK 硬件 AC 是一台要另外购买、供电和登录管理的设备。TPAC 在你已经运行的 Home Assistant 里完成同样的工作。

- **不需要额外硬件，也不需要第二台路由器。** TP-LINK 的 AC 其实是一台内置 AC 功能的完整路由器，设计上要当网关使用，因此会干扰你现有的网络：
  - Wi-Fi 客户端默认在它的 NAT 后面。和主路由并存时会形成双重 NAT，端口转发更麻烦，两个网段的设备也互相发现不了。
  - 它自带 DHCP 服务器，必须记得关掉，否则会和主路由抢着分配地址。
  - 网络里多了一台设备：多一个电源、一套要打补丁的固件、一个管理员密码，也多一个故障点。它升级重启时，所有 AP 都无法管理。
  - 能接多少台 AP、能开多少个网络，都由你买的型号决定。

  用 TPAC 就可以撤掉这台设备。主路由仍是网络里唯一的路由器，AP 改由 Home Assistant 管理。
- **Wi-Fi 成为智能家居的一部分。** 每台 AP、每个 SSID、每个 Wi-Fi 客户端都是 Home Assistant 设备，可以直接用在自动化里：夜间关闭 AP 指示灯、只在聚会时开放访客网络、手机连上或离开 Wi-Fi 时运行脚本。
- **顺带获得在家检测。** TPAC 知道每个客户端连在哪台 AP 上。每层或每个房间一台 AP 时，就能得到楼层或房间级的在家状态和漫游记录，不需要额外的传感器，也不用轮询路由器。
- **历史记录集中在一处。** 连接、漫游和 AP 事件都进入 Home Assistant 的活动记录和历史，与家里其他设备放在一起。
- **用 profile 代替逐台配置。** 一组网络只需定义一次，可应用到任意 AP，之后随时修改。在线的 AP 几秒内生效，不用重启，每个网络都能看到下发结果。
- **本地运行，保护隐私。** 不需要云账号和厂商 App，也不开放远程访问。所有状态都保存在 Home Assistant 的配置目录里，profile 可以导出成文件备份。
- **不受单会话网页的限制。** 可以在任意 Home Assistant 仪表板、任意设备上管理 AP，多人可同时操作，也可以通过 Home Assistant API 调用。
- **开源。** 你可以阅读和修改代码，厂商停止更新后仍能继续使用。

如果你需要 TPAC 还不支持的 AP 型号、开放网络或 WPA3，或者需要厂商技术支持，硬件 AC 仍是更好的选择。

## 功能

- 发现局域网中寻找控制器的 AP。只有在你确认收养之后，TPAC 才会控制该 AP。
- Wi-Fi profile：一组命名的 SSID（WPA/WPA2-PSK、VLAN、隐藏），可应用到一台或多台 AP。改动立即下发到在线的 AP，并显示每个网络的结果。
- 每台 AP、每个 SSID、每个 Wi-Fi 客户端都是一个 Home Assistant 设备，带连接状态、信号强度、地址，漫游事件会记入活动记录。
- AP 指示灯开关、重启、恢复出厂设置和国家码。
- 在厂商网站检查 AP 的新固件。
- 侧边栏面板，用于收养设备、管理 profile 和查看客户端。

已在 TL-7AP5100HC 和 TL-XAP1502GI-PoE 上测试，它们原本由 TL-R470GP-AC 管理。其他型号可能可用，但未经验证。

## 安装前准备

1. **Home Assistant OS 或 Supervised**，与 AP 在同一个局域网（同一二层网段）。Container 和 Core 安装方式只有在 Home Assistant 使用宿主机网络时才可用。
2. Home Assistant 主机上的 **UDP 5246 端口**必须空闲，不能被其他应用或加载项占用。
3. **不能有其他控制器。** 已连到硬件 AC 的 AP 会一直留在那台 AC 上。请先关闭或断开硬件 AC，再重启或复位 AP。
4. **AP 在其他网段，或 Home Assistant 收不到 AP 的广播时**，在 DHCP 服务器上配置 option 138，见下文"配置 DHCP option 138"。
5. 查出面向 AP 的网卡名称：**设置 → 系统 → 网络**，或在终端运行 `ha network info`。常见名称有 `enp0s1`、`eth0`、`end0`。

### 配置 DHCP option 138

AP 默认用广播寻找控制器。以下情况需要在 DHCP 服务器上设置 option 138（CAPWAP 控制器地址），让 AP 直接联系 Home Assistant：

- AP 和 Home Assistant 不在同一网段；
- Home Assistant 运行在虚拟机里，虚拟网卡收不到局域网广播。

option 138 的值是 Home Assistant 的 IPv4 地址（4 字节）。下面以 `192.168.1.10` 为例，请换成你自己的地址。最好只对 AP 所在的网段或 AP 的租约设置这个选项。

**MikroTik RouterOS**

```
/ip dhcp-server option add name=capwap-ac code=138 value="'192.168.1.10'"
```

然后在 **IP → DHCP Server → Networks** 中打开 AP 所在的网段，把 `capwap-ac` 加入 **DHCP Options**（会保留已有的选项）。

**OpenWrt**

```bash
uci add_list dhcp.lan.dhcp_option='138,192.168.1.10'
uci commit dhcp
/etc/init.d/dnsmasq restart
```

**dnsmasq（包括 Pi-hole）**

```
dhcp-option=138,192.168.1.10
```

**ISC Kea（DHCPv4）**

```json
"option-data": [ { "code": 138, "name": "capwap-ac-v4", "data": "192.168.1.10" } ]
```

设置后，AP 要在下次获取租约时才会拿到新选项：重启 AP，或等租约续期。AP 在其他网段时，路由器还必须允许它到 Home Assistant 的 UDP 5246 流量通过。

## 安装

### HACS（推荐）

1. 打开 **HACS → ⋮ → 自定义存储库**。
2. 添加 `https://github.com/janlay/tpac`，类型选 **集成**。
3. 搜索 **TPAC**，打开后选择 **下载**。
4. 重启 Home Assistant：**设置 → 系统 → 右上角 ⏻ → 重启 Home Assistant**。

### 手动安装

在 Home Assistant 主机的终端中运行（例如 **Terminal & SSH** 加载项）：

```bash
cd /tmp
wget -O tpac.zip https://github.com/janlay/tpac/archive/refs/heads/master.zip
unzip -o tpac.zip
mkdir -p /config/custom_components
rm -rf /config/custom_components/tpac
cp -r tpac-master/custom_components/tpac /config/custom_components/
rm -rf tpac.zip tpac-master
ha core check && ha core restart
```

更新时再运行一次同样的命令。

## 设置

1. **设置 → 设备与服务 → 添加集成 → TPAC**。
2. 输入上面第 5 步查到的网卡名称。只能添加一个 TPAC 条目。
3. 寻找控制器的 AP 会出现在 **已发现** 中。选择 **添加** 并确认收养。AP 大约一分钟内入网，状态变为 `online`。
4. 打开侧边栏的 **TPAC**。`default` profile 包含三个网络：2.4 GHz、5 GHz，以及 VLAN 4084 上的 2.4 GHz 访客网络。它们默认未启用、没有密码。设置 SSID 和密码，启用需要的网络，然后选择 **Apply**。
5. 按需添加更多 profile（例如 IoT 或访客网络），并应用到需要广播这些网络的 AP。

## 让 AI Agent 安装

让你的 Agent（例如 Claude Code）能通过 SSH 登录 Home Assistant 主机，然后把下面的提示词粘贴给它：

> 在我的 Home Assistant 主机 `<user>@<host>` 上安装 TPAC 集成（https://github.com/janlay/tpac），按其 README 的"安装前准备"和"手动安装"两节操作：
> 1. 检查主机上 UDP 5246 是否空闲（`netstat -ulnp | grep 5246` 应没有输出），并把 `ha network info` 列出的网卡名称告诉我。
> 2. 把 `master` 分支的 `custom_components/tpac` 复制到 `/config/custom_components/tpac`，覆盖旧版本。
> 3. 运行 `ha core check`，只有通过后才运行 `ha core restart`。
> 4. 重启完成后运行 `ha core logs | grep -i tpac`，报告其中的错误。
> 不要修改其他任何文件。不要添加集成条目，我会在界面里操作。

## 动作

| 动作 | 用途 |
|---|---|
| `tpac.adopt`、`tpac.forget` | 收养 AP，或撤回收养 |
| `tpac.assign_profiles` | 把一个或多个 profile 应用到 AP |
| `tpac.import_profiles`、`tpac.export_profiles` | 从 JSON 文件导入全部 profile，或导出到 JSON 文件 |
| `tpac.reboot`、`tpac.factory_reset` | 重启 AP，或清除其配置 |
| `tpac.set_country` | 设置 AP 的国家码 |
| `tpac.check_ap_update`、`tpac.upgrade_ap` | 检查 AP 新固件；上传固件（实验性） |

各动作的字段见 **开发者工具 → 动作**。

## 用 MikroTik 路由器的备注命名客户端

`scripts/sync-routeros-client-names.sh` 用 RouterOS DHCP 租约的备注给 Wi-Fi 客户端命名。在一台能通过 SSH 同时登录路由器和 Home Assistant 的电脑上运行：

```bash
ROUTER=admin@192.168.88.1 HA=root@homeassistant.local ./scripts/sync-routeros-client-names.sh
```

脚本写入 `/config/tpac_client_names.json`，TPAC 会立即读取。修改租约备注后再运行一次即可。其他工具也可以写这个文件：内容是一个 JSON 对象，键为小写 MAC 地址，值为名称。

## 数据与安全

- TPAC 的状态保存在 `/config/tpac/`。`tpac-profiles.json` 以明文保存 Wi-Fi 密码（文件权限 0600）。`tpac.export_profiles` 导出的文件也包含密码。
- TPAC 在首次启动时，在 `/config/tpac/` 生成自己的控制器证书和私钥。
- 实体的状态和属性中从不显示 Wi-Fi 密码。
- `dtls_established` 事件包含 AP 会话的密钥。能读取 Home Assistant 历史数据库的人可以解密该会话。

## 卸载

1. 在 **设置 → 设备与服务** 中删除 TPAC 条目。AP 失去控制器后会寻找其他控制器。
2. 删除 `/config/custom_components/tpac`；不再需要 profile 时，同时删除 `/config/tpac`。然后重启 Home Assistant。

## 限制

- 不支持开放网络和 WPA3-SAE。
- 只验证过双频（2.4 GHz 和 5 GHz）AP。
- 固件上传（`tpac.upgrade_ap`）是实验性功能。
