"""实验：给(22,23)写特征值并轮询 param_ 是否被游戏回退。"""
import frida
import sys
import time

GAME_NAME = "KairoGames.exe"

SCRIPT = r"""
const game = Process.getModuleByName('GameAssembly.dll');
const POINTER = Process.pointerSize;
function fn(name, ret, args) {
  return new NativeFunction(game.getExportByName(name), ret, args, 'mscdecl');
}
const api = {
  domainGet: fn('il2cpp_domain_get', 'pointer', []),
  domainGetAssemblies: fn('il2cpp_domain_get_assemblies', 'pointer', ['pointer', 'pointer']),
  assemblyImage: fn('il2cpp_assembly_get_image', 'pointer', ['pointer']),
  classFromName: fn('il2cpp_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']),
  classFields: fn('il2cpp_class_get_fields', 'pointer', ['pointer', 'pointer']),
  classParent: fn('il2cpp_class_get_parent', 'pointer', ['pointer']),
  classMethod: fn('il2cpp_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']),
  runtimeInvoke: fn('il2cpp_runtime_invoke', 'pointer', ['pointer', 'pointer', 'pointer', 'pointer']),
  fieldName: fn('il2cpp_field_get_name', 'pointer', ['pointer']),
  fieldOffset: fn('il2cpp_field_get_offset', 'uint', ['pointer']),
  fieldStaticGet: fn('il2cpp_field_static_get_value', 'void', ['pointer', 'pointer']),
  objectClass: fn('il2cpp_object_get_class', 'pointer', ['pointer']),
  arrayLength: fn('il2cpp_array_length', 'uint', ['pointer']),
  threadAttach: fn('il2cpp_thread_attach', 'pointer', ['pointer']),
  threadDetach: fn('il2cpp_thread_detach', 'void', ['pointer']),
};
const domain = api.domainGet();
function text(p) { return p.isNull() ? null : p.readUtf8String(); }
function findClass(ns, nm) {
  const size = Memory.alloc(POINTER);
  const assemblies = api.domainGetAssemblies(domain, size);
  const nsp = Memory.allocUtf8String(ns), nmp = Memory.allocUtf8String(nm);
  for (let i = 0; i < size.readU32(); i++) {
    const image = api.assemblyImage(assemblies.add(i * POINTER).readPointer());
    const k = api.classFromName(image, nsp, nmp);
    if (!k.isNull()) return k;
  }
  throw new Error('not found ' + ns + '.' + nm);
}
const appClass = findClass('main', 'AppData');
const playerClass = findClass('game', 'Player');
const mapClass = findClass('game', 'Map');
const chipClass = findClass('game', 'MapChip');
const bldClass = findClass('game', 'Building');
function getField(klass, name) {
  let k = klass;
  while (!k.isNull()) {
    const it = Memory.alloc(POINTER); it.writePointer(NULL);
    for (;;) {
      const f = api.classFields(k, it);
      if (f.isNull()) break;
      if (text(api.fieldName(f)) === name) return f;
    }
    k = api.classParent(k);
  }
  throw new Error('no field ' + name);
}
function off(klass, name) { return api.fieldOffset(getField(klass, name)); }
function app() {
  const out = Memory.alloc(POINTER); out.writePointer(NULL);
  api.fieldStaticGet(getField(appClass, 'apdat_'), out);
  return out.readPointer();
}
function sane(p) {
  // 粗略指针健全性：用户态低 4GB、非空、按 4 对齐
  const v = p.toUInt32 ? p.toUInt32() : parseInt(p.toString(), 16);
  return v >= 0x10000 && v < 0x80000000 && (v & 3) === 0;
}
function tryReadPtr(addr) {
  try { return addr.readPointer(); } catch (e) { return NULL; }
}
function findTarget() {
  const maps = app().add(off(playerClass, 'map_')).readPointer();
  if (maps.isNull()) throw new Error('maps null');
  const n = api.arrayLength(maps);
  for (let m = 0; m < n; m++) {
    const map = tryReadPtr(maps.add(16 + m * POINTER));
    if (!sane(map)) continue;
    const grid = tryReadPtr(map.add(off(mapClass, 'mapchips_')));
    if (!sane(grid)) continue;
    let rows = 0;
    try { rows = api.arrayLength(grid); } catch (e) { continue; }
    if (rows <= 0 || rows > 512) continue;
    for (let y = 0; y < rows; y++) {
      const row = tryReadPtr(grid.add(16 + y * POINTER));
      if (!sane(row)) continue;
      let cols = 0;
      try { cols = api.arrayLength(row); } catch (e) { continue; }
      if (cols <= 0 || cols > 512) continue;
      for (let x = 0; x < cols; x++) {
        const chip = tryReadPtr(row.add(16 + x * POINTER));
        if (!sane(chip)) continue;
        const b = tryReadPtr(chip.add(off(chipClass, 'building_')));
        if (!sane(b)) continue;
        let isB = false;
        try { isB = api.objectClass(b).equals(bldClass); } catch (e) { isB = false; }
        if (!isB) continue;
        return {m: m, x: x, y: y, b: b};
      }
    }
  }
  throw new Error('no building anywhere');
}
let target = null;
function targetB() {
  if (target === null) throw new Error('call mark first');
  // 重新解析一次，防止对象被重建
  const t = findTarget();
  return t.b;
}
function readPraw(b) {
  const arr = b.add(off(bldClass, 'param_')).readPointer();
  if (arr.isNull()) return null;
  const n = api.arrayLength(arr);
  const r = [];
  for (let i = 0; i < n; i++) r.push(arr.add(16 + i * 2).readS16());
  return r;
}
const getParamMethod = api.classMethod(bldClass, Memory.allocUtf8String('GetParam'), 1);
function getParam(b, i) {
  const thread = api.threadAttach(domain);
  try {
    const argMem = Memory.alloc(4); argMem.writeS32(i);
    const args = Memory.alloc(POINTER); args.writePointer(argMem);
    const exc = Memory.alloc(POINTER); exc.writePointer(NULL);
    const r = api.runtimeInvoke(getParamMethod, b, args, exc);
    if (!exc.readPointer().isNull()) return '<threw>';
    return r.isNull() ? '<null>' : r.add(8).readS32();
  } finally { api.threadDetach(thread); }
}
rpc.exports = {
  mark: function () {
    const thread = api.threadAttach(domain);
    try {
      target = findTarget();
      const b = targetB();
      const before = readPraw(b);
      const arr = b.add(off(bldClass, 'param_')).readPointer();
      for (let i = 0; i < 4; i++) arr.add(16 + i * 2).writeS16((i + 1) * 1111);
      return {pos: [target.m, target.x, target.y], before: before, after: readPraw(b), display: [getParam(b,0), getParam(b,1), getParam(b,2), getParam(b,3)]};
    } finally { api.threadDetach(thread); }
  },
  poll: function () {
    const thread = api.threadAttach(domain);
    try {
      const b = targetB();
      return {praw: readPraw(b), display: [getParam(b,0), getParam(b,1), getParam(b,2), getParam(b,3)]};
    } finally { api.threadDetach(thread); }
  }
};
"""


def main():
    device = frida.get_local_device()
    process = next((p for p in device.enumerate_processes() if p.name.lower() == GAME_NAME.lower()), None)
    if process is None:
        print("game not running"); sys.exit(1)
    session = device.attach(process.pid)
    script = session.create_script(SCRIPT)
    script.load()
    print("mark:", script.exports_sync.mark())
    last = None
    for i in range(60):
        time.sleep(2)
        cur = script.exports_sync.poll()
        state = (tuple(cur["praw"] or []), tuple(cur["display"]))
        if state != last:
            print(f"t={2*(i+1)}s praw={cur['praw']} display={cur['display']}")
            last = state
    print("done")
    session.detach()


if __name__ == "__main__":
    main()
