# Beastie Bay DX 逆向分析与修改器实现文档

## 目标游戏
- **游戏名**：Beastie Bay DX (Kairosoft)
- **进程名**：`KairoGames.exe`
- **引擎**：Unity IL2CPP (Windows)
- **核心模块**：`GameAssembly.dll`

---

## 核心内存结构（IL2CPP 静态字段偏移）

### 单例入口
| 类 | 静态字段 | 类型 | 说明 |
|---|---|---|---|
| `main.AppData` | `apdat_` | `AppData*` | 全局单例，持有 `player_` 指针 |

### Player 关键字段（`game.Player`）| 字段 | 偏移 | 类型 | 说明 |
|---|---|---|---|
| `money_` | `moneyFieldOffset` | `int32` | 金钱（内联，非对象） |
| `moneyBytes_` | `moneyBytesFieldOffset` | `byte[8]` | 金钱小端镜像，**需同步写** |
| `researchPoint_` | `researchFieldOffset` | `Int32[3]` | 资源数组：[0]=食物, [1]=资源(木材), [2]=奖牌 |
| `researchPointBytes_` | `researchBytesFieldOffset` | `byte[3][4]` | 资源镜像，**需同步写** |
| `coinHistory_` | `coinHistoryFieldOffset` | `FastVector<Int64[]>` | 奖牌变动史，最新条目 `elementData[0][3]` 为当前余额 |
| `stamina_` | `staminaFieldOffset` | `SecureLong[?]` | 体力（加密封装） |
| `kairoPoint_` | `kairoPointFieldOffset` | `ExInt` | 开罗点数 |
| `enemyBox_` | `enemyBoxFieldOffset` | `Vector<Enemy>` | 出战队伍（活数据可改） |
| `reserveCasket_` | `reserveCasketFieldOffset` | `Vector<Object[]>` | 储备笼（序列化快照，**不可改**） |
| `dice_` | `diceFieldOffset` | `game.Dice` | 双六骰子实时状态 |
| `sugorokuList_` | `sugorokuListFieldOffset` | `SugorokuData[]` | 双六关卡列表 |
| `sugorokuPlayNum_` | `sugorokuPlayNumFieldOffset` | `ExInt` | 剩余投掷次数 |

### Enemy（同伴/怪兽，`data.Enemy`）
| 字段 | 偏移 | 类型 | 说明 |
|---|---|---|---|
| `name_` | `enemyNameOffset` | `string` | 名称 |
| `Lv_` | `enemyLvOffset` | `int32` | 等级 (1~99) |
| `racialLv_` | `enemyRacialLvOffset` | `int32` | 天赋星数内部值 (0~4，显示+1) |
| `personality_` | `enemyPersonalityOffset` | `int32` | 性格索引 (0~6) |
| `attribute_` | `enemyAttributeOffset` | `int32` | 特征索引 (0~5) |
| `battleHP_` | `enemyBattleHPOffset` | `int32` | **当前战斗 HP（显示值，升级会重算覆盖）** |
| `battleMP_` | `enemyBattleMPOffset` | `int32` | **当前战斗 MP（显示值，升级会重算覆盖）** |
| `defParam_` | `enemyDefParamOffset` | `Int32[14]` | **基础属性数组（含种族加成），升级不覆盖** |
| `addParam_` | `enemyAddParamOffset` | `Int32[14]` | 成长值数组 |
| `defWise_` | `enemyDefWiseOffset` | `int32` | 智慧基础 |
| `addWise_` | `enemyAddWiseOffset` | `int32` | 智慧成长 |
| `equipMax_` | `enemyEquipMaxOffset` | `int32` | **装备插槽数（独立字段，与天赋星数无关；全局上限 `EQUIP_MAX=5`）** |
| `equip_` | - | `java.util.Vector` | 已装备物品列表 |

