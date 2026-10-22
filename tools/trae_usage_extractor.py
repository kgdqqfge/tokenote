# -*- coding: utf-8 -*-
"""
Trae CN ai-agent token 用量提取器
================================
数据源: %APPDATA%\\Trae CN\\ModularData\\ai-agent\\database.db (SQLCipher 加密 SQLite)

原理:
  1. 从加密库文件头读取 salt (前16字节)
  2. 在正在运行的 Trae 进程内存中定位 salt, 在其附近暴力验证 32 字节窗口
     找到 SQLCipher 派生密钥 (AES-256-CBC, 每页 IV 存于页尾保留区)
  3. 解密主库 + WAL, 得到明文 SQLite
  4. 读取用量记录

关键表:
  server_history_info  -- 每次 LLM 请求一条, extra_info JSON 含精确用量(厂商原始口径):
       config_name                        模型ID (如 glm-5.3-flash)
       exact_prompt_tokens_v1             输入
       exact_output_tokens_v1             输出
       exact_cache_read_input_tokens_v1   缓存读
       exact_total_tokens_v1              总量
       exact_token_semantics_v1           口径说明 (provider_raw_usage_completion_includes_reasoning)
  chat_turn            -- 每轮对话, context JSON 含:
       token_usage.{prompt_tokens, completion_tokens, reasoning_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens}
       persist_user_message_context.model_info.{config_name, display_model_name}

用法:
  python trae_usage_extractor.py                 # 按模型汇总
  python trae_usage_extractor.py --by day        # 按天汇总
  python trae_usage_extractor.py --json out.json # 导出逐请求明细
  python trae_usage_extractor.py --save-key key.bin   # 缓存密钥供离线使用
  python trae_usage_extractor.py --key-file key.bin   # 用缓存密钥(无需 Trae 运行)

依赖: pip install cryptography
"""
import argparse, ctypes, ctypes.wintypes as wt, json, os, shutil, sqlite3
import struct, sys, tempfile, datetime

PAGE = 4096
RESERVE = 80          # 页尾: IV(16) + HMAC(64)
DB_DIR = os.path.join(os.environ.get("APPDATA", ""), "Trae CN", "ModularData", "ai-agent")
DB = os.path.join(DB_DIR, "database.db")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# ---------- 1. 密钥提取 ----------

def find_trae_pid():
    """找到加载了 ai_agent.dll 的 Trae CN.exe 进程"""
    psapi = ctypes.windll.psapi
    k32 = ctypes.windll.kernel32
    arr = (wt.DWORD * 4096)()
    needed = wt.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
        return None
    n = needed.value // ctypes.sizeof(wt.DWORD)
    PROCESS_QI = 0x0400
    for i in range(n):
        pid = arr[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_QI, False, pid)
        if not h:
            continue
        try:
            name = ctypes.create_unicode_buffer(260)
            if k32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(wt.DWORD(260))):
                if not name.value.lower().endswith("trae cn.exe"):
                    continue
            mods = (wt.HMODULE * 2048)()
            cb = wt.DWORD()
            h2 = k32.OpenProcess(PROCESS_QI | 0x0010, False, pid)  # +VM_READ
            if h2:
                try:
                    if psapi.EnumProcessModulesEx(h2, ctypes.byref(mods), ctypes.sizeof(mods),
                                                  ctypes.byref(cb), 0x03):
                        cnt = cb.value // ctypes.sizeof(wt.HMODULE)
                        for j in range(cnt):
                            mn = ctypes.create_unicode_buffer(260)
                            psapi.GetModuleFileNameExW(h2, wt.HMODULE(mods[j]), mn, 260)
                            if mn.value.lower().endswith("ai_agent.dll"):
                                return pid
                finally:
                    k32.CloseHandle(h2)
        finally:
            k32.CloseHandle(h)
    return None


