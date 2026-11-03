#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore_ops.py —— 把精确恢复的表格放回 NAS 原目录，并补齐属主与 Synology ACL。

安全约束（与 skill 一致）：
  * 只新增，绝不覆盖、不删除、不改动任何 .L0CK3D 密文；
  * 写临时文件后 rename 落位，落位后立刻重读校验 SHA-256；
  * 属主/权限/ACL 顺序：chown → chmod → synoacltool -copy →（必要时）synoacltool -add administrators；
  * 出台账 JSON（来源、字节数、SHA-256、验证方式）。

用法:
  python3 restore_ops.py --src <恢复件目录> --dst <NAS 目标目录> [--uid 1028 --gid 100]
                         [--map "<密文原名>=<恢复件名>"] [--ledger /tmp/ledger.json]
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ACLTOOL = "/usr/syno/bin/synoacltool"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def acl_get(path):
    r = subprocess.run([ACLTOOL, "-get", path], capture_output=True)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def has_admin_acl(text):
    return "group:administrators" in text


def copy_acl(src, dst):
    return subprocess.run([ACLTOOL, "-copy", src, dst], capture_output=True).returncode


def add_admin_acl(path):
    r = subprocess.run([ACLTOOL, "-add", path,
                        "group:administrators:allow:rwxpdDaARWc--:----"], capture_output=True)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def pick_acl_template(dst_dir):
    """同目录里的原始密文文件当 ACL 模板（它带 Synology ACL）。"""
    for fn in sorted(os.listdir(dst_dir)):
        if fn.endswith(".L0CK3D"):
            p = os.path.join(dst_dir, fn)
            t = acl_get(p)
            if "ACL version" in t and "It's Linux mode" not in t:
                return p
    return None


def place(src_file, dst_path, uid, gid, tpl, ledger, note):
    name = os.path.basename(dst_path)
    if os.path.exists(dst_path):
        print("跳过（已存在，不覆盖）: %s" % dst_path)
        ledger.append({"dst": dst_path, "status": "skipped_exists"})
        return False
    tmp = dst_path + ".tmp_restore"
    shutil.copy2(src_file, tmp)
    # 属主 → 权限 → ACL（顺序不能反：chmod 会把 ACL 打回 Linux mode）
    try:
        os.chown(tmp, uid, gid)
    except OSError as e:
        print("  chown 失败: %s" % e)
    os.chmod(tmp, 0o640)
    if tpl:
        rc = copy_acl(tpl, tmp)
        print("  ACL 复制 rc=%d (模板 %s)" % (rc, os.path.basename(tpl)))
    t = acl_get(tmp)
    if "ACL version" not in t or "It's Linux mode" in t:
        print("  ! 仍无 Synology ACL")
    elif not has_admin_acl(t):
        rc, out = add_admin_acl(tmp)
        print("  补 administrators 条目 rc=%d %s" % (rc, out.strip()[:80]))
    os.rename(tmp, dst_path)
    # 回读校验
    s_src = sha256(src_file)
    s_dst = sha256(dst_path)
    ok = (s_src == s_dst)
    print("  落位 %s  %d 字节  sha256 %s" % (name, os.path.getsize(dst_path), "一致" if ok else "不一致!"))
    ledger.append({
        "dst": dst_path, "src": src_file, "size": os.path.getsize(dst_path),
        "sha256": s_dst, "sha_match": ok, "verification": note, "time": int(time.time()),
    })
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--uid", type=int, default=1028)
    ap.add_argument("--gid", type=int, default=100)
    ap.add_argument("--map", action="append", default=[],
                    help="<目标文件名>=<恢复件文件名>，可重复")
    ap.add_argument("--ledger", default="/tmp/c3r_ledger.json")
    ap.add_argument("--note", default="exact")
    args = ap.parse_args()

    os.makedirs(args.dst, exist_ok=True)
    tpl = pick_acl_template(args.dst)
    print("ACL 模板: %s" % (tpl or "无"))
    ledger = []
    for m in args.map:
        if "=" not in m:
            continue
        dst_name, src_name = m.split("=", 1)
        src = os.path.join(args.src, src_name)
        if not os.path.isfile(src):
            print("恢复件缺失，跳过: %s" % src)
            continue
        place(src, os.path.join(args.dst, dst_name), args.uid, args.gid, tpl, ledger, args.note)
    with open(args.ledger, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    print("台账: %s（%d 条）" % (args.ledger, len(ledger)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