#### defParam_/addParam_ 索引映射（`PARAM_*_MAX` 常量，实测值）
| 索引 | 属性 | GetParam 参数 | 备注 |
|---|---|---|---|
| 0 | HP 上限 | 0 | 差值写 addParam_[0]（不动 defParam_，升级重算后保留） |
| 2 | MP 上限 | 2 | 差值写 addParam_[2]（同上） |
| 4 | 攻击 | 4 | 生效值 = defParam_[4] + addParam_[4] + 种族加成 |
| 5 | ⚠️ 非智慧 | 5 | GetParam(5) 返回 defParam_[5]（其他内部参数），**与界面显示智慧无关** |
| 6 | 防御 | 6 | 含种族加成 |
| 8 | 速度 | 8 | 含种族加成 |
| 10 | 劳动 | 10 | 含种族加成 |

> **智慧真相**：界面显示智慧 = `min(defWise_, 99) + 自然成长`（自然成长随等级累积、不受修改影响，
> 实测 Lv7 时为 13；常量 `RACIAL_WISE_ADD = 2`）。写 defWise_ 超过 99 无意义（显示封顶 99+自然成长）。
> 旧版写 `defParam_[5]` 只改 GetParam(5)（其他内部参数），界面智慧不变——v1.1 起改为读写 defWise_/addWise_。

> **装备插槽**：数量 = `equipMax_` 独立字段，**与天赋星数无关**（实测改 racialLv_ 插槽不变，直写
> equipMax_ 立即生效）。游戏全局上限 `EQUIP_MAX=5`；空槽数 = equipMax_ − 已装备数
> （`GetEquipEmptySlot`），已装备数是 `equip_` Vector 的元素个数。方法族：`GetEquipSlot()`
> （已装备数）、`GetEquipSlot(int)`（分类上限，实测武器/饰品各 2）、`Equip/RemoveEquip/ChangeEquip`。

> **升级重算机制（核心）**：升级/捕捉时游戏调用 `Enemy.AdjustParam` 按程序设计**重算 defParam_ 基础数组
> （随等级增长），但永久保留 addParam_ 成长数组**。因此所有属性的修改都必须写成**差值放进 addParam_**
> （显示值 = 设计基础 + 成长差值 + 种族加成）：升级后显示值 = 修改目标 + 自然成长，实测 HP 999 → 升级后
> 1034（+35）、MP 999 → 1003（+4），**只增不减，绝不掉回程序设定值**。直写 defParam_ 的写法（v1.0/v1.1）
> 升级时会被重算覆盖（999→28 实测），v1.3 起已废弃。`battleHP_`/`battleMP_` 是运行时当前值，升级会重算，
> 修改器不直接写它（用恢复满功能）；`defWise_`/`addWise_` 不受 AdjustParam 影响，直写 defWise_ 即可。

### 物品系统（`main.AppData` 定义表，玩家持有数存在表条目上）
| 分页 | 表字段 | 元素类 | 长度 | 持有判定 |
|---|---|---|---|---|
| 装备 | `equipList_` | `data.Equip` | 159 | `num_` > 0（实测 15 件） |
| 消耗 | `consList_` | `data.Consumables` | 42 | `num_` > 0 |
| 生存 | `survivalList_` | `data.Survival` | 19 | `num_` > 0 |

#### data.Equip 字段（offsets 实测）
| 字段 | 偏移 | 类型 | 说明 |
|---|---|---|---|
| `id_` | 8 | int32 | 装备 ID |
| `name_` | 20 | String | 名称 |
| `type_` | 32 | int32 | 0=武器(TYPE_WEPON) 1=饰品(TYPE_ACCESSORIES) |
| `slot_` | 36 | int32 | 占用插槽分类 |
| `param_` | 44 | Int32[] | 属性加成（强化成长用） |
| `price_` | 76 | Int32[] | 售价 |
| `num_` | 90 | **SByte** | 持有数量（0~127，常量 NUM_MAX） |
| `strengthen_` | 92 | **SByte** | 强化等级 |
| `strengthenCost` | 96 | Int32[] | **每级强化费用，数组长度 = 强化次数上限（实测全部 = 5）** |
| `compItem_` | 68 | Int32[][] | 合成配方 |

- 升级上限硬校验：`strengthen_ < strengthenCost.Length`（游戏限制 +5 级，溢出会被游戏自身逻辑拒绝/破坏存档一致性）
- `limit_`（SByte@24）经实测**不是持有上限**（出现过 num=10 > limit=1），是使用/携带限制，仅作展示；数量修改统一按 0~99 校验（SByte 硬顶 127）
- `data.Survival` 无 num_：字段为 `lv_`(int32@60) 等级 / `life_`(int64@64) 耐久 / `break_`(sbyte@68) 破损——生存页只读展示，修改属后续扩展
- `market.MarketItem` = 商店；`OrderItemData` = 内购订单（与背包无关）

