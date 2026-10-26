#!/usr/bin/env python3
"""交付门控：先核对任务类型，再判是否够格交付。

对应 SKILL.md 第零节的三道闸，脚本负责第一道和第三道（第二道是人和模型对骨架的确认）。

  闸一｜类型闸——不告诉它文种，它反过来问你
      python scripts/gate.py 草稿.md

  闸三｜交付闸——给了文种，逐项验收
      python scripts/gate.py 草稿.md --genre 调研报告
      python scripts/gate.py 草稿.md --genre 批复 --draft   # 草稿态：允许留占位符

退出码：
  0  可交付（只剩需要人签字的几项）
  1  有硬伤，必须改
  2  类型没定下来，先回答闸一的问题
  3  还有没填的占位符，不算定稿

设计取舍：
  凡是脚本判不了的，一律标成"人审"而不是"通过"。门控的意义是把关，
  不是给一份假的安全感——事实准不准、口径对不对、涉不涉密，机器永远判不了。
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_params import (analyze, match_genre, REF, HARD,  # noqa: E402
                          NO_PARAM, ALIAS, BAD_WORDS, LAW_OPEN, LAW_LAW)

PASS, WARN, FAIL, HUMAN, BLOCK = '✓', '!', '✗', '…', '□'

# 避坑词分两档：
#   必改——用错就是不会写公文，任何文种都不该出现
#   看语境——"相关部门""非常"在讲话、引语里是常见的，硬判错会误伤
HARD_BAD = ('我觉得', '我认为', '希望领导重视', '取得了显著成效')
# 真件里「进一步」「切实」每万字约两次，超了就是拿虚词填空
WARN_WORDS = ('进一步', '切实', '努力', '高度重视', '狠抓', '有效地')
WARN_WORDS_BUDGET = 3  # 每万字允许次数


def read(path):
    try:
        return Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        sys.exit(f'找不到文件：{path}')
    except IsADirectoryError:
        sys.exit(f'这是目录，不是文件：{path}')
    except UnicodeDecodeError:
        sys.exit(f'无法按 UTF-8 读取：{path}（先转成 UTF-8 纯文本）')


def first_prose(text):
    """篇名：跳过引用块（示例篇首是披露块）、分隔线，Markdown 标题符要去掉而不是跳过。"""
    for l in text.split('\n'):
        s = l.strip()
        if not s or s.startswith('>') or set(s) <= set('-—=*_ '):
            continue
        return s.lstrip('#').strip()
    return ''


def strip_quotes(text):
    """只留正文。Markdown 引用块（`>`）是批注，不是稿子——
    示例文件篇首的披露块里会引用「相关部门」这种反面例子，
    连它一起扫，门控就会误报。"""
    return '\n'.join(l for l in text.split('\n') if not l.lstrip().startswith('>'))


# ── 闸一：类型 ────────────────────────────────────────────────

def gate_type(text, a):
    print('\n' + '=' * 72)
    print('  闸一｜类型闸：这活到底该写成什么')
    print('=' * 72 + '\n')

    # 判族：看篇幅和一级标题，两个数一卡就清楚
    if a['chars'] < 1500 and a['h1'] == 0:
        family = '基层党建族'
        why = f'篇幅 {a["chars"]} 字、没有一级标题'
    elif a['h1'] >= 2:
        family = '事务性材料族'
        why = f'分 {a["h1"]} 个一级标题'
    else:
        family = '待定'
        why = f'篇幅 {a["chars"]} 字、一级标题 {a["h1"]} 个'

    title = first_prose(text)
    by_title = [g for g in LAW_LAW if g in title]
    by_mark = bool(re.search(r'〔\d{4}〕\d+号', text)) or '主送' in text[:200]
    if by_title or by_mark:
        family = '法定公文'
        why = ('篇名带文种词 ' + '／'.join(by_title)) if by_title else '有发文字号或主送机关'

    print(f'  初判族别：{family}（{why}）')
    print(f'  实测：{a["chars"]} 字　一级 {a["h1"]}　二级 {a["h2"]}　三级 {a["h3"]}')
    print()

    if family == '法定公文':
        print('  法定公文走体例，不查参数。按篇名的文种词确认：')
        print(f'    {"、".join(NO_PARAM)}')
        print('  确认后：python scripts/gate.py 草稿.md --genre 批复')
        print()
        return 2

    print('  参数向量最接近的三种（只作参考，同族文种差异很小）：')
    for i, (g, d) in enumerate(match_genre(a)[:3]):
        print(f'    {"★" if i == 0 else " "} {g:<8} 距离 {d:.2f}')
    print()
    print('  定文种要回答三个问题，别靠距离猜：')
    print('    1) 给谁看——上级（报情况/请示）、下级（提要求）、平级（商洽）、还是自己存档')
    print('    2) 要他干什么——只要知道（报告）、要他批（请示）、要他干（意见/方案/通知）')
    print('    3) 有没有既成事实——写做过的事（经验材料/总结）还是没做的事（方案）')
    print()
    print('  答完带 --genre 重跑，进闸三。')
    return 2


# ── 闸三：交付 ────────────────────────────────────────────────

def collect(text, a, genre, draft):
    """返回 (项名, 状态, 说明) 的列表。状态见文件头的 PASS/WARN/FAIL/HUMAN/BLOCK。"""
    rows = []
    is_law = genre in NO_PARAM

    # 通用项
    quotes = text.count('"')
    rows.append(('引号规范', FAIL if quotes else PASS,
                 f'有 {quotes} 个 ASCII 直引号，公文用中文弯引号' if quotes
                 else '中文弯引号，无直引号'))

    dash = a['dash']
    if is_law:
        rows.append(('破折号', FAIL if dash else PASS,
                     f'{dash} 个；法定公文真件里是 0 个' if dash else '0 个'))
    else:
        rows.append(('破折号', FAIL if dash > 1 else (WARN if dash == 1 else PASS),
                     f'{dash} 个，一篇不超过 1 个' if dash else '0 个'))

    hit = [w for w in BAD_WORDS if w in text]
    must = [w for w in hit if w in HARD_BAD]
    maybe = [w for w in hit if w not in HARD_BAD]
    if must:
        rows.append(('避坑词', FAIL, f'必改：{"、".join(must)}'))
    elif maybe:
        rows.append(('避坑词', WARN,
                     f'看语境：{"、".join(maybe)}（讲话、引语里可能合理，正式文里应换具体说法）'))
    else:
        rows.append(('避坑词', PASS, '无'))

    hits = {w: text.count(w) for w in WARN_WORDS if w in text}
    budget = max(WARN_WORDS_BUDGET, round(a['chars'] / 10000 * WARN_WORDS_BUDGET))
    total = sum(hits.values())
    rows.append(('虚词配额', WARN if total > budget else PASS,
                 f'{"、".join(f"{k}×{v}" for k, v in hits.items())}（这篇别超 {budget} 次）'
                 if hits else '未用"进一步/切实/努力"这类填空词'))

    holds = re.findall(r'【[^】]{0,20}】', text)
    if holds:
        want = '草稿态允许，定稿前必须清' if draft else '定稿不许留，先补齐'
        rows.append(('占位符', WARN if draft else BLOCK,
                     f'{len(holds)} 处：{"".join(holds[:5])}{"…" if len(holds) > 5 else ""}；{want}'))
    else:
        rows.append(('占位符', PASS, '无'))

    # 分族项
    if is_law:
        pat, expect = LAW_OPEN.get(genre, (None, ''))
        lines = text.split('\n')
        # 上行文的请求语在结尾（请示还要落在附件、落款之后），
        # 下行文的承启句在开头。窗口开错了会把对的判成错的。
        window = lines[-15:] if genre in ('请示', '报告') else lines[:20]
        probe = '\n'.join(window)
        if pat is None:
            rows.append(('承启句', PASS, f'{genre}不起承启句，正常'))
        elif re.search(pat, probe):
            rows.append(('承启句', PASS, f'命中「{re.search(pat, probe).group(0)}」'))
        else:
            rows.append(('承启句', FAIL, f'未找到。{genre}应为：{expect}'))

        if a['h3']:
            rows.append(('三级标题', WARN,
                         f'{a["h3"]} 个；只有在附件（指引/办法/细则）里才对，正文应两层'))
        else:
            rows.append(('三级标题', PASS, '未出现三级标题'))

        title = first_prose(text)
        if not any(g in title for g in LAW_LAW):
            rows.append(('篇名要素', FAIL, f'缺文种词：{title[:40]}'))
        elif ('印发' in title or '转发' in title) and '《' not in title:
            rows.append(('篇名要素', FAIL, '印发类篇名要带被印发文件的书名号'))
        else:
            rows.append(('篇名要素', PASS, title[:40]))

        rows.append(('强制词', WARN if a['qz'] > 5 else PASS,
                     f'{a["qz"]} 个；真件中位 0、最高 5'))
    else:
        hard = HARD.get(genre, [])
        bad = [(msg, basis) for key, ok, msg, basis in hard if not ok(a[key])]
        rows.append(('硬冲突', FAIL if bad else PASS,
                     '；'.join(m for m, _ in bad) if bad else f'{len(hard)} 项全过'))

        sent_lo, sent_hi = REF[genre]['sent'][0], REF[genre]['sent'][1]
        rows.append(('句长', WARN if not (sent_lo <= a['sent'] <= sent_hi) else PASS,
                     f'{a["sent"]}，常见 {sent_lo}—{sent_hi}'))

    # 人审项：脚本永远判不了，只提醒
    rows.append(('事实与数据', HUMAN, '数字、文号、人名、日期是否核实（占位符不算核实）'))
    rows.append(('政策表述', HUMAN, '上级文件表述、领导称谓、口径是否准确'))
    if is_law:
        rows.append(('版式要素', HUMAN, '发文字号、主送机关排序、抄送、页码——对照 GB/T 9704'))
    rows.append(('涉密与外发', HUMAN, '涉密、重大决策、对外发布须经本单位核稿人终审'))
    return rows


def gate_deliver(text, a, genre, draft):
    print('\n' + '=' * 72)
    print(f'  闸三｜交付闸：{genre}　{"草稿态" if draft else "定稿态"}')
    print('=' * 72 + '\n')

    rows = collect(text, a, genre, draft)
    for name, status, detail in rows:
        label = {'✓': 'PASS  ', '!': 'WARN  ', '✗': 'FAIL  ',
                 '…': '人审  ', '□': 'BLOCK '}[status]
        print(f'  [{label}] {name:<8} {detail}')

    fails = [r for r in rows if r[1] == FAIL]
    blocks = [r for r in rows if r[1] == BLOCK]
    warns = [r for r in rows if r[1] == WARN]
    humans = [r for r in rows if r[1] == HUMAN]
    # 草稿态下占位符是预期内的，不该混进"需要判断的偏离"
    real_warns = [r for r in warns if not (draft and r[0] == '占位符')]

    print()
    if fails:
        print(f'  结论：不能交付。{len(fails)} 项硬伤：'
              f'{"、".join(r[0] for r in fails)}')
        print('  改完重跑。硬伤在 222 篇语料里 100% 能过，报了基本就是真错了。')
        return 1
    if blocks:
        print(f'  结论：未定稿。{len(blocks)} 项待补：'
              f'{"、".join(r[0] for r in blocks)}')
        print('  补齐后重跑；确实要先交草稿，加 --draft。')
        return 3
    if real_warns:
        print(f'  结论：可以交付，{len(real_warns)} 项需自己判断：'
              f'{"、".join(r[0] for r in real_warns)}')
        print('  提醒项不是错，是"存在合法变体"。答得出为什么偏离就留着，'
              '并在稿件说明里写明是有意偏离。')
    else:
        print('  结论：可以交付，自动项全过。')
    if draft and any(r[0] == '占位符' for r in warns):
        print('  草稿态：占位符还留着，定稿前必须逐条补齐——'
              '它们是你欠读者的事实，不是装饰。')
    print(f'\n  还有 {len(humans)} 项机器判不了，必须人来签字确认——'
          '门控只把得住形式的关。')
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='交付门控：核对任务类型 + 判断是否达到交付标准')
    ap.add_argument('file', help='草稿路径（UTF-8 纯文本 / Markdown）')
    ap.add_argument('--genre', '-g', help='文种；不给则只做类型核对')
    ap.add_argument('--draft', action='store_true', help='草稿态：允许留占位符')
    args = ap.parse_args()

    text = strip_quotes(read(args.file))
    if not text.strip():
        sys.exit('文件是空的')

    a = analyze(text)
    genre = ALIAS.get(args.genre, args.genre) if args.genre else None
    if genre and genre not in REF and genre not in NO_PARAM:
        sys.exit(f'未知文种：{args.genre}\n'
                 f'查参数：{"、".join(REF)}\n'
                 f'法定公文：{"、".join(NO_PARAM)}')

    if genre:
        sys.exit(gate_deliver(text, a, genre, args.draft))
    sys.exit(gate_type(text, a))


if __name__ == '__main__':
    main()