def extract_key_from_memory(salt: bytes, verify) -> bytes | None:
    """扫描进程内存: 在 salt 出现位置附近暴力测试 32 字节窗口"""
    pid = find_trae_pid()
    if not pid:
        raise RuntimeError("未找到加载 ai_agent.dll 的 Trae CN.exe 进程, 请确认 Trae 正在运行")
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        raise RuntimeError(f"OpenProcess({pid}) 失败 err={ctypes.GetLastError()}")

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("PartitionId", wt.DWORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                    ("Protect", wt.DWORD), ("Type", wt.DWORD)]

    READABLE = {0x02, 0x04, 0x20, 0x40, 0x08}
    addr, hits = 0, []
    mbi = MBI()
    while addr < 0x7FFFFFFEFFFF:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0x1000
        if mbi.State == 0x1000 and mbi.Protect in READABLE and mbi.Type in (0x20000, 0x1000000):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t()
            if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)) and got.value:
                data = buf.raw[:got.value]
                idx = data.find(salt)
                while idx != -1 and len(hits) < 50:
                    hits.append(data[max(0, idx - 1024): idx + 1024])
                    idx = data.find(salt, idx + 1)
        nxt = base + size
        if nxt <= base:
            break
        addr = nxt
    k32.CloseHandle(h)

    for ctx in hits:
        for off in range(0, len(ctx) - 32):
            cand = ctx[off:off + 32]
            if verify(cand):
                return cand
    return None


# ---------- 2. 解密 ----------