### 设施系统（`game.Building` / `game.MapChip` / `game.Map`，v1.7 起按坐标制）
- 坐标定位：`Player.map_[岛序号]`（0 基，一座岛=0）→ `Map.mapchips_[y][x].building_` 非空即为设施/地块
  对象——**不依赖游戏光标/窗口栈**，坐标与游戏地图格位一致（实测岛 1 格 (22,23) = 独栋房屋）
- 设施名查表：`AppData.chipModel_`（`data.ChipModel[]`，119 项）按 `Building.modelID_` 直接取 `name_`（@20），
  **纯内存免 invoke**；`menuTypeID_`：2=地形（草/森…）、0/1=设施与道路——列表类型过滤的依据
- `Building` 关键字段：`modelID_`@12 / `build_`@16(SByte，建造中) / `param_`@40(Int16[] 差值数组) / `rebuild_`@44(SByte)
- `Map` 关键字段：`id_`@8 / `saveName_`@16 / `mapchips_`@12（MapChip[][]，行为 y、列为 x）
- 岛显示名：`Player.islandName_`（母岛=map_[0]，玩家可改名，默认「一座岛」）；其余岛按 `Map.id_` 查
  `AppData.islandList_`（`data.Island[]`，28 项，`name_`@20/`mapIndex_`@32，如 常夏岛/龟岛/电车岛…）。
  注意游戏字符串可能带尾部控制字符（实测 islandName_ 尾附 \x08），显示前需 `[\x00-\x1f]` 过滤
- `Building.GetParam(i)` = `(i < param_.Length ? param_[i] : 0) + 模型基础值`——**param_ 是差值数组**
  （实测：写 [11,22,33,44] 后 GetParam(3) 返回 45 = 44 + 基础 1；param_ 为 Int16[4]，全零表示无加成）
- 修改语义：目标显示值 T → `param_[i] = T - (当前显示 - 当前param_[i])`；只影响本格、随存档持久
- 参数索引（推断）：0=文明 1=自然 2=能量 3=回复量（各模型实测仅 idx1/idx3 非零，与住宅/设施语义吻合）
- `data.Rebuild` 是改建条件表（requirePoint/requireChips），不是属性表
- v1.6 及之前的光标定位链（FormManager 栈 → GameForm.mapCursor）已废弃：依赖游戏 UI 状态，实测不可用

---

## 静态常量/枚举

### 性格 (`data.Enemy.personality_` → `personalityStr` 顺序)
```
0: 平凡   1: 麻利   2: 勤劳   3: 顽固
4: 聪明   5: 易怒   6: 温顺
```

### 特征 (`data.Enemy.attribute_` → `ATTRIBUTE_*`)
```
0: 无   1: 火   2: 水   3: 风   4: 土   5: 雷
```

### 天赋星数
- 内部存储：0 基 (0~4)
- UI 显示：+1 星 (1~5 星)

### 安全数值上限
- 资源通用上限：`2,000,000,000` (int32 正数上界)
- 同伴属性战斗安全上限：`9,990,000`
  - 原因：战斗中属性会乘「属性相性」(100±affinity)/100 与「技能百分比」，均为 32 位有符号乘法，超过 ~1000 万会溢出导致伤害异常、防御变负、恢复异常

---

## Frida IL2CPP 脚本关键技术点

### 1. 线程附加必须成对（闪退根因）
```javascript
// 每个 RPC 导出：进入时 attach，返回时（含异常）finally detach
function withThread(handler) {
  return function (...args) {
    const thread = api.threadAttach(domain);
    try { return handler.apply(this, args); }
    finally { api.threadDetach(thread); }
  };
}
// 所有导出统一包装：rpc.exports = { readState: withThread(readState), ... }
```
> **教训**：只 `il2cpp_thread_attach` 不 detach，脚本卸载后死线程仍残留在 IL2CPP 线程列表，
> 游戏 GC 遍历线程扫描到死线程结构 → 随机闪退（异常码 0xc0000409 / StackHash）。
> 每开一次修改器就累积一条，重度使用后迟早触发。必须成对 attach/detach。

