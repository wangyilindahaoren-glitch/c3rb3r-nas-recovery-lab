#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""progress.py —— 恢复进度记录器：每轮开工落 PROGRESS.md，收尾结项。

为什么要它
----------
一次恢复动辄几小时、跨多个会话、还夹着几 GB 的后台任务。
只把进度记在脑子里 / 对话里，换个会话就断了。
本脚本把「当前在做什么、已经拿到多少、下一步是什么」写进一个
**人可读 + 机器可读**的 `PROGRESS.md`，随时 `status` 读回来接着干。

设计
----
* 只用标准库（NAS 端 DSM 自带 Python 3.8 就能跑）。
* 文件结构固定，段落之间用固定标题分隔，方便脚本改写、也方便人读。
* 元数据放在一段 HTML 注释里，Markdown 渲染器看不见，但脚本能解析。
* 幂等：重复 `stage` 只是覆盖当前阶段，不会重复追加日志。

用法
----
    # 开工：建一份进度表（已存在就保留旧内容并续写）
    python3 progress.py init  --dir /work --target "用户A home 的 624 个密文" \
            --nas "群晖/DSM7.2.1/14.5TiB" --g 9324851 --note "先走路线C"

    # 记一条日志
    python3 progress.py log   --dir /work "已盘出 624 个密文，mode 分布 484/99/41"

    # 切阶段（切到哪一步，前面未完成的阶段自动勾掉）
    python3 progress.py stage --dir /work --name "路线D 雕刻扫描"

    # 记一个数字
    python3 progress.py metric --dir /work --name "已装回原件" --value 142

    # 读回当前进度（换会话/换人接手时用这个）
    python3 progress.py status --dir /work

    # 结项
    python3 progress.py close --dir /work --result "救回 459/483 原件，其余走路线C"