def decrypt_db(key: bytes, out_path: str):
    def dec(ct, iv):
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return d.update(ct) + d.finalize()

    def dec_page(pgno, pg):
        iv = pg[PAGE - RESERVE: PAGE - RESERVE + 16]
        if pgno == 1:
            pt = dec(pg[16:PAGE - RESERVE], iv)
            return b"SQLite format 3\x00" + pt[:PAGE - 96] + b"\x00" * RESERVE
        return dec(pg[:PAGE - RESERVE], iv) + b"\x00" * RESERVE

    data = open(DB, "rb").read()
    pages = {i + 1: dec_page(i + 1, data[i * PAGE:(i + 1) * PAGE])
             for i in range(len(data) // PAGE)}

    wal_path = DB + "-wal"
    if os.path.exists(wal_path):
        wal = open(wal_path, "rb").read()
        if len(wal) > 32:
            magic, _, pgsz, _, s1, s2, _, _ = struct.unpack(">8I", wal[:32])
            if magic in (0x377F0682, 0x377F0683) and pgsz == PAGE:
                off, frames, last_commit = 32, [], 0
                while off + 24 + pgsz <= len(wal):
                    pgno, dbsize, fs1, fs2, _, _ = struct.unpack(">6I", wal[off:off + 24])
                    if fs1 == s1 and fs2 == s2:
                        frames.append((pgno, dbsize, wal[off + 24:off + 24 + pgsz]))
                        if dbsize:
                            last_commit = len(frames)
                    off += 24 + pgsz
                for pgno, _, fdata in frames[:last_commit]:
                    pages[pgno] = dec_page(pgno, fdata)

    with open(out_path, "wb") as f:
        for i in range(1, max(pages) + 1):
            f.write(pages.get(i, b"\x00" * PAGE))


# ---------- 3. 用量读取 ----------

def open_plain(key: bytes) -> str:
    out = os.path.join(tempfile.gettempdir(), "trae_ai_usage_plain.db")
    decrypt_db(key, out)
    return out


def fetch_request_records(con):
    """逐请求精确用量 (server_history_info.extra_info)"""
    rows = con.execute(
        "SELECT extra_info, created_at FROM server_history_info "
        "WHERE extra_info LIKE '%exact_prompt_tokens_v1%'").fetchall()
    recs = []
    for e, ts in rows:
        try:
            o = json.loads(e)
            recs.append({
                "time": datetime.datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds"),
                "model": o.get("config_name"),
                "prompt_tokens": int(o.get("exact_prompt_tokens_v1") or 0),
                "output_tokens": int(o.get("exact_output_tokens_v1") or 0),
                "cache_read_tokens": int(o.get("exact_cache_read_input_tokens_v1") or 0),
                "total_tokens": int(o.get("exact_total_tokens_v1") or 0),
                "semantics": o.get("exact_token_semantics_v1"),
                "workspace": o.get("workspace_path"),
            })
        except Exception:
            continue
    return recs


def fetch_turn_records(con):
    """逐轮用量 (chat_turn.context), 含显示名模型"""
    rows = con.execute(
        "SELECT session_id, context, created_at FROM chat_turn "
        "WHERE context LIKE '%token_usage%' AND deleted_at=0").fetchall()
    recs = []
    for sid, ctx, ts in rows:
        try:
            o = json.loads(ctx)
            tu = o.get("token_usage") or {}
            mi = (o.get("persist_user_message_context") or {}).get("model_info") or {}
            recs.append({
                "time": datetime.datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds"),
                "session_id": sid,
                "model": mi.get("config_name"),
                "display_name": mi.get("display_model_name"),
                "prompt_tokens": tu.get("prompt_tokens") or 0,
                "output_tokens": tu.get("completion_tokens") or 0,
                "reasoning_tokens": tu.get("reasoning_tokens") or 0,
                "cache_read_tokens": tu.get("cache_read_input_tokens") or 0,
                "cache_write_tokens": tu.get("cache_creation_input_tokens") or 0,
                "total_tokens": tu.get("total_tokens") or 0,
            })
        except Exception:
            continue
    return recs


def verify_key(key: bytes, salt_check: bool = True) -> bool:
    try:
        with open(DB, "rb") as f:
            pg1 = f.read(PAGE)
        d = Cipher(algorithms.AES(key), modes.CBC(pg1[PAGE - RESERVE:PAGE - RESERVE + 16])).decryptor()
        pt = d.update(pg1[16:PAGE - RESERVE]) + d.finalize()
        return pt[:2] == b"\x10\x00" and pt[2] in (1, 2) and pt[4] == RESERVE
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Trae CN token 用量提取")
    ap.add_argument("--by", choices=["model", "day"], default="model")
    ap.add_argument("--json", help="导出逐请求明细 JSON 路径")
    ap.add_argument("--key-file", help="离线密钥文件(32字节)")
    ap.add_argument("--save-key", help="把提取到的密钥保存到该文件")
    ap.add_argument("--turns", action="store_true", help="输出逐轮(chat_turn)明细而非逐请求")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"未找到加密库: {DB}")

    key = None
    if args.key_file and os.path.exists(args.key_file):
        key = open(args.key_file, "rb").read()
        if len(key) != 32 or not verify_key(key):
            sys.exit("key-file 中的密钥无效")
    else:
        key = extract_key_from_memory(open(DB, "rb").read(16), verify_key)
        if not key:
            sys.exit("内存中未找到有效密钥 (确认 Trae 正在运行且加载了 ai-agent)")
        print(f"[+] 密钥: {key.hex()}")
        if args.save_key:
            open(args.save_key, "wb").write(key)
            print(f"[+] 密钥已保存: {args.save_key}")

    plain = open_plain(key)
    con = sqlite3.connect(plain)
    recs = fetch_turn_records(con) if args.turns else fetch_request_records(con)
    con.close()
    print(f"[+] 记录数: {len(recs)}")

    if args.json:
        json.dump(recs, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[+] 明细已导出: {args.json}")

    agg = {}
    for r in recs:
        k = r["model"] or "?" if args.by == "model" else str(r["time"])[:10]
        a = agg.setdefault(k, [0, 0, 0, 0, 0])
        a[0] += r["prompt_tokens"]; a[1] += r["output_tokens"]
        a[2] += r["cache_read_tokens"]; a[3] += r["total_tokens"]; a[4] += 1

    print(f"\n{'== 按' + args.by + '汇总 ==':^80}")
    if args.by == "model":
        print(f"{'model':38s} {'reqs':>6s} {'prompt':>14s} {'output':>10s} {'cache_read':>13s} {'total':>13s}")
        items = sorted(agg.items(), key=lambda x: -x[1][3])
    else:
        print(f"{'date':12s} {'reqs':>6s} {'prompt':>14s} {'output':>10s} {'cache_read':>13s} {'total':>13s}")
        items = sorted(agg.items())
    for k, (p, o, cr, t, n) in items:
        print(f"{k:38s} {n:6d} {p:14,d} {o:10,d} {cr:13,d} {t:13,d}")

    try:
        os.remove(plain)
    except OSError:
        pass


if __name__ == "__main__":
    main()