### 2. `invokeIntArg1` 三次重试 + system error 过滤
```javascript
for (let attempt = 0; attempt < 3; attempt++) {
  try { return invoke(...); }
  catch (e) { if (!String(e).includes('system error')) throw e; }
}
// 线程挂接由导出层 withThread 统一成对处理，见「线程附加必须成对」
```

### 3. 属性写入策略 (`applyMateParam`，v1.3 统一为成长差值)
- **全部属性**（hpMax/mpMax/atk/defense/spd/work）统一写 `addParam_` 差值，不动 `defParam_`：
  ```javascript
  now = GetParam(idx);            // 当前显示值(含种族加成)
  base = defParam[idx];
  growth = addParam[idx];
  delta = target - now + growth;  // = target - base - 种族加成
  addParam[idx] = Math.max(0, delta);  // 低于「基础+种族」时下限封顶，不砍基础
  ```
- **智慧** (wise)：直写 `defWise_`（不受 AdjustParam 影响），游戏显示封顶 99：
  ```javascript
  addWise_ = 0;
  defWise_ = Math.min(target, 99);
  ```
- 升级后表现：显示值 = 修改目标 + 自然成长（实测 HP 999 → 升级 1034）

### 4. 同伴枚举：穿透 `Human.crew_` 字段
```javascript
const klass = api.objectClass(entry).toString();
if (klass === humanClass.toString()) {
  crew = entry.add(offset(humanClass, 'crew_')).readPointer();
}
```

### 5. 双六骰子 Hook (`CreateDice`)
- `stepLock`：锁定总步数，每次投掷自动重新应用
- `gotoTarget`：目标格模式，按 `目标格 - 当前格` 计算步数，>99 分多次投掷
- 冗余调用去重：记录 `stepOverrideApplied/pos/level`，位置真正变化后才消耗一次性步数

---

## 修改器实现功能清单

| 标签页 | 功能 | 实现方式 |
|---|---|---|
| **概览** | 金钱/奖牌/食物/资源 读写 | `readSecure`/`writeSecure` + 镜像数组同步 |
| | 奖牌锁定 (上限-1) | 定时器 700ms 轮询 `GetResearchPointMax` 实时上限 |
| | 食物/资源锁定 | 同上 |
| | 自动备份存档 | 首次写入前 `shutil.copytree` 到 `backups/时间戳/` |
| **同伴属性** | 列表刷新 (出战/储备) | `readMates` + 分批 `readMatesParams/Params2` |
| | 等级/天赋/性格/特征 | 直接写 `Lv_`/`racialLv_`/`personality_`/`attribute_` |
| | 装备插槽 (equipMax_) | 直写字段（0~5，EQUIP_MAX）；**与天赋无关**，v1.1 新增 |
| | **HP/MP 基础上限** | 差值写 `addParam_[0]/[2]`（升级重算 defParam_ 时保留，实测 999→升级后 1034）✅ |
| | 当前 HP/MP 恢复满 | `healMate` 写 `battleHP_/MP_` = `GetParam(0/2)`（当前值，非上限） |
| | 攻/防/速/劳 | 同款差值算法（含种族加成换算） |
| | 智慧 | 直写 `defWise_`（不受重算影响），显示封顶 99，v1.3 统一 |
| | 一键拉满 (99级/5星/全属性 9990000) | 批量调用上述接口 |
| **物品修改** | 武器/道具/强化食物 分页 | 复用 `SubForm.GetItemList(tab)` 保证分页一致 |
| | 数量改值 / 上限-9 | `ItemData.SetNum` + `GetMaxNum` 校验 |
| **双六屋** | 剩余投掷次数改值/锁定 | `ExInt.Set` / 定时器轮询 |
| | 骰子步数锁定/一次性 | `CreateDice` Hook 注入 |
| | 目标格自动走位 | `gotoTarget` 模式，跨回合自动分步 |
| | 双六卡片数量改值 | `SugorokuItem.SetNum` |
| | 开罗点数改值 | `ExInt.Set` |
| **物品栏** | 装备/消耗/生存三分页 | `readItems` 读 AppData 三张表（equipList_/consList_/survivalList_） |
| | 数量修改（0~99），复选框多选批量 | `applyItemNum(Batch)` 直写 `num_`（SByte），勾选集合 = Treeview 多选 |
| | 一键加到上限-1（98） | 批量 `applyItemNumBatch` value=98 |
| | 装备强化（硬上限 = strengthenCost.Length = 5） | `upgradeEquip(Batch)` 校验后写 `strengthen_`，溢出拒绝/批量中单独跳过 |
| | 生存工具 | 只读展示（lv_/life_/break_ 结构不同，后续扩展） |
| **设施** | 全岛扫描设施/地块（岛/岛名/X/Y/名称） | `scanFacilities`：遍历 `map_`×`mapchips_`，名称查 `chipModel_` 表（纯内存免 invoke） |
| | 岛名显示：母岛读 `Player.islandName_`，其余按 `Map.id_` 查 `islandList_` | `mapDisplayName`（JS 侧，`[\x00-\x1f]` 过滤控制字符） |
| | 名称筛选 + 类型过滤（设施与道路/仅地形/全部） | `menuTypeID_`：2=地形、0/1=设施与道路；Python 侧 `_refilter_facilities` |
| | 坐标查找：岛下拉 + X/Y 输入 → 定位列表行（自动清筛选） | `locate_facility_xy` |
| | 按 (岛,X,Y) 读文明/自然/能量/回复（GetParam 0~3） | `readFacilityAt`+`readFacilityParamsAt2/3`，每 RPC ≤3 invoke，坐标定位不依赖光标 |
| | 保存四项属性 | `applyFacilityParamsAt` 写 param_ 差值数组（T - 基础值），保存前核对目标格快照 |

