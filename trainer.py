import ctypes
import json
import os
import shutil
import tempfile
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk

import frida

GAME_NAME = "KairoGames.exe"
GAME_SAVES = Path(r"E:\SteamLibrary\steamapps\common\Beastie Bay DX\saves")
BACKUP_ROOT = Path(__file__).resolve().parent / "backups"
SETTINGS_FILE = Path(__file__).resolve().parent / "trainer_settings.json"
WINDOW_SIZE_DELAY_MS = 650


def validate_backup_root(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("备份目录不能为空")
    text = os.path.expandvars(str(value).strip())
    if "\x00" in text:
        raise ValueError("备份目录包含无效字符")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("备份目录必须是绝对路径")
    path = path.resolve()
    game_root = GAME_SAVES.parent.resolve()
    if path == game_root or game_root in path.parents:
        raise ValueError("备份目录不能位于游戏目录内，请选择修改器目录或其他位置")
    if path.exists() and not path.is_dir():
        raise ValueError("备份目录指向了文件，请选择文件夹")
    return path


# researchPoint_[0]=食物, [1]=资源(木材), [2]=奖牌；实测与游戏界面显示一致
RESOURCE_FIELDS = (("money", "金钱"), ("medals", "奖牌"), ("food", "食物"), ("wood", "资源"))
RESOURCE_LIMIT = 2_000_000_000
# 支持锁定的三项及其上限字段（锁定目标 = 实时上限 - 1）
LOCKABLE_FIELDS = (("medals", "medalsMax"), ("food", "foodMax"), ("wood", "woodMax"))
LOCK_INTERVAL_MS = 700
LOCK_MAX_FAILURES = 3
# 同伴属性：显示名（写入索引在 JS 侧，依据 PARAM_*_MAX 常量：
# HP上限=0, MP上限=2, 攻=4, 防=6, 速=8, 劳=10；智慧 = defWise_+addWise_，即游戏内 GetParam(-1)，显示封顶 99）
# 写入统一走成长数组 addParam_（升级重算基础值 defParam_ 时成长值保留，修改不丢失）
MATE_PARAM_FIELDS = (
    ("hpMax", "HP 上限"),
    ("mpMax", "MP 上限"),
    ("atk", "攻"),
    ("defense", "防"),
    ("spd", "速"),
    ("work", "劳"),
    ("wise", "智慧"),
)
# 界面分区：HP/MP 的「当前值」只读显示（恢复满=写当前值），「上限」写入成长数组（升级不还原）
MATE_STATUS_FIELDS = (("hpMax", "HP"), ("mpMax", "MP"))
MATE_COMBAT_FIELDS = (("atk", "攻"), ("defense", "防"), ("spd", "速"), ("work", "劳"), ("wise", "智慧"))
# 战斗中属性会乘技能百分比/属性相性（32 位乘法），超过约 1000 万会溢出导致伤害异常
MATE_PARAM_LIMIT = 9_990_000
MATE_RACIAL_MAX = 5  # 天赋星数上限（内部 0~4，显示 1~5 星）
MATE_LV_MAX = 99
MATE_EQUIP_MAX = 5  # 装备插槽上限（游戏常量 data.Enemy.EQUIP_MAX）
ITEM_NUM_MAX = 99  # 物品堆叠上限（num_ 为 SByte，游戏内实际堆叠上限 99）
# 设施参数（Building.GetParam 索引；0~3 为文明/自然/能量/回复量，依据各模型参数模式推断，
# 已实测写 param_ 覆盖数组后 GetParam 立即按覆盖值返回）
FACILITY_PARAM_FIELDS = (("culture", "文明"), ("nature", "自然"), ("energy", "能量"), ("recovery", "回复量"))
# 性格（data.Enemy.personality_，静态 personalityStr 顺序）
MATE_PERSONALITIES = ("平凡", "麻利", "勤劳", "顽固", "聪明", "易怒", "温顺")
# 特征（data.Enemy.attribute_，ATTRIBUTE_NONE..THUNDER）
MATE_ATTRIBUTES = ("无", "火", "水", "风", "土", "雷")

# 字段布局（实测 Beastie Bay DX，IL2CPP）：
# - main.AppData 为单例（静态字段 apdat_），player_ 指向 game.Player
# - Player.money_ 是内联 int32（不是对象引用），moneyBytes_ 为 byte[8] 小端镜像，两者需同步写
# - Player.researchPoint_ 是 Int32[3]：[0]食物 [1]资源 [2]奖牌
# - Player.researchPointBytes_ 是 byte[3][4] 小端镜像，同样需同步写
# - Player.coinHistory_ (FastVector<Int64[]>) 记录奖牌变动史，最新条目第 4 个元素
#   （elementData[0][3]）是当前余额，修改奖牌时同步它保持存档一致
# - 上限来自 Player.GetResearchPointMax(index)：实测 [0]食物 50、[1]资源 50、[2]奖牌 999，
#   随游戏内研究升级变化，需运行时实时读取
# - 同伴（玩家队伍/储备笼）是 data.Enemy 实例，挂在 Player.enemyBox_ / reserveCasket_
#   （java.util.Vector → list_ → C# List<Enemy>）
# - Enemy 属性显示值 = defParam_（基础）+ addParam_（成长）+ 种族星级加成；
#   所有属性统一差值写入 addParam_（成长）——升级时游戏调用 AdjustParam 按设计值重算
#   defParam_，但保留 addParam_，故写入成长值在升级后依然生效；当前值在 battleHP_/battleMP_
# - 智慧显示值 = defWise_ + addWise_（= 游戏内 GetParam(-1)，PARAM_WISE=-1），显示封顶 99；
#   defWise_/addWise_ 不受 AdjustParam 影响，直接写 defWise_ 基础即可，升级后依然有效
# - 装备插槽数 = equipMax_（int32，每同伴独立，游戏全局上限 EQUIP_MAX=5），与天赋星数无关；
#   已装备数量/空槽由 equip_ (Vector) 与 equipMax_ 派生（GetEquipEmptySlot = equipMax_ - 已装备数）
# - Enemy.racialLv_（0 基，显示 +1 星）、personality_（0~6）、attribute_（0~5）、Lv_（0~99）
IL2CPP_SCRIPT = r"""
const game = Process.getModuleByName('GameAssembly.dll');
const POINTER = Process.pointerSize;
function fn(name, ret, args) {
  return new NativeFunction(game.getExportByName(name), ret, args, 'mscdecl');
}
const api = {
  domainGet: fn('il2cpp_domain_get', 'pointer', []),
  domainAssemblies: fn('il2cpp_domain_get_assemblies', 'pointer', ['pointer', 'pointer']),
  assemblyImage: fn('il2cpp_assembly_get_image', 'pointer', ['pointer']),
  classFromName: fn('il2cpp_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']),
  classFields: fn('il2cpp_class_get_fields', 'pointer', ['pointer', 'pointer']),
  classParent: fn('il2cpp_class_get_parent', 'pointer', ['pointer']),
  fieldName: fn('il2cpp_field_get_name', 'pointer', ['pointer']),
  fieldOffset: fn('il2cpp_field_get_offset', 'uint', ['pointer']),
  fieldStaticGet: fn('il2cpp_field_static_get_value', 'void', ['pointer', 'pointer']),
  objectClass: fn('il2cpp_object_get_class', 'pointer', ['pointer']),
  arrayLength: fn('il2cpp_array_length', 'uint', ['pointer']),
  arrayNew: fn('il2cpp_array_new', 'pointer', ['pointer', 'uint']),
  threadAttach: fn('il2cpp_thread_attach', 'pointer', ['pointer']),
  threadDetach: fn('il2cpp_thread_detach', 'void', ['pointer']),
  classMethod: fn('il2cpp_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']),
  runtimeInvoke: fn('il2cpp_runtime_invoke', 'pointer', ['pointer', 'pointer', 'pointer', 'pointer'])
};
const domain = api.domainGet();
const classCache = new Map();
const fieldCache = new Map();
const offsetCache = new Map();
const methodCache = new Map();
let invokeCount = 0;
// 每个 RPC 导出调用前挂接、返回后断开 IL2CPP 线程。
// 只附加不断开会让已死的线程残留在 IL2CPP 线程列表里，游戏 GC 扫描死线程结构
// 导致随机闪退（0xc0000409），所以必须成对 attach/detach。
function withThread(handler) {
  return function (...args) {
    const thread = api.threadAttach(domain);
    invokeCount = 0;
    try {
      return handler.apply(this, args);
    } finally {
      api.threadDetach(thread);
    }
  };
}
function text(pointer) {
  return pointer.isNull() ? null : pointer.readUtf8String();
}
function findClass(namespace, name) {
  const key = namespace + '.' + name;
  if (classCache.has(key)) return classCache.get(key);
  const size = Memory.alloc(POINTER);
  const assemblies = api.domainAssemblies(domain, size);
  const namespacePointer = Memory.allocUtf8String(namespace);
  const namePointer = Memory.allocUtf8String(name);
  for (let i = 0; i < size.readU32(); i++) {
    const image = api.assemblyImage(assemblies.add(i * POINTER).readPointer());
    const klass = api.classFromName(image, namespacePointer, namePointer);
    if (!klass.isNull()) {
      classCache.set(key, klass);
      return klass;
    }
  }
  throw new Error('找不到类：' + key);
}
function getField(klass, name) {
  while (!klass.isNull()) {
    const key = klass.toString();
    let fields = fieldCache.get(key);
    if (fields === undefined) {
      fields = new Map();
      const iterator = Memory.alloc(POINTER);
      iterator.writePointer(NULL);
      for (;;) {
        const field = api.classFields(klass, iterator);
        if (field.isNull()) break;
        fields.set(text(api.fieldName(field)), field);
      }
      fieldCache.set(key, fields);
    }
    if (fields.has(name)) return fields.get(name);
    klass = api.classParent(klass);
  }
  throw new Error('找不到字段：' + name);
}
function offset(klass, name) {
  const key = klass.toString() + ':' + name;
  if (!offsetCache.has(key)) offsetCache.set(key, api.fieldOffset(getField(klass, name)));
  return offsetCache.get(key);
}
function getMethod(klass, name, argc) {
  const key = klass.toString() + ':' + name + ':' + argc;
  if (!methodCache.has(key)) {
    const method = api.classMethod(klass, Memory.allocUtf8String(name), argc);
    if (method.isNull()) throw new Error('找不到方法：' + name);
    methodCache.set(key, method);
  }
  return methodCache.get(key);
}
function managedString(object) {
  if (object.isNull()) return '';
  const length = object.add(8).readS32();
  if (length < 0 || length > 1048576) throw new Error('字符串长度异常，请重新读取');
  return length === 0 ? '' : object.add(12).readUtf16String(length);
}
const appClass = findClass('main', 'AppData');
const playerClass = findClass('game', 'Player');
const enemyClass = findClass('data', 'Enemy');
const appSingletonField = getField(appClass, 'apdat_');
const playerFieldOffset = offset(appClass, 'player_');
const moneyFieldOffset = offset(playerClass, 'money_');
const moneyBytesFieldOffset = offset(playerClass, 'moneyBytes_');
const researchFieldOffset = offset(playerClass, 'researchPoint_');
const researchBytesFieldOffset = offset(playerClass, 'researchPointBytes_');
const coinHistoryFieldOffset = offset(playerClass, 'coinHistory_');
function app() {
  const output = Memory.alloc(POINTER);
  output.writePointer(NULL);
  api.fieldStaticGet(appSingletonField, output);
  const current = output.readPointer();
  if (current.isNull()) throw new Error('游戏尚未初始化，请稍后刷新');
  return current;
}
function player() {
  const current = app().add(playerFieldOffset).readPointer();
  if (current.isNull()) throw new Error('尚未载入存档，请进入正常游戏界面后刷新');
  return current;
}
function readByteArray(array, maxLength) {
  let value = 0;
  const length = Math.min(api.arrayLength(array), maxLength);
  for (let i = length - 1; i >= 0; i--) value = value * 256 + array.add(16 + i).readU8();
  return value;
}
function writeByteArray(array, value, maxLength) {
  let rest = value;
  const length = Math.min(api.arrayLength(array), maxLength);
  for (let i = 0; i < length; i++) {
    array.add(16 + i).writeU8(rest % 256);
    rest = Math.floor(rest / 256);
  }
}
function researchArray(currentPlayer) {
  const array = currentPlayer.add(researchFieldOffset).readPointer();
  if (array.isNull() || api.arrayLength(array) < 3) throw new Error('资源数组尚未初始化');
  return array;
}
function researchBytesArray(currentPlayer) {
  const array = currentPlayer.add(researchBytesFieldOffset).readPointer();
  if (array.isNull() || api.arrayLength(array) < 3) throw new Error('资源镜像数组尚未初始化');
  return array;
}
function invokeInt1(klass, object, name, intArg) {
  const method = getMethod(klass, name, 1);
  const argMem = Memory.alloc(4);
  argMem.writeS32(intArg);
  const args = Memory.alloc(POINTER);
  args.writePointer(argMem);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  if (invokeCount >= 3) throw new Error('单次 RPC 调用次数已达上限，请分批读取');
  invokeCount++;
  const result = api.runtimeInvoke(method, object, args, exception);
  if (!exception.readPointer().isNull() || result.isNull()) throw new Error(name + ' 调用失败');
  return result.add(8).readS32();
}
function researchMax(currentPlayer, index) {
  try {
    return invokeInt1(playerClass, currentPlayer, 'GetResearchPointMax', index);
  } catch (e) {
    return null;
  }
}
function readState() {
  const currentPlayer = player();
  const research = researchArray(currentPlayer);
  return {
    money: currentPlayer.add(moneyFieldOffset).readS32(),
    medals: research.add(16 + 2 * 4).readS32(),
    food: research.add(16).readS32(),
    wood: research.add(16 + 4).readS32(),
    foodMax: researchMax(currentPlayer, 0),
    woodMax: researchMax(currentPlayer, 1),
    medalsMax: researchMax(currentPlayer, 2)
  };
}
function writeMoney(currentPlayer, value) {
  currentPlayer.add(moneyFieldOffset).writeS32(value);
  const mirror = currentPlayer.add(moneyBytesFieldOffset).readPointer();
  if (!mirror.isNull()) writeByteArray(mirror, value, 8);
  if (currentPlayer.add(moneyFieldOffset).readS32() !== value) throw new Error('金钱写入后校验失败');
}
function writeResearch(currentPlayer, index, value) {
  const research = researchArray(currentPlayer);
  research.add(16 + index * 4).writeS32(value);
  const mirror = researchBytesArray(currentPlayer).add(16 + index * POINTER).readPointer();
  if (!mirror.isNull()) writeByteArray(mirror, value, 8);
  if (research.add(16 + index * 4).readS32() !== value) throw new Error('资源写入后校验失败');
}
function writeMedalBalance(currentPlayer, value) {
  const history = currentPlayer.add(coinHistoryFieldOffset).readPointer();
  if (history.isNull()) return;
  const elementData = history.add(offset(api.objectClass(history), 'elementData')).readPointer();
  if (elementData.isNull() || api.arrayLength(elementData) < 1) return;
  const latest = elementData.add(16).readPointer();
  if (latest.isNull() || api.arrayLength(latest) < 4) return;
  latest.add(16 + 3 * 8).writeS64(new Int64(value));
}
function applyChanges(changes) {
  const currentPlayer = player();
  const values = [changes.food, changes.wood, changes.medals];
  writeMoney(currentPlayer, changes.money);
  for (let i = 0; i < 3; i++) writeResearch(currentPlayer, i, values[i]);
  writeMedalBalance(currentPlayer, changes.medals);
  return readState();
}
function applyLockValues(changes) {
  const currentPlayer = player();
  const indexByName = {food: 0, wood: 1, medals: 2};
  const medalTarget = changes.medals !== undefined ? changes.medals : null;
  for (const key in changes) {
    const index = indexByName[key];
    if (index === undefined) continue;
    writeResearch(currentPlayer, index, changes[key]);
  }
  if (medalTarget !== null) writeMedalBalance(currentPlayer, medalTarget);
  return readState();
}
// ---- 同伴（data.Enemy 实例）----
const enemyNameOffset = offset(enemyClass, 'name_');
const enemyLvOffset = offset(enemyClass, 'Lv_');
const enemyRacialLvOffset = offset(enemyClass, 'racialLv_');
const enemyPersonalityOffset = offset(enemyClass, 'personality_');
const enemyAttributeOffset = offset(enemyClass, 'attribute_');
const enemyBattleHPOffset = offset(enemyClass, 'battleHP_');
const enemyBattleMPOffset = offset(enemyClass, 'battleMP_');
const enemyExpOffset = offset(enemyClass, 'exp_');
const enemyEquipMaxOffset = offset(enemyClass, 'equipMax_');
const enemyDefParamOffset = offset(enemyClass, 'defParam_');
const enemyAddParamOffset = offset(enemyClass, 'addParam_');
const enemyDefWiseOffset = offset(enemyClass, 'defWise_');
const enemyAddWiseOffset = offset(enemyClass, 'addWise_');
function listStorage(listObject) {
  if (listObject.isNull()) throw new Error('列表尚未初始化，请重新读取');
  const klass = api.objectClass(listObject);
  const items = listObject.add(offset(klass, '_items')).readPointer();
  const size = listObject.add(offset(klass, '_size')).readS32();
  if (size < 0 || (items.isNull() ? size !== 0 : size > api.arrayLength(items))) {
    throw new Error('列表长度异常，请重新读取');
  }
  return {items: items, size: size};
}
function listItems(listObject) {
  const storage = listStorage(listObject);
  const result = [];
  for (let i = 0; i < storage.size; i++) result.push(storage.items.add(16 + i * POINTER).readPointer());
  return result;
}
function vectorList(vector) {
  if (vector.isNull()) throw new Error('容器尚未初始化，请重新读取');
  return vector.add(offset(api.objectClass(vector), 'list_')).readPointer();
}
function vectorItems(vector) {
  return listItems(vectorList(vector));
}
const mateBoxOffsets = [offset(playerClass, 'enemyBox_'), offset(playerClass, 'reserveCasket_')];
// box=0 -> enemyBox_（出战队伍）, box=1 -> reserveCasket_（储备笼）
function mateContainer(box) {
  if (!Number.isInteger(box) || box < 0 || box >= mateBoxOffsets.length) throw new Error('同伴容器无效，请刷新后重试');
  return player().add(mateBoxOffsets[box]).readPointer();
}
function mateList(box) {
  const container = mateContainer(box);
  return container.isNull() ? [] : vectorItems(container);
}
function invokeIntArg1(klass, object, name, intArg) {
  return invokeInt1(klass, object, name, intArg);
}
function readMateCore(enemy, box, index) {
  // 纯内存读取基础字段（不 invoke）；智慧显示值 = defWise_ + addWise_（= GetParam(-1)）
  return {
    box: box,
    index: index,
    name: managedString(enemy.add(enemyNameOffset).readPointer()),
    lv: enemy.add(enemyLvOffset).readS32(),
    racialLv: enemy.add(enemyRacialLvOffset).readS32(),
    personality: enemy.add(enemyPersonalityOffset).readS32(),
    attribute: enemy.add(enemyAttributeOffset).readS32(),
    battleHP: enemy.add(enemyBattleHPOffset).readS32(),
    battleMP: enemy.add(enemyBattleMPOffset).readS32(),
    wise: enemy.add(enemyDefWiseOffset).readS32() + enemy.add(enemyAddWiseOffset).readS32(),
    equipMax: enemy.add(enemyEquipMaxOffset).readS32()
  };
}
function readMateParams(enemy, box, index) {
  // 六项 GetParam 分成两批 RPC，每批最多三次；智慧随基础字段直接读取。
  const result = readMateCore(enemy, box, index);
  result.hpMax = invokeIntArg1(enemyClass, enemy, 'GetParam', 0);
  result.mpMax = invokeIntArg1(enemyClass, enemy, 'GetParam', 2);
  result.atk = invokeIntArg1(enemyClass, enemy, 'GetParam', 4);
  return result;
}
function readMateParams2(enemy, box, index) {
  const result = {box: box, index: index};
  result.defense = invokeIntArg1(enemyClass, enemy, 'GetParam', 6);
  result.spd = invokeIntArg1(enemyClass, enemy, 'GetParam', 8);
  result.work = invokeIntArg1(enemyClass, enemy, 'GetParam', 10);
  return result;
}
function isEnemyObject(object) {
  if (object.isNull()) return false;
  return api.objectClass(object).equals(enemyClass);
}
function readMates() {
  const result = [];
  const box0 = mateList(0);
  for (let i = 0; i < box0.length; i++) {
    if (isEnemyObject(box0[i])) result.push(readMateCore(box0[i], 0, i));
  }
  // 储备笼（reserveCasket_）条目可能是 BoxSave 序列化的 Object[]，不是 Enemy 实例，
  // 跳过非 Enemy 条目，仅读取可编辑的 Enemy 实例。
  const box1 = mateList(1);
  for (let i = 0; i < box1.length; i++) {
    if (isEnemyObject(box1[i])) result.push(readMateCore(box1[i], 1, i));
  }
  return result;
}
// 供 Python 分批拉取属性（每批 invoke ≤ 3 次，规避 RPC 连续 invoke 限制）
function readMatesParams(box, index) {
  const enemy = findMate(box, index);
  return readMateParams(enemy, box, index);
}
function readMatesParams2(box, index) {
  const enemy = findMate(box, index);
  return readMateParams2(enemy, box, index);
}
function findMate(box, index) {
  const container = mateContainer(box);
  if (container.isNull()) throw new Error('同伴列表已变化，请刷新后重试');
  const storage = listStorage(vectorList(container));
  if (!Number.isInteger(index) || index < 0 || index >= storage.size) throw new Error('同伴索引无效，请刷新后重试');
  const enemy = storage.items.add(16 + index * POINTER).readPointer();
  if (!isEnemyObject(enemy)) throw new Error('目标不是可编辑同伴，请刷新后重试');
  return enemy;
}
function applyMateChanges(changes) {
  const enemy = findMate(changes.box, changes.index);
  // 等级
  if (changes.lv !== undefined && changes.lv !== null) {
    enemy.add(enemyLvOffset).writeS32(changes.lv);
  }
  // 天赋星数（内部 0 基）
  if (changes.racialLv !== undefined && changes.racialLv !== null) {
    enemy.add(enemyRacialLvOffset).writeS32(changes.racialLv);
  }
  // 性格 / 特征
  if (changes.personality !== undefined && changes.personality !== null) {
    enemy.add(enemyPersonalityOffset).writeS32(changes.personality);
  }
  if (changes.attribute !== undefined && changes.attribute !== null) {
    enemy.add(enemyAttributeOffset).writeS32(changes.attribute);
  }
  // 装备插槽数（独立字段，与天赋无关；游戏全局上限 EQUIP_MAX = 5）
  if (changes.equipMax !== undefined && changes.equipMax !== null) {
    enemy.add(enemyEquipMaxOffset).writeS32(changes.equipMax);
  }
  return readMateCore(enemy, changes.box, changes.index);
}
// 单属性写入：box/index/paramKey（atk|defense|spd|work|hpMax|mpMax|wise）/value
// 每次 RPC 只处理一项，普通属性 invoke 1 次，智慧不 invoke
function applyMateParam(box, index, paramKey, value) {
  const enemy = findMate(box, index);
  if (paramKey === 'wise') {
    // 智慧显示值 = defWise_ + addWise_（游戏内 GetParam(-1)，PARAM_WISE=-1），显示封顶 99。
    // defWise_ 不受 AdjustParam（升级重算）影响，直接写基础、清成长即可，升级后依然有效。
    if (value > 99) value = 99;
    enemy.add(enemyAddWiseOffset).writeS32(0);
    enemy.add(enemyDefWiseOffset).writeS32(value);
    return readMateCore(enemy, box, index);
  }
  const defParam = enemy.add(enemyDefParamOffset).readPointer();
  const addParam = enemy.add(enemyAddParamOffset).readPointer();
  if (defParam.isNull() || addParam.isNull()) throw new Error('属性数组尚未初始化');
  const indexByKey = {hpMax: 0, mpMax: 2, atk: 4, defense: 6, spd: 8, work: 10};
  const idx = indexByKey[paramKey];
  if (idx === undefined) throw new Error('未知属性：' + paramKey);
  if (idx >= api.arrayLength(defParam) || idx >= api.arrayLength(addParam)) throw new Error('属性数组长度不足，请重新读取');
  // 统一差值算法：一律写 addParam_（成长），不动 defParam_（基础）。
  // 升级/捕捉时游戏调用 AdjustParam 会按程序设计重算 defParam_，但保留 addParam_，
  // 因此写成长值在升级后依然生效（仅随自然成长略高于目标值，绝不会掉回程序设定值）。
  const now = invokeIntArg1(enemyClass, enemy, 'GetParam', idx);
  const growth = addParam.add(16 + idx * 4).readS32();
  const delta = value - now + growth;
  // 差值为负说明目标低于「基础 + 种族加成」下限，降到下限为止（基础值本身不砍，避免被重算覆盖）
  addParam.add(16 + idx * 4).writeS32(Math.max(0, delta));
  return readMateCore(enemy, box, index);
}
function healMate(box, index) {
  const enemy = findMate(box, index);
  const hpMax = invokeIntArg1(enemyClass, enemy, 'GetParam', 0);
  const mpMax = invokeIntArg1(enemyClass, enemy, 'GetParam', 2);
  enemy.add(enemyBattleHPOffset).writeS32(hpMax);
  enemy.add(enemyBattleMPOffset).writeS32(mpMax);
  return readMateCore(enemy, box, index);
}
// ---- 物品栏（AppData 三张定义表，持有数/强化等级存在表条目上，纯内存读写）----
const consClass = findClass('data', 'Consumables');
const survClass = findClass('data', 'Survival');
const itemEquipClass = findClass('data', 'Equip');
const appConsOffset = offset(appClass, 'consList_');
const appSurvOffset = offset(appClass, 'survivalList_');
const appEquipOffset = offset(appClass, 'equipList_');
const consNumOffset = offset(consClass, 'num_');
const consNameOffset = offset(consClass, 'name_');
const consLimitOffset = offset(consClass, 'limit_');
const survNameOffset = offset(survClass, 'name_');
const survLvOffset = offset(survClass, 'lv_');
const survLifeOffset = offset(survClass, 'life_');
const survBreakOffset = offset(survClass, 'break_');
const equipNumOffset = offset(itemEquipClass, 'num_');
const equipNameOffset = offset(itemEquipClass, 'name_');
const equipTypeOffset = offset(itemEquipClass, 'type_');
const equipStrOffset = offset(itemEquipClass, 'strengthen_');
const equipCostOffset = offset(itemEquipClass, 'strengthenCost');
function tableEntry(tableOffset, index) {
  const arr = app().add(tableOffset).readPointer();
  if (arr.isNull() || !Number.isInteger(index) || index < 0 || index >= api.arrayLength(arr)) throw new Error('物品索引越界，请刷新物品');
  const entry = arr.add(16 + index * POINTER).readPointer();
  if (entry.isNull()) throw new Error('物品条目为空，请刷新物品');
  return entry;
}
function readItems() {
  const appData = app();
  const result = {cons: [], surv: [], equip: []};
  let arr = appData.add(appConsOffset).readPointer();
  if (!arr.isNull()) {
    const n = api.arrayLength(arr);
    for (let i = 0; i < n; i++) {
      const e = arr.add(16 + i * POINTER).readPointer();
      if (e.isNull()) continue;
      const num = e.add(consNumOffset).readS8();
      if (num <= 0) continue;
      result.cons.push({
        idx: i,
        name: managedString(e.add(consNameOffset).readPointer()),
        num: num,
        limit: e.add(consLimitOffset).readS8()
      });
    }
  }
  arr = appData.add(appSurvOffset).readPointer();
  if (!arr.isNull()) {
    const n = api.arrayLength(arr);
    for (let i = 0; i < n; i++) {
      const e = arr.add(16 + i * POINTER).readPointer();
      if (e.isNull()) continue;
      result.surv.push({
        idx: i,
        name: managedString(e.add(survNameOffset).readPointer()),
        lv: e.add(survLvOffset).readS32(),
        life: e.add(survLifeOffset).readS32(),
        broken: e.add(survBreakOffset).readS8()
      });
    }
  }
  arr = appData.add(appEquipOffset).readPointer();
  if (!arr.isNull()) {
    const n = api.arrayLength(arr);
    for (let i = 0; i < n; i++) {
      const e = arr.add(16 + i * POINTER).readPointer();
      if (e.isNull()) continue;
      const num = e.add(equipNumOffset).readS8();
      if (num <= 0) continue;
      const cost = e.add(equipCostOffset).readPointer();
      result.equip.push({
        idx: i,
        name: managedString(e.add(equipNameOffset).readPointer()),
        num: num,
        type: e.add(equipTypeOffset).readS32(),
        strengthen: e.add(equipStrOffset).readS8(),
        maxStrengthen: cost.isNull() ? 0 : api.arrayLength(cost)
      });
    }
  }
  return result;
}
// kind: cons|equip；value 写入 num_（SByte）。游戏堆叠上限 99（SByte 硬顶 127）
function applyItemNum(kind, idx, value) {
  if (!Number.isInteger(value) || value < 0 || value > 99) throw new Error('数量必须是 0 到 99 的整数');
  if (kind === 'cons') {
    const e = tableEntry(appConsOffset, idx);
    e.add(consNumOffset).writeS8(value);
    return {kind: kind, idx: idx, num: e.add(consNumOffset).readS8()};
  }
  if (kind === 'equip') {
    const e = tableEntry(appEquipOffset, idx);
    e.add(equipNumOffset).writeS8(value);
    return {kind: kind, idx: idx, num: e.add(equipNumOffset).readS8()};
  }
  throw new Error('该类别不支持修改数量：' + kind);
}
// 装备强化：硬上限 = strengthenCost 数组长度（游戏限制的升级次数），绝不溢出
function upgradeEquip(idx, targetLv) {
  const e = tableEntry(appEquipOffset, idx);
  const cost = e.add(equipCostOffset).readPointer();
  const maxLv = cost.isNull() ? 0 : api.arrayLength(cost);
  if (!Number.isInteger(targetLv) || targetLv < 0) throw new Error('目标强化等级必须是 ≥0 的整数');
  if (targetLv > maxLv) throw new Error('强化次数已达游戏上限 ' + maxLv + ' 级，溢出会被游戏拒绝');
  e.add(equipStrOffset).writeS8(targetLv);
  return {kind: 'equip', idx: idx, strengthen: e.add(equipStrOffset).readS8(), maxStrengthen: maxLv};
}
// 批量：数量写入，逐条执行并汇报（单 RPC，纯内存写）
function applyItemNumBatch(kind, indices, value) {
  if (!Number.isInteger(value) || value < 0 || value > 99) throw new Error('数量必须是 0 到 99 的整数');
  const ok = [], fail = [];
  for (const idx of indices) {
    try {
      if (kind === 'cons') {
        const e = tableEntry(appConsOffset, idx);
        e.add(consNumOffset).writeS8(value);
        ok.push({idx: idx, num: e.add(consNumOffset).readS8()});
      } else if (kind === 'equip') {
        const e = tableEntry(appEquipOffset, idx);
        e.add(equipNumOffset).writeS8(value);
        ok.push({idx: idx, num: e.add(equipNumOffset).readS8()});
      } else {
        throw new Error('未知类别：' + kind);
      }
    } catch (err) {
      fail.push({idx: idx, error: String(err)});
    }
  }
  return {ok: ok, fail: fail};
}
// 批量：装备强化到统一目标等级，超过各自上限的单独记为失败，不影响其他
function upgradeEquipBatch(indices, targetLv) {
  if (!Number.isInteger(targetLv) || targetLv < 0) throw new Error('目标强化等级必须是 ≥0 的整数');
  const ok = [], fail = [];
  for (const idx of indices) {
    try {
      const e = tableEntry(appEquipOffset, idx);
      const cost = e.add(equipCostOffset).readPointer();
      const maxLv = cost.isNull() ? 0 : api.arrayLength(cost);
      if (targetLv > maxLv) throw new Error('超过该装备强化上限 ' + maxLv + ' 级');
      e.add(equipStrOffset).writeS8(targetLv);
      ok.push({idx: idx, strengthen: e.add(equipStrOffset).readS8(), maxStrengthen: maxLv});
    } catch (err) {
      fail.push({idx: idx, error: String(err)});
    }
  }
  return {ok: ok, fail: fail};
}
// ---- 设施属性（按坐标扫描/定位：Player.map_[岛][行y][列x].building_；写 param_ 差值数组，GetParam 叠加模型基础值）----
const facilityBuildingClass = findClass('game', 'Building');
const mapChipClass = findClass('game', 'MapChip');
const mapClass = findClass('game', 'Map');
const chipModelClass = findClass('data', 'ChipModel');
const playerMapOffset = offset(playerClass, 'map_');
const fbModelOffset = offset(facilityBuildingClass, 'modelID_');
const fbRebuildOffset = offset(facilityBuildingClass, 'rebuild_');
const fbBuildOffset = offset(facilityBuildingClass, 'build_');
const fbParamOffset = offset(facilityBuildingClass, 'param_');
const cmNameOffset = offset(chipModelClass, 'name_');
const cmMenuOffset = offset(chipModelClass, 'menuTypeID_');
const appChipModelOffset = offset(appClass, 'chipModel_');
const islandClass = findClass('data', 'Island');
const appIslandListOffset = offset(appClass, 'islandList_');
const islandNameOffset = offset(islandClass, 'name_');
const playerIslandNameOffset = offset(playerClass, 'islandName_');
const int16Class = findClass('System', 'Int16');
// 设施名查表：Building.modelID_ → AppData.chipModel_[id].name_（纯内存，免 invoke；
// 地形与设施同表：menuTypeID_ 2=地形 0/1=设施与道路）
function chipModelEntry(id) {
  const arr = app().add(appChipModelOffset).readPointer();
  if (arr.isNull() || id < 0 || id >= api.arrayLength(arr)) return null;
  return arr.add(16 + id * POINTER).readPointer();
}
function facilityName(id) {
  const entry = chipModelEntry(id);
  if (entry === null || entry.isNull()) return '#' + id;
  const value = managedString(entry.add(cmNameOffset).readPointer()).replace(/[\x00-\x1f]/g, '').trim();
  return value === '' ? '#' + id : value;
}
// param_ 是差值数组：GetParam(i) = (i < len ? param_[i] : 0) + 模型基础值（实测验证）
function readParamRaw(building) {
  const arr = building.add(fbParamOffset).readPointer();
  if (arr.isNull()) return [];
  const n = api.arrayLength(arr);
  const r = [];
  for (let i = 0; i < n; i++) r.push(arr.add(16 + i * 2).readS16());
  return r;
}
// 岛显示名：map_[0] 是母岛，名字在 Player.islandName_（玩家可改，默认「一座岛」）；
// 其余岛按 Map.id_ 查 AppData.islandList_（data.Island.name_，如 常夏岛/龟岛…）
function mapDisplayName(mapIndex, map) {
  if (mapIndex === 0) {
    const home = player().add(playerIslandNameOffset).readPointer();
    const homeName = home.isNull() ? '' : managedString(home).replace(/[\x00-\x1f]/g, '').trim();
    if (homeName !== '') return homeName;
  }
  const islands = app().add(appIslandListOffset).readPointer();
  if (islands.isNull()) return '';
  const id = map.add(offset(mapClass, 'id_')).readS32();
  if (id < 0 || id >= api.arrayLength(islands)) return '';
  const entry = islands.add(16 + id * POINTER).readPointer();
  if (entry.isNull()) return '';
  return managedString(entry.add(islandNameOffset).readPointer()).replace(/[\x00-\x1f]/g, '').trim();
}
// 按岛序号+坐标定位设施对象：map_ 数组下标即岛（0 基，一座岛=0），mapchips_[y][x].building_ 非空即为设施/地块
function facilityAt(mapIndex, x, y) {
  const maps = player().add(playerMapOffset).readPointer();
  if (maps.isNull()) throw new Error('地图尚未初始化');
  if (!Number.isInteger(mapIndex) || mapIndex < 0 || mapIndex >= api.arrayLength(maps)) throw new Error('岛序号无效：' + mapIndex);
  const map = maps.add(16 + mapIndex * POINTER).readPointer();
  if (map.isNull()) throw new Error('岛 ' + (mapIndex + 1) + ' 尚未开放');
  const grid = map.add(offset(mapClass, 'mapchips_')).readPointer();
  if (grid.isNull() || !Number.isInteger(y) || y < 0 || y >= api.arrayLength(grid)) throw new Error('Y 坐标越界：' + y);
  const row = grid.add(16 + y * POINTER).readPointer();
  if (row.isNull() || !Number.isInteger(x) || x < 0 || x >= api.arrayLength(row)) throw new Error('X 坐标越界：' + x);
  const chip = row.add(16 + x * POINTER).readPointer();
  if (chip.isNull()) throw new Error('目标格（' + x + ',' + y + '）不存在');
  const building = chip.add(offset(mapChipClass, 'building_')).readPointer();
  if (building.isNull()) throw new Error('该格没有设施或地块对象');
  return {map: mapIndex, x: x, y: y, building: building, mapObject: map};
}
function facilitySnapshot(loc) {
  return {
    map: loc.map, x: loc.x, y: loc.y,
    modelID: loc.building.add(fbModelOffset).readS32(),
    rebuild: loc.building.add(fbRebuildOffset).readS8(),
    build: loc.building.add(fbBuildOffset).readS8(),
    praw: readParamRaw(loc.building)
  };
}
function checkFacility(snapshot, expected) {
  if (!expected || ['map', 'x', 'y', 'modelID', 'rebuild'].some(key => snapshot[key] !== expected[key]) ||
      !Array.isArray(expected.praw) || snapshot.praw.length !== expected.praw.length ||
      snapshot.praw.some((value, index) => value !== expected.praw[index])) {
    throw new Error('设施位置或属性已变化，请重新读取设施后保存');
  }
}
// 全岛扫描（纯内存，不 invoke）：列出所有带 building_ 的格，名称走 chipModel_ 表
function scanFacilities() {
  const maps = player().add(playerMapOffset).readPointer();
  if (maps.isNull()) throw new Error('地图尚未初始化');
  const result = [];
  const mapCount = api.arrayLength(maps);
  for (let m = 0; m < mapCount; m++) {
    const map = maps.add(16 + m * POINTER).readPointer();
    if (map.isNull()) continue;
    const mapName = mapDisplayName(m, map);
    const grid = map.add(offset(mapClass, 'mapchips_')).readPointer();
    if (grid.isNull()) continue;
    const rows = api.arrayLength(grid);
    for (let y = 0; y < rows; y++) {
      const row = grid.add(16 + y * POINTER).readPointer();
      if (row.isNull()) continue;
      const cols = api.arrayLength(row);
      for (let x = 0; x < cols; x++) {
        const chip = row.add(16 + x * POINTER).readPointer();
        if (chip.isNull()) continue;
        const building = chip.add(offset(mapChipClass, 'building_')).readPointer();
        if (building.isNull()) continue;
        const modelID = building.add(fbModelOffset).readS32();
        const entry = chipModelEntry(modelID);
        result.push({
          map: m, mapName: mapName, x: x, y: y, modelID: modelID, name: facilityName(modelID),
          menu: entry === null || entry.isNull() ? -1 : entry.add(cmMenuOffset).readS8(),
          rebuild: building.add(fbRebuildOffset).readS8(),
          build: building.add(fbBuildOffset).readS8()
        });
      }
    }
  }
  return result;
}
function readFacilityAt(mapIndex, x, y) {
  const loc = facilityAt(mapIndex, x, y);
  const result = facilitySnapshot(loc);
  result.mapName = mapDisplayName(mapIndex, loc.mapObject);
  result.p0 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 0);
  result.p1 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 1);
  result.p2 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 2);
  return result;
}
function readFacilityParamsAt2(mapIndex, x, y, expected) {
  const loc = facilityAt(mapIndex, x, y);
  const result = facilitySnapshot(loc);
  checkFacility(result, expected);
  result.p3 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 3);
  result.p4 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 4);
  result.p5 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 5);
  return result;
}
function readFacilityParamsAt3(mapIndex, x, y, expected) {
  const loc = facilityAt(mapIndex, x, y);
  const result = facilitySnapshot(loc);
  checkFacility(result, expected);
  result.p6 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 6);
  result.p7 = invokeIntArg1(facilityBuildingClass, loc.building, 'GetParam', 7);
  return result;
}
// 保存：写差值数组 param_[0..3] = 目标显示值 - 基础值（基础值 = 当前显示 - 当前差值，由 Python 换算后传入）
function applyFacilityParamsAt(mapIndex, x, y, newP, expected) {
  if (!Array.isArray(newP) || newP.length !== 4) throw new Error('参数必须是 4 项差值数组');
  for (const v of newP) {
    if (!Number.isInteger(v) || v < -32768 || v > 32767) throw new Error('差值超出 Int16 范围（-32768~32767）');
  }
  const loc = facilityAt(mapIndex, x, y);
  checkFacility(facilitySnapshot(loc), expected);
  const b = loc.building;
  let arr = b.add(fbParamOffset).readPointer();
  if (arr.isNull() || api.arrayLength(arr) < 4) {
    const old = readParamRaw(b);
    const fresh = api.arrayNew(int16Class, 4);
    if (fresh.isNull()) throw new Error('设施参数数组分配失败');
    for (let i = 0; i < 4; i++) fresh.add(16 + i * 2).writeS16(i < old.length ? old[i] : 0);
    b.add(fbParamOffset).writePointer(fresh);
    arr = fresh;
  }
  for (let i = 0; i < 4; i++) arr.add(16 + i * 2).writeS16(newP[i]);
  return {
    map: loc.map, x: loc.x, y: loc.y,
    params: [
      invokeIntArg1(facilityBuildingClass, b, 'GetParam', 0),
      invokeIntArg1(facilityBuildingClass, b, 'GetParam', 1),
      invokeIntArg1(facilityBuildingClass, b, 'GetParam', 2)
    ]
  };
}
// RPC 回调线程连续 runtimeInvoke 可能触发 system error，
// 因此拆分导出：每次 RPC 最多调用 3 次，由 invokeInt1 统一限制。
// 所有导出统一经 withThread 成对挂接/断开 IL2CPP 线程，避免死线程残留导致 GC 闪退。
rpc.exports = {
  readState: withThread(readState),
  applyChanges: withThread(applyChanges),
  applyLockValues: withThread(applyLockValues),
  readMates: withThread(readMates),
  readMatesParams: withThread(readMatesParams),
  readMatesParams2: withThread(readMatesParams2),
  applyMateChanges: withThread(applyMateChanges),
  applyMateParam: withThread(applyMateParam),
  healMate: withThread(healMate),
  readItems: withThread(readItems),
  applyItemNum: withThread(applyItemNum),
  upgradeEquip: withThread(upgradeEquip),
  applyItemNumBatch: withThread(applyItemNumBatch),
  upgradeEquipBatch: withThread(upgradeEquipBatch),
  scanFacilities: withThread(scanFacilities),
  readFacilityAt: withThread(readFacilityAt),
  readFacilityParamsAt2: withThread(readFacilityParamsAt2),
  readFacilityParamsAt3: withThread(readFacilityParamsAt3),
  applyFacilityParamsAt: withThread(applyFacilityParamsAt)
};
"""


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        self.pending = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<FocusIn>", self.show, add="+")
        widget.bind("<FocusOut>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def show(self, _event=None):
        if self.window is not None or self.pending is not None:
            return
        try:
            self.pending = self.widget.after(450, self._show_now)
        except tk.TclError:
            self.pending = None

    def _show_now(self):
        self.pending = None
        try:
            if not self.widget.winfo_exists() or not self.widget.winfo_viewable():
                return
            left = self.widget.winfo_vrootx()
            top = self.widget.winfo_vrooty()
            screen_width = self.widget.winfo_vrootwidth()
            screen_height = self.widget.winfo_vrootheight()
            self.window = tk.Toplevel(self.widget)
            self.window.withdraw()
            self.window.wm_overrideredirect(True)
            self.window.wm_attributes("-topmost", True)
            tk.Label(
                self.window,
                text=self.text,
                justify="left",
                wraplength=min(420, max(80, screen_width - 40)),
                background="#fff8dc",
                relief="solid",
                borderwidth=1,
                padx=9,
                pady=7,
            ).pack()
            self.window.update_idletasks()
            width = min(self.window.winfo_reqwidth(), screen_width)
            height = min(self.window.winfo_reqheight(), screen_height)
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            if y + height > top + screen_height:
                y = self.widget.winfo_rooty() - height - 6
            x = max(left, min(x, left + screen_width - width))
            y = max(top, min(y, top + screen_height - height))
            self.window.wm_geometry(f"{width}x{height}{x:+d}{y:+d}")
            self.window.deiconify()
        except tk.TclError:
            self.hide()

    def hide(self, _event=None):
        if self.pending is not None:
            try:
                self.widget.after_cancel(self.pending)
            except tk.TclError:
                pass
            self.pending = None
        window, self.window = self.window, None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass


class IL2CPPGame:
    def __init__(self):
        self.session = None
        self.script = None
        self.pid = None
        self.backup_created = False
        self.backup_root = BACKUP_ROOT
        self.last_backup_path = None

    def connect(self):
        self.close()
        device = frida.get_local_device()
        process = next((p for p in device.enumerate_processes() if p.name.lower() == GAME_NAME.lower()), None)
        if process is None:
            raise RuntimeError("未找到 KairoGames.exe，请先启动游戏并载入存档")
        self.session = device.attach(process.pid)
        try:
            self.script = self.session.create_script(IL2CPP_SCRIPT)
            self.script.load()
            self.pid = process.pid
        except Exception:
            self.close()
            raise

    def read_state(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.read_state()

    def apply_changes(self, changes):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
        result = self.script.exports_sync.apply_changes(changes)
        self.backup_created = True
        return result

    def apply_lock_values(self, changes):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True
        return self.script.exports_sync.apply_lock_values(changes)

    def read_mates(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.read_mates()

    def read_mate_details(self, box, index):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        result = self.script.exports_sync.read_mates_params(box, index)
        extra = self.script.exports_sync.read_mates_params2(box, index)
        if any(part.get("box") != box or part.get("index") != index for part in (result, extra)):
            raise RuntimeError("同伴列表已变化，请刷新后重试")
        result.update(extra)
        return result

    def apply_mate_changes(self, changes):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True
        box, index = changes["box"], changes["index"]
        # 元数据（等级/天赋/装备插槽/性格/特征）一次 RPC，无 invoke
        self.script.exports_sync.apply_mate_changes({
            key: value for key, value in changes.items()
            if key in ("box", "index", "lv", "racialLv", "equipMax", "personality", "attribute")
        })
        # 属性逐项写入（每项一次 RPC，普通属性 invoke 1 次，智慧不 invoke）
        for key in ("hpMax", "mpMax", "atk", "defense", "spd", "work", "wise"):
            if changes.get(key) is not None:
                self.script.exports_sync.apply_mate_param(box, index, key, changes[key])
        return self.read_mate_details(box, index)

    def heal_mate(self, box, index):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True
        self.script.exports_sync.heal_mate(box, index)
        return self.read_mate_details(box, index)

    def read_items(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.read_items()

    def apply_item_num(self, kind, idx, value):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        self.ensure_backup()
        return self.script.exports_sync.apply_item_num(kind, idx, value)

    def upgrade_equip(self, idx, target_lv):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        self.ensure_backup()
        return self.script.exports_sync.upgrade_equip(idx, target_lv)

    def apply_item_num_batch(self, kind, indices, value):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        self.ensure_backup()
        return self.script.exports_sync.apply_item_num_batch(kind, list(indices), value)

    def upgrade_equip_batch(self, indices, target_lv):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        self.ensure_backup()
        return self.script.exports_sync.upgrade_equip_batch(list(indices), target_lv)

    def scan_facilities(self):
        """全岛扫描设施/地块：纯内存走查 map_ × mapchips_，名称走 chipModel_ 表（免 invoke）"""
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.scan_facilities()

    def read_facility(self, map_index, x, y):
        """按岛序号+坐标读取设施：定位 + GetParam(0..7) 八项 + param_ 原始差值"""
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        self._facility_last = None
        result = self.script.exports_sync.read_facility_at(map_index, x, y)
        snapshot = {key: result[key] for key in ("map", "x", "y", "modelID", "rebuild", "praw")}
        for read_batch in (self.script.exports_sync.read_facility_params_at2, self.script.exports_sync.read_facility_params_at3):
            extra = read_batch(map_index, x, y, snapshot)
            if any(extra.get(key) != value for key, value in snapshot.items()):
                raise RuntimeError("读取期间设施位置或属性已变化，请重新读取")
            result.update(extra)
        for key in ("p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7"):
            if not isinstance(result.get(key), int):
                raise RuntimeError(f"设施参数读取失败（{key}={result.get(key)}），请重试")
        result["params8"] = [result[k] for k in ("p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7")]
        result["praw"] = result.get("praw") or []
        self._facility_last = {
            "display": list(result["params8"]),
            "praw": list(result["praw"]),
            "snapshot": snapshot,
            "pos": (map_index, x, y),
        }
        return result

    def apply_facility(self, targets4):
        """保存设施四项显示值：换算成差值 param_[0..3] = 目标 - 基础值（基础值 = 显示 - 当前差值）"""
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        last = getattr(self, "_facility_last", None)
        if last is None:
            raise RuntimeError("请先读取设施")
        if len(targets4) != 4 or any(type(value) is not int for value in targets4):
            raise RuntimeError("设施目标值必须是四项整数")
        display, praw = last["display"], last["praw"]
        new_p = []
        for i in range(4):
            p_i = praw[i] if i < len(praw) else 0
            new_p.append(targets4[i] - display[i] + p_i)
        map_index, x, y = last["pos"]
        self.ensure_backup()
        try:
            return self.script.exports_sync.apply_facility_params_at(map_index, x, y, new_p, last["snapshot"])
        finally:
            self._facility_last = None

    def ensure_backup(self):
        if not self.backup_created:
            return self.backup_saves()
        return self.last_backup_path

    def backup_saves(self):
        self.backup_created = False
        root = validate_backup_root(self.backup_root)
        if not GAME_SAVES.is_dir():
            raise RuntimeError(f"找不到存档目录：{GAME_SAVES}；本次修改已中止")
        destination = root / datetime.now().strftime("%Y%m%d-%H%M%S-%f") / "saves"
        try:
            validate_backup_root(destination)
            destination.parent.mkdir(parents=True, exist_ok=False)
            validate_backup_root(destination)
            shutil.copytree(GAME_SAVES, destination)
        except Exception as exc:
            raise RuntimeError(f"备份失败，本次修改已中止：{exc}；目标：{destination}") from exc
        self.last_backup_path = destination
        self.backup_created = True
        return destination

    def close(self):
        self._facility_last = None
        if self.script is not None:
            try:
                self.script.unload()
            except Exception:
                pass
        if self.session is not None:
            try:
                self.session.detach()
            except Exception:
                pass
        self.script = None
        self.session = None
        self.pid = None


class TrainerApp(tk.Tk):
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        super().__init__()
        self._dpi_scale = self.winfo_fpixels("1i") / 96
        self._window_size_timer_id = None
        self._startup_timer_id = None
        self._closing = False
        self._settings_save_error = None
        self.title("Beastie Bay DX 修改器")
        self.game = IL2CPPGame()
        self.state = None
        self.process_var = tk.StringVar(value="未连接")
        self.status_var = tk.StringVar(value="正在准备界面……")
        self.summary_var = tk.StringVar(value="尚未读取数据")
        self.resource_vars = {name: tk.StringVar() for name, _label in RESOURCE_FIELDS}
        self.lock_vars = {name: tk.BooleanVar(value=False) for name, _ in LOCKABLE_FIELDS}
        self.max_var = tk.StringVar(value="当前上限：尚未读取")
        self.mates = []
        self.selected_mate = None
        self.mate_choice_var = tk.StringVar()
        self.mate_lv_var = tk.StringVar()
        self.mate_star_var = tk.StringVar()
        self.mate_equip_var = tk.StringVar()
        self.mate_personality_var = tk.StringVar()
        self.mate_attribute_var = tk.StringVar()
        self.mate_param_vars = {name: tk.StringVar() for name, _label in MATE_PARAM_FIELDS}
        # 当前 HP/MP（只读显示；「恢复满」写入的就是这两个当前值）
        self.mate_battle_hp_var = tk.StringVar(value="-")
        self.mate_battle_mp_var = tk.StringVar(value="-")
        self.mate_info_var = tk.StringVar(value="请先刷新读取同伴数据")
        # 物品栏
        self.items = {"cons": [], "surv": [], "equip": []}
        self.selected_item = None
        self.item_num_var = tk.StringVar()
        self.item_str_var = tk.StringVar()
        self.item_info_var = tk.StringVar(value="请先刷新读取物品数据")
        self._item_trees = {}
        # 设施属性（按坐标扫描/定位）
        self.facility_vars = {name: tk.StringVar() for name, _label in FACILITY_PARAM_FIELDS}
        self.facility_info_var = tk.StringVar(value="请先扫描设施，在列表中选中目标后读取参数")
        self.facility_name_var = tk.StringVar(value="未选择设施")
        self.facility_filter_var = tk.StringVar()
        self.facility_type_var = tk.StringVar(value="设施与道路")
        self.facility_count_var = tk.StringVar(value="尚未扫描")
        self.facility_island_var = tk.StringVar()
        self.facility_x_var = tk.StringVar()
        self.facility_y_var = tk.StringVar()
        self._facility_island_ids = []
        self._facility_island_labels = []
        self.facility_rows = []  # 最近一次扫描结果（scanFacilities 原始顺序）
        self.facility_target = None  # 最近一次读取的 {map,x,y}
        self.facility_params8 = None
        self._lock_timer_id = None
        self._lock_failures = 0
        self.tooltips = []
        self.settings = self.load_settings()
        self.game.backup_root = Path(self.settings["backup_root"])
        self.backup_path_var = tk.StringVar(value=str(self.game.backup_root))
        self.backup_directory_var = tk.StringVar(value=str(self.game.backup_root))
        self.auto_connect_var = tk.BooleanVar(value=self.settings["auto_connect"])
        self._restore_window_size()
        self._setup_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._on_window_configure, add="+")
        self._startup_timer_id = self.after(100, self._finish_startup)

    def add_help(self, parent, text):
        label = ttk.Label(parent, text="？", foreground="#1769aa", cursor="question_arrow", takefocus=1)
        self.tooltips.append(Tooltip(label, text))
        return label

    def _setup_style(self):
        available = set(tkfont.families())
        family = next(
            (name for name in ("LXGW WenKai", "霞鹜文楷", "Microsoft YaHei UI") if name in available),
            "TkDefaultFont",
        )
        self.font_base = tkfont.Font(family=family, size=9)
        self.font_heading = tkfont.Font(family=family, size=10, weight="bold")
        self.font_big = tkfont.Font(family=family, size=14)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=9)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=self.font_base)
        style.configure("TFrame", background="#f5f6f8")
        style.configure("TLabel", background="#f5f6f8")
        style.configure("TCheckbutton", background="#f5f6f8")
        style.configure("TLabelframe", background="#f5f6f8")
        style.configure("TLabelframe.Label", background="#f5f6f8", font=self.font_heading, foreground="#1769aa")
        style.configure("TNotebook", background="#f5f6f8")
        style.configure("TNotebook.Tab", font=self.font_heading, padding=(16, 6))
        style.configure("Treeview", font=self.font_base, rowheight=24)
        style.configure("Treeview.Heading", font=self.font_heading)
        style.configure(
            "Accent.TButton",
            font=self.font_heading,
            foreground="#ffffff",
            background="#1769aa",
            padding=(12, 4),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#0d47a1"), ("disabled", "#9bb8d3")],
            foreground=[("disabled", "#e8edf2")],
        )
        style.configure("Heading.TLabel", font=self.font_heading, foreground="#1769aa")
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Big.TLabel", font=self.font_big, foreground="#1769aa")
        style.configure("Status.Info.TLabel", foreground="#1769aa", background="#e8edf2")
        style.configure("Status.Ok.TLabel", foreground="#2e7d32", background="#e6f0e6")
        style.configure("Status.Error.TLabel", foreground="#c62828", background="#f7e6e6")

    def _recolor_status(self, *_args):
        text = self.status_var.get()
        if any(key in text for key in ("失败", "无法", "异常", "错误")):
            level = "Error"
        elif "成功" in text or text.startswith("已"):
            level = "Ok"
        else:
            level = "Info"
        self.status_label.configure(style=f"Status.{level}.TLabel")

    def _scrollable_page(self, parent, min_width=440, min_height=0, expand=False):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, background="#f5f6f8")
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=canvas.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        content = ttk.Frame(canvas, padding=12)
        window = canvas.create_window(0, 0, window=content, anchor="nw")

        pending = None

        def resize():
            nonlocal pending
            pending = None
            width = max(min_width, canvas.winfo_width())
            height = max(min_height, content.winfo_reqheight(), canvas.winfo_height() if expand else 0)
            canvas.itemconfigure(window, width=width, height=height if expand else 0)
            canvas.configure(scrollregion=(0, 0, width, height))

        def request_resize(_event=None):
            nonlocal pending
            if pending is None:
                pending = content.after_idle(resize)

        def cancel_resize(_event=None):
            nonlocal pending
            if pending is not None:
                content.after_cancel(pending)
                pending = None

        content._request_page_resize = request_resize

        def scroll(event):
            widget = event.widget
            if isinstance(widget, (ttk.Treeview, ttk.Combobox, ttk.Spinbox)):
                return
            while widget is not None and widget is not parent:
                widget = getattr(widget, "master", None)
            if widget is parent and event.delta:
                view = canvas.xview if event.state & 1 else canvas.yview
                move = canvas.xview_scroll if event.state & 1 else canvas.yview_scroll
                if view() != (0.0, 1.0):
                    move(-1 if event.delta > 0 else 1, "units")
                    return "break"

        canvas.bind("<Configure>", request_resize)
        content.bind("<Configure>", request_resize)
        content.bind("<Destroy>", cancel_resize, add="+")
        self.bind("<MouseWheel>", scroll, add="+")
        return content

    def _wrapping_label(self, parent, **kwargs):
        label = ttk.Label(parent, justify="left", anchor="w", wraplength=360, **kwargs)
        pending = None

        def reflow():
            nonlocal pending
            pending = None
            width = max(80, label.winfo_width() - 4)
            if label.winfo_width() > 1 and label.winfo_pixels(label.cget("wraplength")) != width:
                label.configure(wraplength=width)
            ancestor = parent
            while ancestor is not None:
                request = getattr(ancestor, "_request_page_resize", None)
                if request is not None:
                    request()
                    break
                ancestor = getattr(ancestor, "master", None)

        def request_reflow(*_args):
            nonlocal pending
            if pending is None:
                pending = label.after_idle(reflow)

        variable = kwargs.get("textvariable")
        trace = variable.trace_add("write", request_reflow) if isinstance(variable, tk.Variable) else None

        def cleanup(_event=None):
            if pending is not None:
                label.after_cancel(pending)
            if trace is not None:
                variable.trace_remove("write", trace)

        label.bind("<Configure>", request_reflow)
        label.bind("<Destroy>", cleanup, add="+")
        return label

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        overview_page = ttk.Frame(self.notebook)
        self.mate_page = ttk.Frame(self.notebook)
        self.item_page = ttk.Frame(self.notebook)
        facility_page = ttk.Frame(self.notebook)
        settings_page = ttk.Frame(self.notebook)
        self.notebook.add(overview_page, text="概览")
        self.notebook.add(self.mate_page, text="同伴属性")
        self.notebook.add(self.item_page, text="物品栏")
        self.notebook.add(facility_page, text="设施")
        self.notebook.add(settings_page, text="设置")
        self._build_overview_page(self._scrollable_page(overview_page))
        self._build_mate_page(self._scrollable_page(self.mate_page, min_width=480))
        self._build_item_page(self._scrollable_page(self.item_page, min_width=460, min_height=520, expand=True))
        self._build_facility_page(self._scrollable_page(facility_page))
        self._build_settings_page(self._scrollable_page(settings_page))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)

        self.status_label = self._wrapping_label(root, textvariable=self.status_var, style="Status.Info.TLabel", relief="sunken", padding=6)
        self.status_label.grid(row=1, column=0, sticky="ew")
        self.status_var.trace_add("write", self._recolor_status)

    def _build_overview_page(self, overview_page):
        overview_page.columnconfigure(0, weight=1)

        connection = ttk.LabelFrame(overview_page, text="游戏连接", padding=10)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        connection.columnconfigure(0, weight=1)
        connection_actions = ttk.Frame(connection)
        connection_actions.grid(row=0, column=0, sticky="ew")
        ttk.Button(connection_actions, text="重新连接", style="Accent.TButton", command=self.connect).pack(side="left")
        ttk.Button(connection_actions, text="刷新数值", command=self.refresh).pack(side="left", padx=8)
        self._wrapping_label(connection, textvariable=self.process_var, style="Heading.TLabel").grid(row=1, column=0, sticky="ew", pady=(8, 0))

        resources = ttk.LabelFrame(overview_page, text="基础资源数据", padding=12)
        resources.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        resources.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(RESOURCE_FIELDS):
            ttk.Label(resources, text=label).grid(row=row, column=0, padx=(0, 12), pady=5, sticky="w")
            ttk.Entry(resources, textvariable=self.resource_vars[key], width=22).grid(row=row, column=1, pady=5, sticky="ew")
            if key in self.lock_vars:
                lock_frame = ttk.Frame(resources)
                lock_frame.grid(row=row, column=2, padx=(12, 0), pady=5, sticky="w")
                ttk.Checkbutton(
                    lock_frame,
                    text="锁定",
                    variable=self.lock_vars[key],
                    command=lambda k=key: self.toggle_lock(k),
                ).pack(side="left")
                self.add_help(
                    lock_frame,
                    "勾选后定时补充到游戏当前上限减 1；研究升级后会跟随新上限。"
                    "锁定期间，手动输入的数值会被覆盖。",
                ).pack(side="left", padx=(4, 0))
        self._wrapping_label(resources, text="可输入 0 到 2000000000；应用后自动核对结果", style="Hint.TLabel").grid(
            row=len(RESOURCE_FIELDS), column=0, columnspan=3, pady=(8, 0), sticky="ew"
        )
        self._wrapping_label(resources, textvariable=self.max_var, style="Hint.TLabel").grid(
            row=len(RESOURCE_FIELDS) + 1, column=0, columnspan=3, pady=(4, 0), sticky="ew"
        )
        resource_actions = ttk.Frame(resources)
        resource_actions.grid(row=len(RESOURCE_FIELDS) + 2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        resource_actions.columnconfigure(0, weight=1)
        ttk.Button(resource_actions, text="应用资源修改", style="Accent.TButton", command=self.apply_resources).grid(row=0, column=0, sticky="w")
        self._wrapping_label(resource_actions, text="首次写入或锁定前自动备份存档", style="Hint.TLabel").grid(row=1, column=0, sticky="ew", pady=(6, 0))

        summary = ttk.LabelFrame(overview_page, text="存档概况", padding=12)
        summary.grid(row=2, column=0, sticky="ew")
        self._wrapping_label(summary, textvariable=self.summary_var, style="Big.TLabel").pack(fill="x")
        ttk.Label(summary, text="存档备份目录", style="Hint.TLabel").pack(anchor="w", pady=(6, 0))
        self._wrapping_label(summary, textvariable=self.backup_path_var, style="Hint.TLabel").pack(fill="x", pady=(4, 0))

    def _build_mate_page(self, mate_page):
        mate_page.columnconfigure(0, weight=1)

        selector = ttk.Frame(mate_page)
        selector.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        selector.columnconfigure(1, weight=1)
        ttk.Label(selector, text="当前同伴").grid(row=0, column=0, sticky="w")
        self.mate_selector = ttk.Combobox(selector, textvariable=self.mate_choice_var, state="readonly", width=16)
        self.mate_selector.grid(row=0, column=1, sticky="ew", padx=10)
        self.mate_selector.bind("<<ComboboxSelected>>", self.on_mate_selected)
        ttk.Button(selector, text="刷新同伴", command=self.load_mates).grid(row=0, column=2)

        basic = ttk.LabelFrame(mate_page, text="基础信息", padding=10)
        basic.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        basic.columnconfigure(1, weight=1)
        basic.columnconfigure(3, weight=1)
        ttk.Label(basic, text="等级（1~99）").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        ttk.Spinbox(basic, from_=1, to=MATE_LV_MAX, textvariable=self.mate_lv_var, width=6).grid(row=0, column=1, padx=(0, 16), pady=4, sticky="ew")
        ttk.Label(basic, text="天赋").grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")
        star_values = [f"{i} 星" for i in range(1, MATE_RACIAL_MAX + 1)]
        self.mate_star_combo = ttk.Combobox(basic, textvariable=self.mate_star_var, values=star_values, state="readonly", width=6)
        self.mate_star_combo.grid(row=0, column=3, pady=4, sticky="ew")
        ttk.Label(basic, text="性格").grid(row=1, column=0, padx=(0, 8), pady=4, sticky="w")
        self.mate_personality_combo = ttk.Combobox(basic, textvariable=self.mate_personality_var, values=list(MATE_PERSONALITIES), state="readonly", width=8)
        self.mate_personality_combo.grid(row=1, column=1, padx=(0, 16), pady=4, sticky="ew")
        ttk.Label(basic, text="特征").grid(row=1, column=2, padx=(0, 8), pady=4, sticky="w")
        self.mate_attribute_combo = ttk.Combobox(basic, textvariable=self.mate_attribute_var, values=list(MATE_ATTRIBUTES), state="readonly", width=8)
        self.mate_attribute_combo.grid(row=1, column=3, pady=4, sticky="ew")
        ttk.Label(basic, text="装备插槽").grid(row=2, column=0, padx=(0, 8), pady=4, sticky="w")
        ttk.Spinbox(basic, from_=0, to=MATE_EQUIP_MAX, textvariable=self.mate_equip_var, width=6).grid(row=2, column=1, padx=(0, 16), pady=4, sticky="ew")
        self.add_help(
            basic,
            "装备插槽与天赋星数独立，最多 5 个。调低前，请先在游戏内卸下多余装备。",
        ).grid(row=2, column=2, columnspan=2, pady=4, sticky="w")

        status = ttk.LabelFrame(mate_page, text="HP / MP（当前值 · 上限）", padding=10)
        status.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        status.columnconfigure(1, weight=1)
        status.columnconfigure(3, weight=1)
        for row, (name, label) in enumerate(MATE_STATUS_FIELDS):
            ttk.Label(status, text=f"{label} 当前").grid(row=row, column=0, padx=(0, 8), pady=4, sticky="w")
            self._wrapping_label(status, textvariable=(self.mate_battle_hp_var if name == "hpMax" else self.mate_battle_mp_var)).grid(
                row=row, column=1, padx=(0, 16), pady=4, sticky="ew"
            )
            ttk.Label(status, text=f"{label} 上限").grid(row=row, column=2, padx=(0, 8), pady=4, sticky="w")
            ttk.Entry(status, textvariable=self.mate_param_vars[name], width=10).grid(row=row, column=3, pady=4, sticky="ew")
        self._wrapping_label(
            status,
            text="上限修改在升级后保留；当前值请用“恢复满 HP/MP”补满。",
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=4, pady=(6, 0), sticky="ew")
        self.add_help(
            status,
            "HP/MP 上限通过调整成长值保存，升级后仍然保留。当前 HP/MP 仅供查看，可用下方按钮恢复到上限。",
        ).grid(row=0, column=4, rowspan=2, padx=(8, 0), sticky="e")

        params = ttk.LabelFrame(mate_page, text="属性（升级后保留）", padding=10)
        params.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        params.columnconfigure(1, weight=1)
        params.columnconfigure(3, weight=1)
        for index, (name, label) in enumerate(MATE_COMBAT_FIELDS):
            row, col = divmod(index, 2)
            ttk.Label(params, text=label).grid(row=row, column=col * 2, padx=(0, 6), pady=3, sticky="w")
            ttk.Entry(params, textvariable=self.mate_param_vars[name], width=10).grid(row=row, column=col * 2 + 1, padx=(0, 12) if col == 0 else 0, pady=3, sticky="ew")
        self.add_help(
            params,
            "修改后的属性在升级后保留。智慧最高 99；其他属性最高 9990000。"
            "超过上限可能导致战斗计算异常；极高数值会明显影响游戏体验。",
        ).grid(row=0, column=4, rowspan=3, padx=(8, 0), sticky="e")
        self._wrapping_label(
            params,
            text="攻 / 防 / 速 / 劳可输入 0~9990000；智慧可输入 0~99。",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=5, pady=(6, 0), sticky="ew")

        actions = ttk.Frame(mate_page)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(actions, text="应用同伴修改", style="Accent.TButton", command=self.apply_mate).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Button(actions, text="恢复满 HP/MP", command=self.heal_mate).grid(row=0, column=1, padx=8, pady=3)
        ttk.Button(actions, text="一键拉满", command=self.max_mate).grid(row=1, column=0, sticky="w", pady=3)
        self.add_help(
            actions,
            "一键拉满：等级 99、天赋 5 星、智慧 99，HP/MP 上限与攻/防/速/劳设为 9990000，"
            "并恢复满 HP/MP。数值极高，会明显降低游戏难度，请确认后使用。",
        ).grid(row=1, column=1, sticky="w", padx=8, pady=3)
        self._wrapping_label(mate_page, textvariable=self.mate_info_var, style="Hint.TLabel").grid(row=5, column=0, sticky="ew", pady=(6, 0))

    def _build_item_page(self, item_page):
        item_page.columnconfigure(0, weight=1)
        item_page.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(item_page)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)
        ttk.Button(toolbar, text="刷新物品", style="Accent.TButton", command=self.load_items).grid(row=0, column=0, sticky="w")
        self._wrapping_label(toolbar, text="装备 / 消耗 / 生存", style="Hint.TLabel").grid(row=0, column=1, sticky="ew", padx=(10, 0))

        self.item_notebook = ttk.Notebook(item_page)
        self.item_notebook.grid(row=1, column=0, sticky="nsew")
        equip_tab = ttk.Frame(self.item_notebook, padding=10)
        cons_tab = ttk.Frame(self.item_notebook, padding=10)
        surv_tab = ttk.Frame(self.item_notebook, padding=10)
        self.item_notebook.add(equip_tab, text="装备")
        self.item_notebook.add(cons_tab, text="消耗")
        self.item_notebook.add(surv_tab, text="生存")

        # ---- 装备 ----
        equip_tab.columnconfigure(0, weight=1)
        equip_tab.rowconfigure(0, weight=1)
        equip_tree = self._make_item_tree(
            equip_tab,
            ("idx", "name", "num", "strengthen", "max", "type"),
            ("ID", "名称", "数量", "强化", "强化上限", "类型"),
            (50, 200, 60, 60, 80, 70),
        )
        self._item_trees["equip"] = equip_tree
        equip_tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_item_selection("equip"))
        equip_ctrl = ttk.Frame(equip_tab)
        equip_ctrl.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        equip_ctrl.columnconfigure(1, weight=1)
        ttk.Label(equip_ctrl, text="数量").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(equip_ctrl, textvariable=self.item_num_var, width=8).grid(row=0, column=1, sticky="ew", padx=(8, 10), pady=3)
        ttk.Button(equip_ctrl, text="应用数量", command=lambda: self.apply_item_num("equip")).grid(row=0, column=2, padx=(0, 6), pady=3)
        ttk.Button(equip_ctrl, text="加到上限-1", command=lambda: self.fill_items_max_minus_1("equip")).grid(row=0, column=3, pady=3)
        ttk.Label(equip_ctrl, text="强化等级").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(equip_ctrl, textvariable=self.item_str_var, width=8).grid(row=1, column=1, sticky="ew", padx=(8, 10), pady=3)
        ttk.Button(equip_ctrl, text="升级", command=self.upgrade_equip).grid(row=1, column=2, sticky="ew", padx=(0, 6), pady=3)
        selection = ttk.Frame(equip_ctrl)
        selection.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        ttk.Button(selection, text="全选", command=lambda: self.select_all_items("equip", True)).pack(side="left")
        ttk.Button(selection, text="全不选", command=lambda: self.select_all_items("equip", False)).pack(side="left", padx=6)
        self.add_help(
            selection,
            "点击选择物品，支持 Ctrl 多选、Shift 连选。数量和升级操作对全部勾选物品生效。\n"
            "“加到上限-1”将数量设为 98，留出拾取空间。升级会逐件检查强化上限，超限物品会提示失败。",
        ).pack(side="left", padx=(4, 0))

        # ---- 消耗 ----
        cons_tab.columnconfigure(0, weight=1)
        cons_tab.rowconfigure(0, weight=1)
        cons_tree = self._make_item_tree(
            cons_tab,
            ("idx", "name", "num", "limit"),
            ("ID", "名称", "数量", "使用限制"),
            (50, 240, 70, 80),
        )
        self._item_trees["cons"] = cons_tree
        cons_tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_item_selection("cons"))
        cons_ctrl = ttk.Frame(cons_tab)
        cons_ctrl.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        cons_ctrl.columnconfigure(1, weight=1)
        ttk.Label(cons_ctrl, text="数量").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(cons_ctrl, textvariable=self.item_num_var, width=8).grid(row=0, column=1, sticky="ew", padx=(8, 10), pady=3)
        ttk.Button(cons_ctrl, text="应用数量", command=lambda: self.apply_item_num("cons")).grid(row=0, column=2, padx=(0, 6), pady=3)
        ttk.Button(cons_ctrl, text="加到上限-1", command=lambda: self.fill_items_max_minus_1("cons")).grid(row=0, column=3, pady=3)
        selection = ttk.Frame(cons_ctrl)
        selection.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        ttk.Button(selection, text="全选", command=lambda: self.select_all_items("cons", True)).pack(side="left")
        ttk.Button(selection, text="全不选", command=lambda: self.select_all_items("cons", False)).pack(side="left", padx=6)
        self.add_help(
            selection,
            "数量可输入 0~99，设为 0 会丢弃道具。数量操作对全部勾选物品生效，支持 Ctrl 多选、Shift 连选。\n"
            "“使用限制”是游戏内使用或携带条件，不是数量上限。",
        ).pack(side="left", padx=(4, 0))

        # ---- 生存（只读展示） ----
        surv_tab.columnconfigure(0, weight=1)
        surv_tab.rowconfigure(0, weight=1)
        surv_tree = self._make_item_tree(
            surv_tab,
            ("idx", "name", "lv", "life", "broken"),
            ("ID", "名称", "等级", "耐久", "状态"),
            (50, 220, 60, 70, 80),
        )
        self._item_trees["surv"] = surv_tree
        self._wrapping_label(
            surv_tab,
            text="生存工具的等级、耐久与状态仅供查看。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.item_notebook.bind("<<NotebookTabChanged>>", lambda _e: self._sync_item_selection())
        self._wrapping_label(item_page, textvariable=self.item_info_var, style="Hint.TLabel").grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _make_item_tree(self, parent, columns, headings, widths):
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("check",) + tuple(columns), show="headings", height=8, selectmode="extended")
        tree.heading("check", text="选")
        tree.column("check", width=36, minwidth=36, anchor="center", stretch=False)
        for col, heading, width in zip(columns, headings, widths):
            tree.heading(col, text=heading)
            tree.column(col, width=width, minwidth=width, anchor="w", stretch=col == "name")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tree

    def _row_by_iid(self, kind, iid):
        if not iid:
            return None
        try:
            idx = int(iid)
        except (ValueError, TypeError):
            return None
        return getattr(self, "_item_indices", {}).get(kind, {}).get(idx)

    def _checked_rows(self, kind):
        return [row for iid in self._item_trees[kind].selection()
                if (row := self._row_by_iid(kind, iid)) is not None]

    def select_all_items(self, kind, select=True):
        tree = self._item_trees[kind]
        tree.selection_set(tree.get_children() if select else ())
        if not select:
            tree.focus("")
        self._sync_item_selection(kind)

    def _clear_items(self):
        self.items = {kind: [] for kind in self._item_trees}
        self._item_indices = {kind: {} for kind in self._item_trees}
        self._item_checked = {kind: set() for kind in self._item_trees}
        self._items_loaded = False
        self.selected_item = None
        for tree in self._item_trees.values():
            children = tree.get_children()
            if children:
                tree.delete(*children)
            tree.focus("")
        self.item_num_var.set("")
        self.item_str_var.set("")

    def load_items(self, silent=False, keep_selection=None):
        selections = {kind: list(tree.selection()) for kind, tree in self._item_trees.items()}
        focuses = {kind: tree.focus() for kind, tree in self._item_trees.items()}
        if keep_selection is not None:
            selections.update({kind: [str(i) for i in indices] for kind, indices in keep_selection.items()})
        try:
            self.items = self.game.read_items()
            self._populate_item_trees()
            for kind, tree in self._item_trees.items():
                valid = [iid for iid in selections.get(kind, ()) if int(iid) in self._item_indices[kind]]
                tree.selection_set(valid)
                focus = focuses[kind] if focuses[kind] in valid else (valid[0] if valid else "")
                tree.focus(focus)
                self._sync_item_selection(kind)
            self._sync_item_selection()
            counts = {kind: len(rows) for kind, rows in self.items.items()}
            self.item_info_var.set(
                f"装备 {counts['equip']} 件 · 消耗 {counts['cons']} 种 · 生存 {counts['surv']} 件"
            )
            self._items_loaded = True
            self._item_load_error = ""
            if not silent:
                self.status_var.set("已读取物品数据")
            return True
        except Exception as exc:
            self._clear_items()
            self._item_load_error = str(exc)
            self.item_info_var.set(f"物品读取失败：{exc}；请重新读取后操作")
            if not silent:
                self.status_var.set(f"物品读取失败：{exc}")
            return False

    def _populate_item_trees(self):
        self._item_indices = {kind: {row["idx"]: row for row in rows} for kind, rows in self.items.items()}
        self._item_checked = {kind: set() for kind in self._item_trees}
        for tree in self._item_trees.values():
            children = tree.get_children()
            if children:
                tree.delete(*children)
            tree.focus("")
        for row in self.items["equip"]:
            self._item_trees["equip"].insert(
                "", "end", iid=str(row["idx"]),
                values=("☐", row["idx"], row["name"].strip(), row["num"], row["strengthen"], row["maxStrengthen"], "武器" if row["type"] == 0 else "饰品"),
            )
        for row in self.items["cons"]:
            self._item_trees["cons"].insert(
                "", "end", iid=str(row["idx"]),
                values=("☐", row["idx"], row["name"].strip(), row["num"], row["limit"]),
            )
        for row in self.items["surv"]:
            self._item_trees["surv"].insert(
                "", "end", iid=str(row["idx"]),
                values=("☐", row["idx"], row["name"].strip(), row["lv"], row["life"], "破损" if row["broken"] else "正常"),
            )

    def _selected_item(self, kind):
        tree = self._item_trees[kind]
        selected = tree.selection()
        focus = tree.focus()
        iid = focus if focus in selected else (selected[0] if selected else "")
        if focus != iid:
            tree.focus(iid)
        return self._row_by_iid(kind, iid)

    def _current_item_kind(self):
        tab = self.item_notebook.index(self.item_notebook.select())
        return ("equip", "cons", "surv")[tab]

    def _sync_item_selection(self, kind=None, *_args):
        if kind is None:
            kind = self._current_item_kind()
        tree = self._item_trees[kind]
        selected = set(tree.selection())
        if not hasattr(self, "_item_checked"):
            self._item_checked = {}
        previous = self._item_checked.get(kind, set())
        for iid in previous ^ selected:
            if tree.exists(iid):
                tree.set(iid, "check", "☑" if iid in selected else "☐")
        self._item_checked[kind] = selected
        if kind != self._current_item_kind():
            return
        row = self._selected_item(kind) if kind != "surv" else None
        self.selected_item = dict(row, kind=kind) if row else None
        self.item_num_var.set(str(row["num"]) if row else "")
        self.item_str_var.set(str(row["strengthen"]) if row and kind == "equip" else "")

    def _report_item_batch(self, result, kind, rows, action):
        refreshed = self.load_items(silent=True, keep_selection={kind: [row["idx"] for row in rows]})
        failures = result.get("fail", [])
        message = f"{action}：成功 {len(result.get('ok', []))} 件，失败 {len(failures)} 件"
        details = "\n".join(f"索引 {entry.get('idx', '?')}：{entry.get('error', '未知错误')}" for entry in failures)
        if details:
            message += "；" + details.replace("\n", "；")
        if not refreshed:
            message += f"；刷新失败：{self._item_load_error}，列表已清空，请重新读取"
        self.status_var.set(message)
        if failures:
            messagebox.showwarning("批量操作结果", message, parent=self)

    def apply_item_num(self, kind):
        if kind not in ("equip", "cons"):
            self.status_var.set("无法应用：该类物品不支持修改数量")
            return
        rows = self._checked_rows(kind)
        if not rows:
            self.status_var.set("无法应用：请先勾选要修改的物品（点击行即可勾选）")
            return
        try:
            value = int(self.item_num_var.get())
        except ValueError:
            self.status_var.set("无法应用：数量必须是整数")
            return
        if not 0 <= value <= ITEM_NUM_MAX:
            self.status_var.set(f"无法应用：数量必须在 0 到 {ITEM_NUM_MAX} 之间")
            return
        if value == 0 and not messagebox.askyesno(
            "确认丢弃物品", f"将把勾选的 {len(rows)} 件物品数量设为 0，相当于丢弃这些物品。确定继续？", parent=self,
        ):
            return
        try:
            result = self.game.apply_item_num_batch(kind, [r["idx"] for r in rows], value)
        except Exception as exc:
            self._clear_items()
            self.item_info_var.set("写入结果不确定，请重新读取物品")
            self.status_var.set(f"数量修改失败：{exc}；请重新读取确认实际数量")
            return
        self._report_item_batch(result, kind, rows, f"数量设为 {value}")

    def fill_items_max_minus_1(self, kind):
        if kind not in ("equip", "cons"):
            self.status_var.set("无法填充：该类物品不支持修改数量")
            return
        rows = self._checked_rows(kind)
        if not rows:
            self.status_var.set(f"无法填充：请先勾选要修改的物品（目标 {ITEM_NUM_MAX - 1}）")
            return
        try:
            result = self.game.apply_item_num_batch(kind, [r["idx"] for r in rows], ITEM_NUM_MAX - 1)
        except Exception as exc:
            self._clear_items()
            self.item_info_var.set("写入结果不确定，请重新读取物品")
            self.status_var.set(f"填充失败：{exc}；请重新读取确认实际数量")
            return
        self._report_item_batch(result, kind, rows, f"数量设为 {ITEM_NUM_MAX - 1}")

    def upgrade_equip(self):
        rows = self._checked_rows("equip")
        if not rows:
            self.status_var.set("无法升级：请先勾选要升级的装备（点击行即可勾选）")
            return
        try:
            target = int(self.item_str_var.get())
        except ValueError:
            self.status_var.set("无法升级：目标等级必须是整数")
            return
        if not 0 <= target <= 127:
            self.status_var.set("无法升级：目标强化等级必须在 0 到 127 之间，且不能超过各装备上限")
            return
        eligible = [row for row in rows if target <= row["maxStrengthen"]]
        failures = [{"idx": row["idx"], "error": f"超过该装备强化上限 {row['maxStrengthen']} 级"}
                    for row in rows if target > row["maxStrengthen"]]
        if not eligible:
            message = "升级失败：" + "；".join(f"索引 {entry['idx']}：{entry['error']}" for entry in failures)
            self.status_var.set(message)
            messagebox.showwarning("装备强化结果", message, parent=self)
            return
        try:
            result = self.game.upgrade_equip_batch([r["idx"] for r in eligible], target)
        except Exception as exc:
            self._clear_items()
            self.item_info_var.set("写入结果不确定，请重新读取物品")
            self.status_var.set(f"升级失败：{exc}；请重新读取确认实际等级")
            return
        result["fail"] = failures + result.get("fail", [])
        self._report_item_batch(result, "equip", rows, f"强化到 {target} 级")

    def _build_facility_page(self, facility_page):
        facility_page.columnconfigure(0, weight=1)
        facility_page.rowconfigure(1, weight=1)

        intro = ttk.LabelFrame(facility_page, text="使用方式", padding=10)
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._wrapping_label(
            intro,
            text="点「扫描设施」列出各岛全部设施与地块（含岛名）→ 在列表中点击选中，或在「坐标查找」输入岛和 X/Y 直接定位 → "
                 "「读取参数」→ 修改四项数值 → 「保存设施」。保存时会重新核对目标格未变化。",
            style="Hint.TLabel",
        ).pack(fill="x")

        list_frame = ttk.LabelFrame(facility_page, text="设施列表（坐标 = 游戏地图格位）", padding=10)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(list_frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="扫描设施", style="Accent.TButton", command=self.scan_facilities).pack(side="left")
        ttk.Label(toolbar, text="名称筛选").pack(side="left", padx=(12, 4))
        filter_entry = ttk.Entry(toolbar, textvariable=self.facility_filter_var, width=14)
        filter_entry.pack(side="left")
        filter_entry.bind("<KeyRelease>", lambda _e: self._refilter_facilities())
        ttk.Label(toolbar, text="类型").pack(side="left", padx=(12, 4))
        type_combo = ttk.Combobox(
            toolbar, textvariable=self.facility_type_var,
            values=("设施与道路", "仅地形", "全部"), state="readonly", width=10,
        )
        type_combo.pack(side="left")
        type_combo.bind("<<ComboboxSelected>>", lambda _e: self._refilter_facilities())
        ttk.Label(toolbar, textvariable=self.facility_count_var, style="Hint.TLabel").pack(side="left", padx=(12, 0))

        lookup = ttk.Frame(list_frame)
        lookup.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(lookup, text="坐标查找").pack(side="left")
        self.facility_island_combo = ttk.Combobox(
            lookup, textvariable=self.facility_island_var, state="readonly", width=14
        )
        self.facility_island_combo.pack(side="left", padx=(6, 10))
        ttk.Label(lookup, text="X").pack(side="left")
        ttk.Entry(lookup, textvariable=self.facility_x_var, width=6).pack(side="left", padx=(4, 8))
        ttk.Label(lookup, text="Y").pack(side="left")
        ttk.Entry(lookup, textvariable=self.facility_y_var, width=6).pack(side="left", padx=(4, 8))
        ttk.Button(lookup, text="定位", command=self.locate_facility_xy).pack(side="left")
        self.add_help(
            lookup,
            "输入岛和坐标后直接定位到列表中的对应格（会自动清除筛选）；该格没有设施或是空地、超出范围时会提示。",
        ).pack(side="left", padx=(8, 0))

        tree_frame = ttk.Frame(list_frame)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.facility_tree = ttk.Treeview(
            tree_frame,
            columns=("map", "mapname", "x", "y", "name", "rebuild", "build"),
            show="headings", height=9, selectmode="browse",
        )
        for col, heading, width, anchor in (
            ("map", "岛", 36, "center"),
            ("mapname", "岛名", 92, "w"),
            ("x", "X", 44, "center"),
            ("y", "Y", 44, "center"),
            ("name", "名称", 150, "w"),
            ("rebuild", "改建", 48, "center"),
            ("build", "建造中", 54, "center"),
        ):
            self.facility_tree.heading(col, text=heading)
            self.facility_tree.column(col, width=width, minwidth=width, anchor=anchor, stretch=col in ("mapname", "name"))
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.facility_tree.yview)
        self.facility_tree.configure(yscrollcommand=tree_scroll.set)
        self.facility_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.facility_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_facility_selected())

        self._wrapping_label(list_frame, textvariable=self.facility_name_var, style="Hint.TLabel").grid(
            row=3, column=0, sticky="ew", pady=(6, 0)
        )

        params = ttk.LabelFrame(facility_page, text="设施属性", padding=10)
        params.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        params.columnconfigure(1, weight=1)
        for row, (name, label) in enumerate(FACILITY_PARAM_FIELDS):
            ttk.Label(params, text=label).grid(row=row, column=0, padx=(0, 10), pady=4, sticky="w")
            ttk.Entry(params, textvariable=self.facility_vars[name], width=14).grid(row=row, column=1, pady=4, sticky="ew")
        self.add_help(
            params,
            "对应设施信息中的文明、自然、能量、回复量。修改只对当前选中的这一格生效，并随游戏存档保存。"
            "地形（草、森等）同样有四项数值，可正常读取修改。",
        ).grid(row=0, column=2, rowspan=4, padx=(16, 0), sticky="e")
        actions = ttk.Frame(params)
        actions.grid(row=0, column=3, rowspan=4, padx=(12, 0), sticky="ns")
        ttk.Button(actions, text="读取参数", style="Accent.TButton", command=self.read_facility).pack(side="top", pady=2)
        ttk.Button(actions, text="保存设施", style="Accent.TButton", command=self.apply_facility).pack(side="top", pady=2)

        self._wrapping_label(facility_page, textvariable=self.facility_info_var, style="Hint.TLabel").grid(
            row=3, column=0, sticky="ew"
        )

    def _clear_facility(self):
        self.facility_target = None
        self.facility_params8 = None
        for var in self.facility_vars.values():
            var.set("")

    def _show_facility(self, result):
        self.facility_target = {key: result[key] for key in ("map", "x", "y")}
        self.facility_params8 = list(result["params8"])
        for i, (name, _label) in enumerate(FACILITY_PARAM_FIELDS):
            self.facility_vars[name].set(str(result["params8"][i]))
        map_name = str(result.get("mapName") or "").strip()
        map_label = f"岛 {result['map'] + 1}（{map_name}）" if map_name else f"岛 {result['map'] + 1}"
        self.facility_info_var.set(
            f"已读取：{map_label} 格 ({result['x']},{result['y']}) · 模型 {result['modelID']} · 改建 {result['rebuild']}"
        )

    def _selected_facility_row(self):
        selection = self.facility_tree.selection()
        if not selection:
            return None
        try:
            index = int(selection[0])
        except (ValueError, TypeError):
            return None
        if 0 <= index < len(self.facility_rows):
            return self.facility_rows[index]
        return None

    def _on_facility_selected(self):
        row = self._selected_facility_row()
        if row is None:
            self.facility_name_var.set("未选择设施")
            return
        map_label = self._facility_map_label(row)
        self.facility_name_var.set(
            f"已选中：{map_label} 格 ({row['x']},{row['y']}) · {row['name']} · 模型 {row['modelID']}"
        )

    def _facility_map_label(self, row):
        map_name = str(row.get("mapName") or "").strip()
        return f"岛 {row['map'] + 1}（{map_name}）" if map_name else f"岛 {row['map'] + 1}"

    def _facility_type_pass(self, row):
        kind = self.facility_type_var.get()
        if kind == "仅地形":
            return row.get("menu") == 2
        if kind == "全部":
            return True
        return row.get("menu") != 2  # 设施与道路（menu 0/1），排除地形（menu 2）

    def _refilter_facilities(self):
        if not hasattr(self, "facility_tree"):
            return
        needle = self.facility_filter_var.get().strip().lower()
        tree = self.facility_tree
        for iid in tree.get_children():
            tree.delete(iid)
        shown = 0
        for index, row in enumerate(self.facility_rows):
            if not self._facility_type_pass(row):
                continue
            if needle and needle not in str(row["name"]).lower():
                continue
            tree.insert("", "end", iid=str(index), values=(
                row["map"] + 1, row.get("mapName") or "", row["x"], row["y"], row["name"],
                "✔" if row["rebuild"] else "",
                "…" if row["build"] else "",
            ))
            shown += 1
        self.facility_count_var.set(f"显示 {shown} / 共 {len(self.facility_rows)}")
        self._on_facility_selected()

    def _refresh_facility_islands(self):
        names = {}
        for row in self.facility_rows:
            names.setdefault(row["map"], str(row.get("mapName") or "").strip())
        ids = sorted(names)
        labels = []
        for map_index in ids:
            map_name = names[map_index]
            labels.append(f"{map_index + 1} · {map_name}" if map_name else f"岛 {map_index + 1}")
        self._facility_island_ids = ids
        self._facility_island_labels = labels
        self.facility_island_combo.configure(values=labels)
        if labels:
            current = self.facility_island_var.get()
            if current not in labels:
                self.facility_island_var.set(labels[0])

    def locate_facility_xy(self):
        if not self.facility_rows:
            self.status_var.set("无法定位：请先扫描设施")
            return
        try:
            x = int(self.facility_x_var.get())
            y = int(self.facility_y_var.get())
        except ValueError:
            self.status_var.set("无法定位：X、Y 必须是整数")
            return
        map_index = None
        selected = self.facility_island_var.get()
        for label, map_id in zip(self._facility_island_labels, self._facility_island_ids):
            if selected == label:
                map_index = map_id
                break
        if map_index is None:
            self.status_var.set("无法定位：请先选择岛")
            return
        row_index = next(
            (i for i, row in enumerate(self.facility_rows)
             if row["map"] == map_index and row["x"] == x and row["y"] == y),
            None,
        )
        if row_index is None:
            self.status_var.set(f"岛 {map_index + 1} 格 ({x},{y}) 没有设施或是空地、超出范围")
            return
        self.facility_filter_var.set("")
        self.facility_type_var.set("全部")
        self._refilter_facilities()
        iid = str(row_index)
        self.facility_tree.selection_set(iid)
        self.facility_tree.focus(iid)
        self.facility_tree.see(iid)
        self._on_facility_selected()
        row = self.facility_rows[row_index]
        self.status_var.set(
            f"已定位：{self._facility_map_label(row)} 格 ({x},{y}) · {row['name']}，可点「读取参数」"
        )

    def scan_facilities(self):
        try:
            rows = self.game.scan_facilities()
        except Exception as exc:
            self.facility_rows = []
            self._refilter_facilities()
            self.facility_info_var.set("扫描失败，请重新扫描")
            self.status_var.set(f"扫描设施失败：{exc}")
            return False
        self.facility_rows = rows
        self._clear_facility()
        self._refresh_facility_islands()
        self._refilter_facilities()
        if not rows:
            self.facility_info_var.set("未扫描到任何设施或地块")
        else:
            self.facility_info_var.set(f"已扫描到 {len(rows)} 个设施/地块，选中后点「读取参数」，或直接输入坐标定位")
        self.status_var.set(f"已扫描设施：{len(rows)} 个")
        return True

    def read_facility(self):
        self._clear_facility()
        row = self._selected_facility_row()
        if row is None:
            self.facility_info_var.set("请先在列表中选中一个设施或地块")
            self.status_var.set("无法读取：请先在设施列表中选中目标")
            return False
        try:
            self._show_facility(self.game.read_facility(row["map"], row["x"], row["y"]))
        except Exception as exc:
            self._clear_facility()
            self.facility_info_var.set("设施读取失败，请重新读取")
            self.status_var.set(f"读取设施失败：{exc}")
            return False
        self.status_var.set("已读取设施属性")
        return True

    def apply_facility(self):
        if self.facility_params8 is None or self.facility_target is None:
            self.status_var.set("无法保存：请先选中设施并读取参数")
            return
        targets = []
        for name, label in FACILITY_PARAM_FIELDS:
            try:
                value = int(self.facility_vars[name].get())
            except ValueError:
                self.status_var.set(f"无法保存：{label}必须是整数")
                return
            if not 0 <= value <= RESOURCE_LIMIT:
                self.status_var.set(f"无法保存：{label}必须在 0 到 {RESOURCE_LIMIT} 之间")
                return
            targets.append(value)
        expected = dict(self.facility_target)
        try:
            result = self.game.apply_facility(targets)
        except Exception as exc:
            self._clear_facility()
            self.facility_info_var.set("保存失败，请重新读取设施后操作")
            self.status_var.set(f"保存设施失败：{exc}；请重新读取确认实际属性")
            return
        try:
            refreshed = self.game.read_facility(expected["map"], expected["x"], expected["y"])
            if any(refreshed[key] != expected[key] for key in expected):
                raise RuntimeError("回读时目标格已变化，无法确认设施属性")
            self._show_facility(refreshed)
            if list(refreshed["params8"][:4]) != targets:
                self.status_var.set("设施保存后的回读值与目标不一致，已显示实际数值，请检查")
                return
        except Exception as exc:
            self._clear_facility()
            self.facility_info_var.set("保存请求已返回，但刷新失败，请重新读取设施")
            self.status_var.set(f"设施保存请求已返回，但刷新失败：{exc}；尚未确认实际属性")
            return
        self.status_var.set(
            f"已保存设施属性并确认回读值：岛 {result['map'] + 1} 格 ({result['x']},{result['y']}) → 文明{targets[0]} 自然{targets[1]} 能量{targets[2]} 回复{targets[3]}"
        )

    def load_settings(self):
        defaults = {"backup_root": str(BACKUP_ROOT), "auto_connect": True}
        self._settings_load_error = None
        try:
            if not SETTINGS_FILE.exists():
                return defaults
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError("设置内容必须是 JSON 对象")
            if "backup_root" in settings:
                if not isinstance(settings["backup_root"], str):
                    raise ValueError("备份目录必须是字符串")
                settings["backup_root"] = str(validate_backup_root(settings["backup_root"]))
            if "auto_connect" in settings and type(settings["auto_connect"]) is not bool:
                raise ValueError("启动自动连接必须是布尔值")
            if "window_size" in settings:
                size = settings["window_size"]
                if (not isinstance(size, dict)
                        or any(type(size.get(key)) is not int or not 100 <= size[key] <= 50000
                               for key in ("width", "height"))
                        or type(size.get("dpi")) not in (int, float)
                        or not 0.5 <= size["dpi"] <= 8):
                    raise ValueError("窗口尺寸设置无效")
            return {**defaults, **settings}
        except (OSError, ValueError, RuntimeError) as exc:
            self._settings_load_error = f"无法读取设置，已使用默认值：{exc}\n设置文件：{SETTINGS_FILE}"
            return defaults

    def save_settings(self, show_error=True):
        temporary = None
        try:
            payload = json.dumps(self.settings, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=SETTINGS_FILE.parent, prefix=".trainer-settings-",
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, SETTINGS_FILE)
            self._settings_save_error = None
            return True
        except (OSError, ValueError, TypeError) as exc:
            error = f"无法保存设置：{exc}\n设置文件：{SETTINGS_FILE}"
            self.status_var.set(error.replace("\n", "；"))
            if show_error and error != self._settings_save_error:
                messagebox.showerror("设置保存失败", error, parent=self)
            self._settings_save_error = error
            return False
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError as exc:
                    self.status_var.set(f"设置临时文件清理失败：{temporary}；{exc}")

    def _build_settings_page(self, parent):
        parent.columnconfigure(0, weight=1)
        section = ttk.LabelFrame(parent, text="备份与启动", padding=14)
        section.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        section.columnconfigure(0, weight=1)
        ttk.Label(section, text="备份目录").grid(row=0, column=0, sticky="w")
        directory = ttk.Frame(section)
        directory.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        directory.columnconfigure(0, weight=1)
        ttk.Entry(directory, textvariable=self.backup_directory_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(directory, text="浏览…", command=self._browse_backup_directory).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(section, text="保存后生效；首次修改前才创建备份。请选择游戏目录以外的位置。",
                  style="Hint.TLabel", wraplength=440).grid(row=2, column=0, sticky="w", pady=(0, 10))
        ttk.Checkbutton(section, text="启动时自动连接游戏", variable=self.auto_connect_var).grid(row=3, column=0, sticky="w")
        actions = ttk.Frame(section)
        actions.grid(row=4, column=0, sticky="w", pady=(14, 0))
        ttk.Button(actions, text="恢复默认", command=self._reset_settings_draft).pack(side="left")
        ttk.Button(actions, text="保存设置", command=self._apply_settings).pack(side="left", padx=(8, 0))

    def _browse_backup_directory(self):
        directory = filedialog.askdirectory(parent=self, title="选择备份目录", mustexist=False)
        if directory:
            self.backup_directory_var.set(directory)

    def _reset_settings_draft(self):
        self.backup_directory_var.set(str(BACKUP_ROOT))
        self.auto_connect_var.set(True)
        self.status_var.set("已恢复默认设置草稿，点击「保存设置」后生效")

    def _apply_settings(self):
        try:
            root = validate_backup_root(self.backup_directory_var.get())
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("备份目录无效", str(exc), parent=self)
            return
        previous = self.settings
        self.settings = {**previous, "backup_root": str(root), "auto_connect": self.auto_connect_var.get()}
        if not self.save_settings():
            self.settings = previous
            return
        if root != self.game.backup_root:
            self.game.backup_root = root
            self.game.backup_created = False
        self.backup_path_var.set(str(root))
        self.backup_directory_var.set(str(root))
        self.status_var.set("设置已保存；备份目录立即生效，自动连接选项在下次启动时生效")

    def _finish_startup(self):
        self._startup_timer_id = None
        if self._settings_load_error:
            messagebox.showwarning("设置读取失败", self._settings_load_error, parent=self)
        if self.settings["auto_connect"]:
            self.connect()
        else:
            self.status_var.set("自动连接已关闭，可点击「重新连接」手动连接")

    def _restore_window_size(self):
        scale = self._dpi_scale
        available_width = max(240, self.winfo_screenwidth() - round(48 * scale))
        available_height = max(200, self.winfo_screenheight() - round(96 * scale))
        minimum_width = min(round(680 * scale), available_width)
        minimum_height = min(round(500 * scale), available_height)
        size = self.settings.get("window_size")
        if size:
            ratio = scale / size["dpi"]
            width, height = round(size["width"] * ratio), round(size["height"] * ratio)
        else:
            width, height = round(920 * scale), round(680 * scale)
        width = min(available_width, max(minimum_width, width))
        height = min(available_height, max(minimum_height, height))
        self.minsize(minimum_width, minimum_height)
        self.geometry(f"{width}x{height}")
        self._normal_window_size = {"width": width, "height": height, "dpi": scale}

    def _on_window_configure(self, event):
        if event.widget is not self or self._closing or self.wm_state() != "normal":
            return
        size = {"width": event.width, "height": event.height, "dpi": self._dpi_scale}
        if event.width < 100 or event.height < 100 or size == self._normal_window_size:
            return
        self._normal_window_size = size
        if self._window_size_timer_id is not None:
            self.after_cancel(self._window_size_timer_id)
        self._window_size_timer_id = self.after(WINDOW_SIZE_DELAY_MS, self._persist_window_size)

    def _capture_window_size(self):
        if self.wm_state() == "normal":
            width, height = self.winfo_width(), self.winfo_height()
            if width >= 100 and height >= 100:
                self._normal_window_size = {"width": width, "height": height, "dpi": self._dpi_scale}
        self.settings["window_size"] = dict(self._normal_window_size)

    def _persist_window_size(self):
        self._window_size_timer_id = None
        if not self._closing:
            self._capture_window_size()
            self.save_settings()

    def _clear_resources(self):
        self.state = None
        for var in self.resource_vars.values():
            var.set("")
        self.summary_var.set("尚未读取数据")
        self.max_var.set("当前上限：尚未读取")

    def _on_main_tab_changed(self, _event=None):
        if self.state is None:
            return True
        page = self.notebook.select()
        if page == str(getattr(self, "mate_page", "")) and not getattr(self, "_mates_loaded", False):
            return self.load_mates()
        if page == str(getattr(self, "item_page", "")) and not getattr(self, "_items_loaded", False):
            return self.load_items()
        return True

    def refresh(self):
        self._clear_mates(preserve_selection=True)
        self._clear_items()
        self.item_info_var.set("物品数据待刷新，切到物品栏时读取")
        try:
            self.update_state(self.game.read_state())
        except Exception as exc:
            self._clear_resources()
            self._stop_lock_loop()
            for var in self.lock_vars.values():
                var.set(False)
            self.status_var.set(f"资源读取失败：{exc}；请重新刷新后操作")
            return False
        self.status_var.set("已读取资源数据；同伴与物品按分页读取")
        return self._on_main_tab_changed()

    def connect(self):
        self._stop_lock_loop()
        for var in self.lock_vars.values():
            var.set(False)
        self._clear_resources()
        self._clear_mates()
        self._clear_items()
        self._clear_facility()
        self.item_info_var.set("请切到物品栏读取数据")
        self.facility_info_var.set("请重新扫描设施后读取参数")
        self.process_var.set("正在连接……")
        try:
            self.game.connect()
            self.process_var.set(f"已连接：{GAME_NAME}（PID {self.game.pid}）")
            return self.refresh()
        except Exception as exc:
            self.process_var.set("未连接")
            self.status_var.set(f"连接失败：{exc}")
            return False

    def update_state(self, state):
        values = {key: str(state[key]) for key in self.resource_vars}
        maxima = []
        for _key, max_key in LOCKABLE_FIELDS:
            maximum = state.get(max_key)
            maxima.append(int(maximum) if maximum is not None else None)
        summary = f"金钱 {state['money']}  ·  奖牌 {state['medals']}  ·  食物 {state['food']}  ·  资源 {state['wood']}"
        self.state = state
        for key, value in values.items():
            self.resource_vars[key].set(value)
        if all(maximum is not None for maximum in maxima):
            labels = dict(RESOURCE_FIELDS)
            parts = [f"{labels[key]} {maximum}" for (key, _max_key), maximum in zip(LOCKABLE_FIELDS, maxima)]
            self.max_var.set("当前上限：" + "  ·  ".join(parts) + "（锁定目标 = 上限 - 1）")
        else:
            self.max_var.set("当前上限：无法读取，请进入正常游戏界面后刷新")
        self.summary_var.set(summary)

    def locked_targets(self, state):
        targets = {}
        for key, max_key in LOCKABLE_FIELDS:
            maximum = state.get(max_key)
            if isinstance(maximum, int) and not isinstance(maximum, bool) and 1 <= maximum <= RESOURCE_LIMIT + 1:
                targets[key] = maximum - 1
        return targets

    def toggle_lock(self, key):
        if self.lock_vars[key].get():
            if self.state is None or key not in self.locked_targets(self.state):
                self.lock_vars[key].set(False)
                self.status_var.set("无法锁定：请先刷新并确认该项上限可读取且有效")
                return
            if self._lock_timer_id is None:
                self._lock_failures = 0
                self.status_var.set(f"已启用{dict(RESOURCE_FIELDS)[key]}锁定（目标 = 实时上限 - 1，每 0.7 秒检查）")
                self._lock_loop()
            else:
                self.status_var.set(f"已启用{dict(RESOURCE_FIELDS)[key]}锁定")
        else:
            if not any(var.get() for var in self.lock_vars.values()):
                self._stop_lock_loop()
            self.status_var.set(f"已停用{dict(RESOURCE_FIELDS)[key]}锁定")

    def _lock_loop(self):
        self._lock_timer_id = None
        enabled = {key for key, var in self.lock_vars.items() if var.get()}
        if not enabled:
            return
        try:
            state = self.game.read_state()
            targets = self.locked_targets(state)
            missing = enabled - targets.keys()
            if missing:
                labels = dict(RESOURCE_FIELDS)
                raise RuntimeError("无法读取有效上限：" + "、".join(labels[key] for key in sorted(missing)))
            wanted = {key: targets[key] for key in enabled}
            needs_write = any(state[key] != value for key, value in wanted.items())
            if needs_write:
                result = self.game.apply_lock_values(wanted)
                self.update_state(result)
                if any(result.get(key) != value for key, value in wanted.items()):
                    raise RuntimeError("锁定写入返回值与目标不一致，请刷新检查")
                self.status_var.set("锁定中，已按上限 - 1 补回数值")
            else:
                self.update_state(state)
            self._lock_failures = 0
        except Exception as exc:
            self._clear_resources()
            self._lock_failures += 1
            failures = self._lock_failures
            self.status_var.set(f"锁定检查失败（{failures}/{LOCK_MAX_FAILURES}）：{exc}")
            if failures >= LOCK_MAX_FAILURES:
                for var in self.lock_vars.values():
                    var.set(False)
                self._stop_lock_loop()
                self.status_var.set(f"锁定已停止：连续读取或写入失败；最后原因：{exc}。请进入游戏并刷新数值")
                return
        if any(var.get() for var in self.lock_vars.values()):
            self._lock_timer_id = self.after(LOCK_INTERVAL_MS, self._lock_loop)

    def _stop_lock_loop(self):
        if self._lock_timer_id is not None:
            self.after_cancel(self._lock_timer_id)
            self._lock_timer_id = None
        self._lock_failures = 0

    def mate_display_name(self, mate):
        box_label = "出战" if mate["box"] == 0 else "储备"
        return f"[{box_label}] {mate['name'].strip()}  Lv.{mate['lv']}"

    def _clear_mate_form(self):
        self.selected_mate = None
        for var in (self.mate_lv_var, self.mate_star_var, self.mate_equip_var,
                    self.mate_personality_var, self.mate_attribute_var):
            var.set("")
        for var in self.mate_param_vars.values():
            var.set("")
        self.mate_battle_hp_var.set("-")
        self.mate_battle_mp_var.set("-")

    def _clear_mates(self, preserve_selection=False):
        mate = self.selected_mate
        key = (mate["box"], mate["index"]) if mate is not None else getattr(self, "_mate_pick_key", None)
        self._mate_pick_key = key if preserve_selection else None
        self._mate_details = {}
        self._mates_loaded = False
        self.mates = []
        self._clear_mate_form()
        self.mate_selector.configure(values=())
        self.mate_choice_var.set("")
        self.mate_info_var.set("同伴数据待刷新，切到同伴分页时读取")

    def load_mates(self, silent=False):
        self._clear_mates(preserve_selection=True)
        try:
            mates = self.game.read_mates()
            if not self.update_mates(mates):
                return False
            self._mates_loaded = True
            if not silent:
                self.status_var.set("已读取同伴列表与当前同伴详情" if mates else "已读取同伴列表：当前没有同伴")
            return True
        except Exception as exc:
            self._clear_mates(preserve_selection=True)
            self.mate_info_var.set(f"同伴读取失败：{exc}；请重新读取")
            self.status_var.set(f"同伴读取失败：{exc}")
            return False

    def update_mates(self, mates):
        previous = self.selected_mate
        key = (previous["box"], previous["index"]) if previous is not None else getattr(self, "_mate_pick_key", None)
        self._mate_details = {}
        self.mates = list(mates)
        self._clear_mate_form()
        self.mate_selector.configure(values=[self.mate_display_name(mate) for mate in self.mates])
        if not mates:
            self._mate_pick_key = None
            self.mate_choice_var.set("当前没有同伴")
            self.mate_info_var.set("没有可修改的同伴；抓到怪或同伴入队后刷新即可")
            return True
        pick = next((i for i, mate in enumerate(mates) if (mate["box"], mate["index"]) == key), 0)
        self.mate_selector.current(pick)
        return self.on_mate_selected()

    def on_mate_selected(self, _event=None):
        pick = self.mate_selector.current()
        self._clear_mate_form()
        if pick < 0 or pick >= len(self.mates):
            self.mate_info_var.set("请先读取并选择同伴")
            return False
        self.selected_mate = self.mates[pick]
        self._mate_pick_key = (self.selected_mate["box"], self.selected_mate["index"])
        return self.load_mate_form()

    def _invalidate_current_mate(self):
        mate = self.selected_mate
        if mate is not None:
            key = (mate["box"], mate["index"])
            self._mate_pick_key = key
            getattr(self, "_mate_details", {}).pop(key, None)
        self._clear_mate_form()
        self.mate_info_var.set("详情已失效，请重新选择同伴或刷新后再操作")

    def load_mate_form(self):
        core = self.selected_mate
        if core is None:
            return False
        key = (core["box"], core["index"])
        if not hasattr(self, "_mate_details"):
            self._mate_details = {}
        try:
            mate = self._mate_details.get(key)
            if mate is None:
                mate = dict(core)
                mate.update(self.game.read_mate_details(*key))
            if (mate["box"], mate["index"]) != key:
                raise RuntimeError("同伴详情与当前选择不一致，请刷新列表")
            self.mate_lv_var.set(str(mate["lv"]))
            self.mate_star_var.set(f"{mate['racialLv'] + 1} 星")
            self.mate_equip_var.set(str(mate["equipMax"]))
            personality, attribute = mate["personality"], mate["attribute"]
            self.mate_personality_var.set(MATE_PERSONALITIES[personality] if 0 <= personality < len(MATE_PERSONALITIES) else str(personality))
            self.mate_attribute_var.set(MATE_ATTRIBUTES[attribute] if 0 <= attribute < len(MATE_ATTRIBUTES) else str(attribute))
            for name, _label in MATE_PARAM_FIELDS:
                value = min(mate[name], 99) if name == "wise" else mate[name]
                self.mate_param_vars[name].set(str(value))
            self.mate_battle_hp_var.set(str(mate["battleHP"]))
            self.mate_battle_mp_var.set(str(mate["battleMP"]))
            self.mate_info_var.set(
                f"当前 HP {mate['battleHP']}/{mate['hpMax']}  ·  MP {mate['battleMP']}/{mate['mpMax']}  ·  智慧 {min(mate['wise'], 99)}"
            )
            self._mate_details[key] = mate
            self.selected_mate = mate
            return True
        except Exception as exc:
            self._invalidate_current_mate()
            self.status_var.set(f"同伴详情读取失败：{exc}；请重新选择或刷新")
            return False

    def collect_mate_changes(self):
        mate = self.selected_mate
        if mate is None:
            raise ValueError("请先选择同伴")
        changes = {"box": mate["box"], "index": mate["index"]}
        try:
            lv = int(self.mate_lv_var.get())
        except ValueError:
            raise ValueError("等级必须是整数")
        if lv < 1 or lv > MATE_LV_MAX:
            raise ValueError(f"等级必须在 1 到 {MATE_LV_MAX} 之间")
        changes["lv"] = lv
        star_text = self.mate_star_var.get()
        try:
            stars = int(star_text.replace("星", "").strip())
        except ValueError:
            raise ValueError("天赋星数无效")
        if stars < 1 or stars > MATE_RACIAL_MAX:
            raise ValueError(f"天赋必须在 1 到 {MATE_RACIAL_MAX} 星之间")
        changes["racialLv"] = stars - 1
        if self.mate_personality_var.get() not in MATE_PERSONALITIES:
            raise ValueError("请选择有效的性格")
        changes["personality"] = MATE_PERSONALITIES.index(self.mate_personality_var.get())
        if self.mate_attribute_var.get() not in MATE_ATTRIBUTES:
            raise ValueError("请选择有效的特征")
        changes["attribute"] = MATE_ATTRIBUTES.index(self.mate_attribute_var.get())
        try:
            equip_max = int(self.mate_equip_var.get())
        except ValueError:
            raise ValueError("装备插槽必须是整数")
        if equip_max < 0 or equip_max > MATE_EQUIP_MAX:
            raise ValueError(f"装备插槽必须在 0 到 {MATE_EQUIP_MAX} 之间")
        changes["equipMax"] = equip_max
        for name, label in MATE_PARAM_FIELDS:
            try:
                value = int(self.mate_param_vars[name].get())
            except ValueError:
                raise ValueError(f"{label}必须是整数")
            limit = 99 if name == "wise" else MATE_PARAM_LIMIT
            if value < 0 or value > limit:
                hint = "（智慧显示封顶 99）" if name == "wise" else "（更高会溢出战斗计算）"
                raise ValueError(f"{label}必须在 0 到 {limit} 之间{hint}")
            changes[name] = value
        return changes

    def apply_mate(self):
        if self.selected_mate is None:
            self.status_var.set("无法应用：请先刷新并选择同伴")
            return
        try:
            changes = self.collect_mate_changes()
        except ValueError as exc:
            self.status_var.set(f"无法应用：{exc}")
            return
        try:
            result = self.game.apply_mate_changes(changes)
            if self.replace_mate(result):
                self.status_var.set(f"已应用同伴修改（{result['name'].strip()}），已显示返回属性")
            else:
                self.status_var.set("同伴写入请求已返回，但详情刷新失败，请重新选择或刷新后确认")
        except Exception as exc:
            self._invalidate_current_mate()
            self.status_var.set(f"应用同伴修改失败：{exc}；可能已有部分写入，请刷新确认")

    def heal_mate(self):
        if self.selected_mate is None:
            self.status_var.set("无法恢复：请先刷新并选择同伴")
            return
        try:
            result = self.game.heal_mate(self.selected_mate["box"], self.selected_mate["index"])
            if self.replace_mate(result):
                mate = self.selected_mate
                self.status_var.set(
                    f"已执行恢复，返回 HP {mate['battleHP']}/{mate['hpMax']} · MP {mate['battleMP']}/{mate['mpMax']}"
                )
            else:
                self.status_var.set("恢复请求已返回，但详情刷新失败，请重新选择或刷新后确认")
        except Exception as exc:
            self._invalidate_current_mate()
            self.status_var.set(f"恢复失败：{exc}；请重新选择或刷新确认实际 HP/MP")

    def max_mate(self):
        if self.selected_mate is None:
            self.status_var.set("无法拉满：请先刷新并选择同伴")
            return
        if not messagebox.askyesno(
            "确认一键拉满",
            "将把当前同伴设为：等级 99、天赋 5 星、HP/MP 上限与攻/防/速/劳全部 9990000，智慧 99，并恢复满 HP/MP。\n"
            "这些数值会大幅降低游戏乐趣。确定继续？",
            parent=self,
        ):
            return
        applied = False
        try:
            mate = self.selected_mate
            changes = {
                "box": mate["box"], "index": mate["index"],
                "lv": MATE_LV_MAX, "racialLv": MATE_RACIAL_MAX - 1,
                "hpMax": MATE_PARAM_LIMIT, "mpMax": MATE_PARAM_LIMIT,
                "atk": MATE_PARAM_LIMIT, "defense": MATE_PARAM_LIMIT,
                "spd": MATE_PARAM_LIMIT, "work": MATE_PARAM_LIMIT, "wise": 99,
            }
            result = self.game.apply_mate_changes(changes)
            applied = True
            result.update(self.game.heal_mate(mate["box"], mate["index"]))
            if self.replace_mate(result):
                self.status_var.set("已执行一键拉满与恢复 HP/MP，已显示返回属性（智慧上限 99）")
            else:
                self.status_var.set("拉满与恢复请求已返回，但详情刷新失败，请重新选择或刷新确认")
        except Exception as exc:
            self._invalidate_current_mate()
            progress = "属性写入已返回，恢复或显示失败" if applied else "可能已有部分属性写入"
            self.status_var.set(f"一键拉满失败：{exc}；{progress}，请刷新确认")

    def replace_mate(self, mate):
        key = (mate["box"], mate["index"])
        for i, existing in enumerate(self.mates):
            if (existing["box"], existing["index"]) == key:
                self.mates[i] = mate
                break
        else:
            i = len(self.mates)
            self.mates.append(mate)
        self.selected_mate = mate
        self._mate_pick_key = key
        if not hasattr(self, "_mate_details"):
            self._mate_details = {}
        required = {name for name, _label in MATE_PARAM_FIELDS}
        required.update(("name", "lv", "racialLv", "equipMax", "personality", "attribute", "battleHP", "battleMP"))
        self._mate_details.pop(key, None)
        if required <= mate.keys():
            self._mate_details[key] = mate
        self.mate_selector.configure(values=[self.mate_display_name(existing) for existing in self.mates])
        self.mate_selector.current(i)
        return self.load_mate_form()

    def apply_resources(self):
        if self.state is None:
            self.status_var.set("无法应用：请先刷新读取数据")
            return
        locked = [dict(RESOURCE_FIELDS)[key] for key, _ in LOCKABLE_FIELDS if self.lock_vars[key].get()]
        if locked:
            self.status_var.set("无法应用：以下项目正在锁定中，手动修改会被覆盖，请先取消锁定：" + "、".join(locked))
            return
        changes = {}
        for key, label in RESOURCE_FIELDS:
            try:
                changes[key] = int(self.resource_vars[key].get())
            except ValueError:
                self.status_var.set(f"无法应用：{label}必须是整数")
                return
            if changes[key] < 0 or changes[key] > RESOURCE_LIMIT:
                self.status_var.set(f"无法应用：{label}必须在 0 到 {RESOURCE_LIMIT} 之间")
                return
        try:
            result = self.game.apply_changes(changes)
            self.update_state(result)
            if any(result.get(key) != value for key, value in changes.items()):
                self.status_var.set("资源写入返回值与目标不一致，已显示返回数值，请检查")
                return
            self.status_var.set("已应用资源修改，返回数值与目标一致")
        except Exception as exc:
            self._clear_resources()
            self.status_var.set(f"资源修改失败：{exc}；请重新刷新确认实际数值")

    def on_close(self):
        self._closing = True
        self._stop_lock_loop()
        for var in self.lock_vars.values():
            var.set(False)
        for name in ("_window_size_timer_id", "_startup_timer_id"):
            timer = getattr(self, name, None)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, name, None)
        self._capture_window_size()
        if not self.save_settings(show_error=False):
            if not messagebox.askyesno("设置保存失败", f"{self._settings_save_error}\n\n仍要退出吗？选择「否」可返回并重试保存。", parent=self):
                self._closing = False
                return
        self.game.close()
        self.destroy()


if __name__ == "__main__":
    app = TrainerApp()
    app.mainloop()
