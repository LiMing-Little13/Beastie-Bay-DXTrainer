# Beastie Bay DX Trainer

《Beastie Bay DX》（开罗游戏）Windows / Steam 版实时修改器。

程序通过 [Frida](https://frida.re/) 附加到游戏进程 `KairoGames.exe`，调用 `GameAssembly.dll` 导出的 IL2CPP API 定位游戏对象与字段，直接读写内存中的数值。**不会修改游戏安装目录中的任何文件**，所有改动仅作用于当前运行的游戏进程。

> 仅建议用于单机 / 离线存档。首次写入前会自动备份存档，但仍请自行承担使用风险。

## 功能

- **概览**
  - 读取 / 修改金钱、奖牌、食物、资源（木材）
  - 奖牌、食物、资源支持「锁定」：每 0.7 秒补到当前研究上限 - 1，研究升级后自动跟随新上限
  - 写入后自动回读校验，并同步游戏内的字节镜像字段
- **同伴属性**
  - 等级（1~99）、天赋星级（1~5）、性格、特征、装备插槽数
  - HP / MP 当前值与上限；攻、防、速、劳、智慧
  - 「恢复满 HP/MP」「一键拉满」
  - 属性写入成长数组 `addParam_`，升级后不会被游戏重算覆盖
- **物品栏**
  - 装备 / 消耗 / 生存三类物品列表
  - 修改数量、批量设为 98（堆叠上限 99 - 1）
  - 装备强化等级
- **设施**
  - 扫描各岛全部设施，按名称、类型筛选，或按岛 + X/Y 坐标定位
  - 读取 / 修改设施的文明、自然、能量、回复量
- **存档备份**
  - 每次启动后的首次写入或锁定前，自动完整复制游戏存档
  - 备份目录可配置，且强制要求位于游戏目录之外
- **设置**
  - 备份目录、启动时自动连接游戏、窗口尺寸记忆

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11 |
| 游戏 | Beastie Bay DX（Steam 版），进程名 `KairoGames.exe` |
| Python | 3.12（启动脚本默认查找 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`） |
| 包管理 | [uv](https://docs.astral.sh/uv/) |
| 运行库 | `frida==17.26.3`（首次启动时自动安装到项目内 `.venv`） |

## 快速开始

1. 启动游戏并载入存档。
2. 双击 **`启动修改器.bat`**。
   - 首次运行会自动创建 `.venv`，并通过 uv 安装 `frida==17.26.3`，需要能访问 PyPI。
   - 之后会直接以 `pythonw.exe` 启动 `trainer.py`。
3. 修改器默认在启动时自动连接游戏；若显示未连接，点击「重新连接」。
4. 在对应页面修改数值后点击「应用」。首次写入前会自动创建存档备份。

手动启动（可选）：

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe frida==17.26.3
.venv\Scripts\python.exe trainer.py
```

## 重要：存档路径

`trainer.py` 中的存档路径目前为硬编码：

```python
GAME_SAVES = Path(r"E:\SteamLibrary\steamapps\common\Beastie Bay DX\saves")
```

如果你的 Steam 库不在 `E:\SteamLibrary`，请把这一行改为实际的 `Beastie Bay DX\saves` 目录。否则自动备份会失败，出于安全考虑修改操作会被中止。

> 请勿把备份目录设置在游戏安装目录内，程序会拒绝这种配置。

## 文件说明

| 路径 | 说明 |
| --- | --- |
| `trainer.py` | 主程序：Tkinter 界面 + Frida IL2CPP 注入脚本 |
| `启动修改器.bat` | 启动器：创建虚拟环境、安装依赖并启动修改器 |
| `documents/rea.md` | 逆向分析笔记：内存结构、字段偏移、RPC 接口、已知限制 |
| `probe_hooks.py` | 调试探针：挂钩 `Building` / `Map` 方法，观察设施参数重算时机 |
| `probe_param_revert.py` | 调试探针：写入设施参数并轮询是否被游戏回退 |
| `trainer_settings.json` | 本地运行时设置（自动生成，已加入 `.gitignore`） |
| `backups/` | 存档备份目录（自动生成，已加入 `.gitignore`） |

`probe_*.py` 仅用于逆向调试，正常使用修改器时不需要运行。

## 工作原理

1. Frida 附加到 `KairoGames.exe`，加载内嵌的 JavaScript 脚本。
2. 脚本通过 `il2cpp_domain_get_assemblies`、`il2cpp_class_from_name`、`il2cpp_class_get_fields` 等 API 动态解析类与字段，不依赖固定地址。
3. 从静态单例 `main.AppData.apdat_` 找到 `game.Player`，再访问资源、同伴（`data.Enemy`）、物品和地图设施（`game.Building`）。
4. 写入时同步维护游戏使用的镜像字段（如 `moneyBytes_`、`researchPointBytes_`、`coinHistory_`），并在写入后回读校验。
5. 同伴属性通过成长值 `addParam_` 修改，避免升级重算时丢失；智慧则写入 `defWise_`。

更完整的字段布局与逆向过程见 [`documents/rea.md`](documents/rea.md)。

## 已知限制与注意事项

- 仅针对当前测试的游戏版本；游戏更新后字段布局可能变化，需要重新分析。
- 仅供单机 / 离线使用，请勿用于联机、排行榜或其他竞争性场景。
- 战斗属性存在 32 位乘法溢出风险，程序将攻 / 防 / 速 / 劳限制在 9,990,000 以内。
- 物品堆叠数量受游戏 `SByte` 限制，上限为 99。
- 设施参数保存在 `SByte` 覆盖数组中，部分设施可能被游戏重新计算。
- 部分同伴（储备笼中的 `Object[]`）结构特殊，程序仅处理可识别的 `Enemy` 实例。
- **禁止向游戏安装目录写入任何文件**，本程序只读取游戏目录中的存档用于备份。

## 免责声明

本项目仅供学习、研究和个人离线娱乐使用。使用修改器可能违反游戏服务条款，或影响成就、云存档等正常功能，一切后果由使用者自行承担。