---

## 已知限制与注意事项

1. **存档路径硬编码**
   ```python
   GAME_SAVES = Path(r"E:\SteamLibrary\steamapps\common\Beastie Bay DX\saves")
   ```
   需按实际 Steam 库位置修改，否则自动备份失效（内存修改不受影响）。

2. **储备笼不可改**
   `reserveCasket_` 存的是序列化 `Object[]`，非活 `Enemy` 实例，强改会导致存档损坏。

3. **Frida 版本锁定**
   `frida==17.26.3`（启动脚本 `启动修改器.bat` 自动安装）。

4. **属性溢出保护**
   UI 限制 0~9,990,000；JS 侧 `applyMateParam` 内部再次校验。

5. **RPC 并发限制**
   连续 `runtimeInvoke` >4 次触发 system error，已拆分：
   - `read_mates` → `read_mates_params` → `read_mates_params2` (每批 ≤3 invoke)
   - 智慧与当前 HP/MP 为纯内存读取，随 `read_mates` 返回，不再占 invoke 配额
   - `apply_mate_param` 单属性单 RPC

---

## 关键代码定位（`trainer.py`）

| 功能 | 行号范围 |
|---|---|
| IL2CPP JS 脚本 | 57~467 |
| `IL2CPPGame` 类 | 497~607 |
| 同伴读取/写入 | 541~600 |
| UI 同伴标签页构建 | 794~860 |
| 同伴表单加载/收集/应用 | 1024~1122 |
| 资源锁定循环 | 925~985 |
| 双六 Hook 逻辑 | 136~239 |

---

## 复现/扩展指南

1. **新增同伴字段**：
   - JS: 在 `readMateCore`/`applyMateChanges` 增加 offset 读写
   - Python: `collect_mate_changes` 增加校验，`apply_mate_changes` 透传
   - UI: `_build_mate_page` 增加控件绑定 `StringVar`

2. **新增资源类型**：
   - 扩展 `RESOURCE_FIELDS`/`LOCKABLE_FIELDS`
   - JS `readState`/`applyChanges` 对应数组下标
   - UI 概览页自动生成

3. **其他 Kairosoft IL2CPP 游戏**：
   - 同引擎结构高度一致，只需调整类名/字段名/偏移
   - 可复用 `findClass`/`getField`/`offset` 通用模板

---

