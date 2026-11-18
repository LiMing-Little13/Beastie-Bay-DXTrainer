"""探针：挂钩 Building._initParam / GetParamRatio / Map.UpdataMapInfo，抓 param_ 重算时机。
配合 probe_param_revert 的轮询使用：先起本脚本，再 mark，观察日志。"""
import frida
import sys
import time

GAME_NAME = "KairoGames.exe"

SCRIPT = r"""
const game = Process.getModuleByName('GameAssembly.dll');
const POINTER = Process.pointerSize;
function fn(name, ret, args) {
  const a = game.getExportByName(name);
  return a.isNull() ? null : new NativeFunction(a, ret, args, 'mscdecl');
}
const api = {
  domainGet: fn('il2cpp_domain_get', 'pointer', []),
  domainGetAssemblies: fn('il2cpp_domain_get_assemblies', 'pointer', ['pointer', 'pointer']),
  assemblyImage: fn('il2cpp_assembly_get_image', 'pointer', ['pointer']),
  classFromName: fn('il2cpp_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']),
  classMethod: fn('il2cpp_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']),
  classFields: fn('il2cpp_class_get_fields', 'pointer', ['pointer', 'pointer']),
  classParent: fn('il2cpp_class_get_parent', 'pointer', ['pointer']),
  fieldName: fn('il2cpp_field_get_name', 'pointer', ['pointer']),
  fieldOffset: fn('il2cpp_field_get_offset', 'uint', ['pointer']),
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
const bldClass = findClass('game', 'Building');
const mapClass = findClass('game', 'Map');
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
const fbParamOff = api.fieldOffset(getField(bldClass, 'param_'));
function readPraw(b) {
  try {
    const arr = b.add(fbParamOff).readPointer();
    if (arr.isNull()) return null;
    const n = api.arrayLength(arr);
    const r = [];
    for (let i = 0; i < Math.min(n, 4); i++) r.push(arr.add(16 + i * 2).readS16());
    return r;
  } catch (e) { return '<av>'; }
}
// MethodInfo → 编译代码指针：逐个候选探测
function methodPtr(klass, name, argc) {
  const m = api.classMethod(klass, Memory.allocUtf8String(name), argc);
  if (m.isNull()) throw new Error('no method ' + name);
  for (let off = 0; off <= 24; off += POINTER) {
    const cand = m.add(off).readPointer();
    const v = parseInt(cand.toString(), 16);
    // GameAssembly 模块范围
    if (v > 0x10000000 && v < 0x30000000) return cand;
  }
  throw new Error('no code ptr for ' + name);
}
const hooks = [];
function hook(name, klass, argc, extra) {
  try {
    const p = methodPtr(klass, name, argc);
    const hit = {name: name, count: 0, samples: []};
    Interceptor.attach(p, {
      onEnter(args) {
        hit.count++;
        const self = args[0];
        const rec = {t: Date.now() % 1000000, self: self.toString(), praw: null};
        if (self.toString() !== '0x0') {
          try { rec.praw = readPraw(self); } catch (e) { }
        }
        this.rec = rec;
        if (hit.samples.length < 40) hit.samples.push(rec);
      },
      onLeave(retval) {
        if (this.rec && hit.samples.length < 40) {
          try { this.rec.after = readPraw(args[0]); } catch (e) { }
        }
      }
    });
    hooks.push(hit);
    return name + ' @' + p + ' hooked';
  } catch (e) { return name + ' FAIL: ' + e; }
}
const logs = [];
function logLine(s) { logs.push(s); if (logs.length > 200) logs.shift(); }
rpc.exports = {
  setup: function () {
    const out = [];
    out.push(hook('_initParam', bldClass, 3));
    out.push(hook('GetParamRatio', bldClass, 1));
    out.push(hook('CheckHarvest', bldClass, 0));
    out.push(hook('UpdataMapInfo', mapClass, 1));
    out.push(hook('Updata', mapClass, 0));
    return out.join('\n');
  },
  drain: function () {
    const out = logs.slice();
    logs.length = 0;
    for (const h of hooks) {
      if (h.count > 0) { out.push('HOOK ' + h.name + ' x' + h.count); h.count = 0; }
    }
    return out.join('\n');
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
    script.on("message", lambda m, d: print("[js]", m.get("payload", m)) if m.get("type") == "send" else None)
    script.load()
    print(script.exports_sync.setup())
    print("hooks armed; watching 150s (run mark/poll probe in another shell)...")
    for _ in range(30):
        time.sleep(5)
        d = script.exports_sync.drain()
        if d:
            print(d)
    session.detach()


if __name__ == "__main__":
    main()