"""
import argparse
import io
import os
import re
import sys
import time

PROGRESS_NAME = 'PROGRESS.md'
META_BEGIN = '<!-- c3r-progress-meta'
META_END = '-->'

# 标准工作流阶段（init 时生成清单；stage 按名字匹配）
STAGES = [
    '0 分流与密钥窗口',
    '1 preflight 环境自检',
    '2 env 卷信息',
    '3 gens G 值',
    '4 survey 普查',
    '5 路线A 导出旧 extent',
    '6 路线B 明文提取',
    '7 路线C 找副本',
    '8 路线D 雕刻',
    '9 验证校验',
    '10 装回原位',
    '11 交付前闸门',
    '12 收尾清理',
    '13 收尾回写 skill',
]

# 各级标题（写文件时按这个顺序拼；**不带** "## " 前缀）
H_TARGET = '目标'
H_ENV = '环境'
H_STAGE = '阶段'
H_METRIC = '关键数字'
H_LOG = '日志'
H_NEXT = '下一步'
H_RESULT = '结果'


def now():
    return time.strftime('%Y-%m-%d %H:%M')


def path_of(args):
    if args.file:
        return args.file
    d = args.dir or '.'
    return os.path.join(d, PROGRESS_NAME)


def parse_meta(text):
    """从 PROGRESS.md 里解出元数据字典。"""
    meta = {}
    m = re.search(re.escape(META_BEGIN) + r'(.*?)' + re.escape(META_END),
                  text, re.S)
    if not m:
        return meta
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        k, v = line.split(':', 1)
        meta[k.strip()] = v.strip()
    return meta


def render_meta(meta):
    lines = [META_BEGIN]
    for k in ('author', 'round', 'state', 'stage', 'started', 'updated'):
        if k in meta:
            lines.append('%s: %s' % (k, meta[k]))
    lines.append(META_END)
    return '\n'.join(lines)


def split_sections(text):
    """把文件拆成 {标题: 正文}。没有标题的散字归到 ''。

    开头那段 HTML 注释（元数据）先摘掉，不混进正文。
    """
    text = re.sub(re.escape(META_BEGIN) + r'.*?' + re.escape(META_END),
                  '', text, flags=re.S)
    secs = {}
    cur = ''
    buf = []
    for line in text.splitlines():
        if line.startswith('## '):
            secs[cur] = '\n'.join(buf).strip('\n')
            cur = line[3:].strip()
            buf = []
        elif line.startswith('# '):
            secs[cur] = '\n'.join(buf).strip('\n')
            cur = '\x00title'
            buf = []
        else:
            buf.append(line)
    secs[cur] = '\n'.join(buf).strip('\n')
    return secs


def join_sections(title, secs):
    order = ['\x00title', H_TARGET, H_ENV, H_STAGE, H_METRIC, H_RESULT, H_NEXT, H_LOG]
    out = []
    for key in order:
        if key in secs:
            if key == '\x00title':
                out.append(secs[key].strip() or title)
            else:
                out.append('## ' + key)
                out.append(secs[key])
    for key, val in secs.items():
        if key not in order and val.strip():
            out.append('## ' + key)
            out.append(val)
    return '\n\n'.join(x for x in out if x.strip()) + '\n'


def load(path):
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding='utf-8') as f:
        return f.read()


def save(path, text):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def add_log(secs, msg):
    log = secs.get(H_LOG, '')
    line = '- [%s] %s' % (now(), msg)
    secs[H_LOG] = (log + '\n' + line).strip('\n') if log else line


def cmd_init(args):
    path = path_of(args)
    old = load(path)
    rounds = 1
    if old:
        secs = split_sections(old)
        meta = parse_meta(old)
        title = '# 恢复进度'
        for k, v in secs.items():
            if k == '\x00title' and v.strip():
                title = v.strip()
        rounds = int(meta.get('round', '1') or 1) + 1
        print('[progress] 已存在进度表，续写（第 %d 轮）: %s' % (rounds, path))
    else:
        secs = {'\x00title': '# 恢复进度'}
        meta = {}
        title = '# 恢复进度'

    meta.setdefault('author', args.author)
    meta.setdefault('started', now())
    meta['round'] = rounds
    meta['state'] = '进行中'
    meta['stage'] = args.stage or STAGES[0]
    meta['updated'] = now()

    if args.target:
        secs[H_TARGET] = args.target
    if args.nas:
        env = secs.get(H_ENV, '')
        env = (env + '\n' if env else '') + ('| 项 | 值 |\n|---|---|\n| NAS | %s |' % args.nas)
        secs[H_ENV] = env
    if args.g:
        env = secs.get(H_ENV, '')
        if '| G 值 |' not in env:
            env = env + ('\n| G 值 | %s |' % args.g if env.startswith('|') else
                         '\n| 项 | 值 |\n|---|---|\n| G 值 | %s |' % args.g)
        secs[H_ENV] = env

    # 阶段清单：新轮次一律重置成未完成（历史在日志里）
    secs[H_STAGE] = '\n'.join('- [ ] %s' % s for s in STAGES)

    if rounds > 1:
        add_log(secs, '第 %d 轮开工：%s' % (rounds, args.note or args.target or '（无备注）'))
    elif args.note:
        add_log(secs, '开工：%s' % args.note)
    else:
        add_log(secs, '开工：创建进度表')

    secs[H_NEXT] = args.next or '（待填：下一步要做什么）'

    text = render_meta(meta) + '\n\n' + join_sections(title, secs)
    save(path, text)
    print('[progress] 已写入 %s' % path)
    print('[progress] 当前阶段: %s' % meta.get('stage'))
    return 0


def cmd_stage(args):
    path = path_of(args)
    text = load(path)
    if not text:
        print('[progress] 还没有进度表，先跑 init', file=sys.stderr)
        return 2
    secs = split_sections(text)
    meta = parse_meta(text)

    body = secs.get(H_STAGE, '')
    lines = body.splitlines()
    target = args.name
    # 三态勾选：目标之前的阶段 -> [x] 已完成；目标本身 -> [-] 进行中；之后 -> [ ] 未开始
    hit = False
    new_lines = []
    for ln in lines:
        if target and target in ln and not hit:
            hit = True
            new_lines.append(re.sub(r'- \[[ x-]\]', '- [-]', ln, count=1))
        elif hit:
            new_lines.append(ln)                 # 目标之后的阶段保持原样
        elif ln.strip().startswith('- ['):
            new_lines.append(re.sub(r'- \[.\]', '- [x]', ln, count=1))
        else:
            new_lines.append(ln)
    if target and not hit:
        new_lines.append('- [-] %s' % target)
    secs[H_STAGE] = '\n'.join(new_lines)

    meta['stage'] = target or meta.get('stage', '')
    meta['updated'] = now()
    add_log(secs, '进入阶段：%s%s' % (target, ('（%s）' % args.note) if args.note else ''))
    if args.next:
        secs[H_NEXT] = args.next

    save(path, render_meta(meta) + '\n\n' + join_sections('# 恢复进度', secs))
    print('[progress] 当前阶段: %s' % meta.get('stage'))
    return 0


def cmd_log(args):
    path = path_of(args)
    text = load(path)
    if not text:
        print('[progress] 还没有进度表，先跑 init', file=sys.stderr)
        return 2
    secs = split_sections(text)
    meta = parse_meta(text)
    meta['updated'] = now()
    add_log(secs, args.message)
    if args.next:
        secs[H_NEXT] = args.next
    save(path, render_meta(meta) + '\n\n' + join_sections('# 恢复进度', secs))
    print('[progress] 已追加日志: %s' % args.message)
    return 0


def cmd_metric(args):
    path = path_of(args)
    text = load(path)
    if not text:
        print('[progress] 还没有进度表，先跑 init', file=sys.stderr)
        return 2
    secs = split_sections(text)
    meta = parse_meta(text)
    meta['updated'] = now()

    body = secs.get(H_METRIC, '')
    lines = body.splitlines()
    new_lines = []
    found = False
    for ln in lines:
        if ln.startswith('| ') and args.name in ln:
            found = True
            new_lines.append('| %s | %s |' % (args.name, args.value))
        else:
            new_lines.append(ln)
    if not found:
        if not any(l.startswith('| 指标 |') for l in new_lines):
            new_lines = ['| 指标 | 值 |', '|---|---|'] + new_lines
        new_lines.append('| %s | %s |' % (args.name, args.value))
    secs[H_METRIC] = '\n'.join(new_lines)

    add_log(secs, '%s = %s' % (args.name, args.value))
    save(path, render_meta(meta) + '\n\n' + join_sections('# 恢复进度', secs))
    print('[progress] 指标已更新: %s = %s' % (args.name, args.value))
    return 0


def cmd_status(args):
    path = path_of(args)
    text = load(path)
    if not text:
        print('[progress] 还没有进度表: %s' % path, file=sys.stderr)
        return 2
    meta = parse_meta(text)
    secs = split_sections(text)
    print('=== 进度 %s ===' % path)
    print('状态   : %s' % meta.get('state', '?'))
    print('阶段   : %s' % meta.get('stage', '?'))
    print('开始   : %s' % meta.get('started', '?'))
    print('更新   : %s' % meta.get('updated', '?'))
    print('作者   : %s' % meta.get('author', '?'))
    if secs.get(H_TARGET):
        print('目标   : %s' % secs[H_TARGET].splitlines()[0])
    body = secs.get(H_METRIC, '')
    if body.strip():
        print('--- 关键数字 ---')
        for ln in body.splitlines():
            if ln.startswith('|'):
                print('  ' + ln)
    stage_body = secs.get(H_STAGE, '')
    if stage_body.strip():
        done = [l for l in stage_body.splitlines() if '[x]' in l]
        doing = [l for l in stage_body.splitlines() if '[-]' in l]
        todo = [l for l in stage_body.splitlines() if '[ ]' in l]
        print('--- 阶段 (%d/%d 完成) ---' % (len(done), len(done) + len(doing) + len(todo)))
        for ln in done[-3:]:
            print('  ✓ ' + ln.lstrip('- [x] ').strip())
        for ln in doing:
            print('  ▶ ' + ln.lstrip('- [-] ').strip())
        for ln in todo[:4]:
            print('  · ' + ln.lstrip('- [ ] ').strip())
    if secs.get(H_NEXT):
        print('--- 下一步 ---')
        print('  ' + secs[H_NEXT].replace('\n', '\n  '))
    print('--- 最近日志 ---')
    for ln in secs.get(H_LOG, '').splitlines()[-args.tail:]:
        print('  ' + ln)
    return 0


def cmd_close(args):
    path = path_of(args)
    text = load(path)
    if not text:
        print('[progress] 还没有进度表', file=sys.stderr)
        return 2
    secs = split_sections(text)
    meta = parse_meta(text)
    meta['state'] = '已完成'
    meta['stage'] = '13 收尾回写 skill'
    meta['updated'] = now()
    # 结项 = 所有阶段收尾
    stage_body = secs.get(H_STAGE, '')
    secs[H_STAGE] = '\n'.join(
        re.sub(r'- \[[ x-]\]', '- [x]', l, count=1) if l.strip().startswith('- [') else l
        for l in stage_body.splitlines())
    add_log(secs, '结项：%s' % (args.result or '（未填结果）'))
    secs[H_RESULT] = args.result or '（未填结果）'
    secs[H_NEXT] = '无（已结项）'
    save(path, render_meta(meta) + '\n\n' + join_sections('# 恢复进度', secs))
    print('[progress] 已结项: %s' % path)
    print('[progress] 别忘了执行第 14 步「收尾回写 skill」')
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description='恢复进度记录器（PROGRESS.md）')
    ap.add_argument('--file', default='', help='直接指定 PROGRESS.md 路径（默认 <dir>/PROGRESS.md）')
    ap.add_argument('--dir', default='.', help='工作目录')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('init', help='开工建表')
    p.add_argument('--target', default='', help='目标描述')
    p.add_argument('--nas', default='', help='NAS 环境描述')
    p.add_argument('--g', default='', help='G 值')
    p.add_argument('--stage', default='', help='起始阶段')
    p.add_argument('--note', default='', help='开工备注')
    p.add_argument('--next', default='', help='「下一步」写什么')
    p.add_argument('--author', default='wangyilin', help='作者署名')
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('stage', help='切换当前阶段')
    p.add_argument('--name', required=True, help='阶段名（子串匹配即可）')
    p.add_argument('--note', default='', help='备注')
    p.add_argument('--next', default='', help='同时更新「下一步」')
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser('log', help='追加一条日志')
    p.add_argument('message')
    p.add_argument('--next', default='', help='同时更新「下一步」')
    p.set_defaults(func=cmd_log)

    p = sub.add_parser('metric', help='记一个关键数字')
    p.add_argument('--name', required=True)
    p.add_argument('--value', required=True)
    p.set_defaults(func=cmd_metric)

    p = sub.add_parser('status', help='读回当前进度')
    p.add_argument('--tail', type=int, default=8, help='显示最近几条日志')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('close', help='结项')
    p.add_argument('--result', default='', help='结果摘要')
    p.set_defaults(func=cmd_close)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, 'func', None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