## 版本历史
- **v1.7** (2026-11-17)：设施页重做——废弃 FormManager/GameForm 光标定位（依赖游戏 UI 状态，实测无法使用），
  改为坐标制：`Player.map_[岛]`（一座岛=0）→ `mapchips_[y][x].building_` 按坐标直取设施对象。新增
  `scanFacilities` 全岛扫描（名称走 `AppData.chipModel_` 表，纯内存免 invoke；`menuTypeID_` 2=地形、0/1=设施与道路，
  列表支持名称筛选与类型过滤）；岛名：母岛读 `Player.islandName_`（默认「一座岛」、可改名、尾附 \x08 需过滤），
  其余岛按 `Map.id_` 查 `islandList_`；新增坐标查找（岛下拉 + X/Y 定位，自动清筛选）。已实测：岛 1 格 (22,23)=独栋房屋
  读写、+5/-5 改值还原、地形 359+设施道路 423=782 全对账、XY 定位/越界/清筛选、UI 冒烟通过
- **v1.6** (2026-11-17)：新增「设施」页——游戏内点开设施信息窗口后点「读取设施」，自动经
  FormManager→GameForm.mapCursor→MapChip.building_ 定位设施，读取并修改 文明/自然/能量/回复量
  （GetParam 0~3；param_ 为差值数组，按「目标 - 基础值」换算写入，仅本设施生效、随存档持久）。
  保存时光标移开会被拒绝；参数索引映射为推断值，已按各模型实测模式校验，待与游戏内显示对照
- **v1.5** (2026-11-17)：物品栏批量——复选框列多选（点击勾选、Ctrl/Shift 多选、全选/全不选），
  「应用数量」「加到上限-1（98）」「升级」均对全部勾选生效；JS 侧 `applyItemNumBatch`/`upgradeEquipBatch`
  单 RPC 逐条执行，装备强化超过各自 strengthenCost 上限的单独跳过不连坐；操作后勾选态保留
- **v1.4** (2026-11-17)：新增物品栏——装备/消耗/生存三分页（与游戏排布一致）。数量修改直写 `num_`
  （0~99）；装备强化写 `strengthen_`，硬上限 = `strengthenCost.Length`（游戏限制 +5 级），JS 侧校验溢出拒绝；
  生存工具只读展示（`lv_`/`life_`/`break_` 结构不同，列为后续扩展）。已实测：读 15 装备/6 消耗/19 生存、
  数量改值与还原、强化到 5/5、+6 溢出被拒、双会话稳定
- **v1.3** (2026-11-16)：属性写入策略修正——升级时游戏 `AdjustParam` 会重算 `defParam_` 基础数组（v1.1 直写
  基础的写法升级后被覆盖，实测 999→28），全部属性统一改为差值写入 `addParam_` 成长数组（重算时永久保留），
  升级后显示值 = 修改目标 + 自然成长（实测 HP 999→1034、MP 999→1003）；智慧显示封顶 99（写超无意义），
  表单显示/校验同步；模拟升级与真实升级路径均已实测验证
- **v1.2** (2026-11-16)：修复 IL2CPP 线程附加泄漏——所有 RPC 导出成对 attach/detach，脚本卸载不再残留
  死线程，消除游戏随机闪退（0xc0000409，此前 11/14 已有三次同因崩溃）
- **v1.1** (2026-11-16)：真机端到端校验通过；修复智慧读写错误（`defParam_[5]` → `defWise_/addWise_`，
  即游戏内 `GetParam(-1)`，`PARAM_WISE=-1`）；同伴界面按游戏内「同伴的状态」重排，
  HP/MP 当前值（只读）与上限（写基础值）分离；所有报错提示统一到底部状态条，移除弹窗；
  新增装备插槽修改（`equipMax_`，与天赋无关，上限 5）；修复恢复满 HP/MP 后界面刷新缺 defense 键的 KeyError；
  清理 `_probe.py` 与测试期存档备份
- **v1.0** (2026-11-16)：基础资源+同伴属性+物品+双六屋完整实现
- 关键修复：HP/MP 基础上限写 `defParam_` 避免升级覆盖、移除误报校验异常、同步 UI 双向绑定

---

*生成时间：2026-11-16*  
*分析工具：Frida 17.26.3 + IL2CPP 静态分析*  
*参考项目：High Sea Saga DX 修改器 (同架构复用)*