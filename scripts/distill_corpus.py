#!/usr/bin/env python3
"""语料提炼器

把一批公文原文拆成可复用的写作要素：篇名结构、承启句、标题句式、收尾语、
高频动词搭配、条款骨架。跟 ingest_corpus.py 分工不同——
那个算数值参数，这个抽文字骨架。

用法:
  python scripts/distill_corpus.py                       # 扫 参考/真件样例
  python scripts/distill_corpus.py --dir 我的语料/原文    # 扫自己的稿子
  python scripts/distill_corpus.py --out 某处/提炼.md

设计取舍:
  只抽结构件，不抄内容。输出的每一条都能追溯到原文里的位置，方便回去核对。
  高频搭配按「动词＋2~4 字」的模式抽，比裸 n-gram 的信噪比高得多。
"""
import argparse
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "参考" / "真件样例"
SUFFIX = {".md", ".markdown", ".txt", ".text"}

# 同目录里的说明性文档不算语料（按文件名主干匹配，忽略后缀与大小写）
META_NAMES = {"readme", "索引", "实测", "提炼", "写法提要", "参数汇总", "说明"}

# 承启句：公文的文种指纹。顺序有讲究——「收悉」必须排在「文号」前面，
# 否则批复那句「你委《…》的请示（残工委发〔2026〕5号）收悉」会被当成文号行。
OPENERS = [
    (r"收悉。?现(批复|函复)如下：?", "批复/复函承启"),
    (r"现(提出|函复|答复)如下(意见)?：?", "意见/答复承启"),
    (r"现将有关情况通报如下", "通报承启"),
    (r"现印发.{0,80}?通知如下：?", "印发＋部署承启"),
    (r"现将.{0,60}?(印发|批转)给你们", "印发式承启"),
    (r"现将.{0,40}?(通知|通报)如下：?", "通知/通报承启"),
    (r"现提出如下意见", "意见承启"),
    (r"我代表.{0,40}报告工作", "报告承启"),
    (r"经.{0,12}同意，", "授权语"),
    (r"〔\d{4}〕\d+号", "文号行"),
    (r"^各.+：$", "主送机关"),
    (r"请.{0,12}(认真)?(贯彻)?执行", "执行要求"),
]

CLOSERS = [
    (r"妥否，?请批示", "请示"),
    (r"以上请示，?请予批准", "请示"),
    (r"特此报告|以上报告，?请审阅", "报告"),
    (r"请遵照执行|特此通知", "通知"),
    (r"专此函达|特此函复|此复", "函/批复"),
    (r"特此通报", "通报"),
]

# 公文高频动词，用来捞搭配
VERBS = ("加强", "健全", "完善", "推进", "推动", "统筹", "落实", "抓好",
         "支持", "鼓励", "强化", "深化", "规范", "优化", "加快", "建立",
         "构建", "提升", "严格", "坚持", "突出", "着力", "依法", "全面")
VERB_RE = re.compile("(" + "|".join(VERBS) + r")([\u4e00-\u9fa5]{2,6})")

# 动词搭配里必然混进的假阳性：专名残片，以及「提升工程实施方案」这类
# 动词其实是名词成分的情况。数量少，直接列出来比调模型划算。
COLLOC_STOP = {"习近平", "贯彻", "新时代", "工程", "工程实施", "行动",
               "各项工作", "目标任务", "各项任务", "各项", "工作"}


def clean_colloc(counter):
    """去掉半截词与假阳性。6 字窗口会把「高质量发展」切成「高质量发」「高质量户」，
    按前缀关系只留最长的那一个；连词开头的（「和量的合理增」）和专名残片直接丢。"""
    items = [(v, w, n) for (v, w), n in counter.items()]
    out = Counter()
    for v, w, n in items:
        if w in COLLOC_STOP or w[0] in "和与及并或":
            continue
        if any(k in w for k in ("习近平", "贯彻", "新时代")) or w.startswith("工程"):
            continue
        longer = [nn for vv, ww, nn in items
                  if vv == v and len(ww) > len(w) and ww.startswith(w)]
        if longer and max(longer) >= n:
            continue
        out[(v, w)] = n
    return out

H1_RE = re.compile(r"^\s*([一二三四五六七八九十]+)、\s*(.+)$")
H2_RE = re.compile(r"^\s*（([一二三四五六七八九十]+)）\s*(.+)$")
H3_RE = re.compile(r"^\s*(\d+)[．.]\s*(.+)$")


def read_text(p: Path) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def analyse(text, genre=None):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    # 篇名常被排版断成两行（「国务院关于推行常住地提供」＋「基本公共服务的实施意见」）。
    # 判据两条：第一行不以文种字收尾；或者第一行有没闭合的《（书名号跨行最准）。
    title = lines[0] if lines else ""
    if len(lines) > 1:
        unbalanced = title.count("《") > title.count("》")
        if unbalanced or not re.search(r"(通知|意见|批复|决定|报告|通报|函|纪要|公告|通告)$", title):
            nxt = lines[1]
            if not re.search(r"〔\d{4}〕", nxt) and len(nxt) <= 24:
                title = title + nxt
    body = "\n".join(lines)

    openers = []
    used = set()
    for l in lines[:14]:
        for pat, name in OPENERS:
            if l in used:
                break
            m = re.search(pat, l)
            if m:
                # 取匹配点附近的一段，而不是行的开头。
                # 承启句常常落在一段长引文的末尾，取 l[:90] 会只印出铺垫部分。
                lo, hi = max(0, m.start() - 36), min(len(l), m.end() + 16)
                frag = ("…" if lo else "") + l[lo:hi] + ("…" if hi < len(l) else "")
                openers.append((name, frag))
                used.add(l)
                break
        if len(openers) >= 5:
            break

    closers = []
    for l in lines[-12:]:
        for pat, name in CLOSERS:
            if re.search(pat, l):
                closers.append((name, l[:60]))
                break

    # 一级层次分三种，必须分开统计。混在一起会得出「批复有 4 个一级标题」这种误导结论。
    #   独行式——「一、重点任务」，短语，正文另起，用于意见/通知/报告
    #   合体式——「一、做好宣传解读。要将《指引》迅速传达……」，标题＋句号＋正文同段
    #   条款式——「一、原则同意《方案》，请认真组织实施。」整条即规定，句号后没有正文
    # 判据看句号后面还有没有正文：有正文是合体式，句号即终点是条款式。
    # 批复要单独处理——它的一级条目本身就是规定，一条里可以有好几句，
    # 光看「句号后面还有东西」会把它误判成合体式，只能按文种定。
    h1_title, h1_half, h1_clause = [], [], []
    clause_genres = ("批复", "复函", "决定")
    for m in (H1_RE.match(l) for l in lines):
        if not m:
            continue
        t = m.group(2).strip()
        if genre in clause_genres:
            h1_clause.append(t)
        elif "。" in t:
            head, _, tail = t.partition("。")
            if len(tail.strip()) >= 12:
                h1_half.append(head + "。")
            else:
                h1_clause.append(t)
        elif len(t) > 30:
            h1_clause.append(t)
        else:
            h1_title.append(t)
    h2 = [m.group(2).strip() for m in (H2_RE.match(l) for l in lines) if m]
    h3 = [m.group(2).strip() for m in (H3_RE.match(l) for l in lines) if m]

    # 收尾语：取最后一段真正的正文。跳过成文日期、发文机关署名、
    # 「（此件公开发布）」这类括注和层次标题，否则「收尾语」一栏全是破折号。
    tail = ""
    for l in reversed(lines):
        s = l.strip()
        if not s:
            continue
        if re.match(r"^[\d〇零一二三四五六七八九十]{4}\s*年", s):
            continue
        if s.startswith("（") and s.endswith("）"):
            continue
        # 只跳过"短标题"，不跳过带句号的长标题——批复的一级条目本身就是
        # 一条完整的规定（40—366 字），那才是它的收尾句。
        if (H1_RE.match(s) or H2_RE.match(s) or H3_RE.match(s)) and len(s) <= 24 and "。" not in s:
            continue
        if s.startswith("附件") or (s.endswith("：") and len(s) <= 12):
            continue
        if s == title or s.startswith("（本文有删减"):
            continue
        # 发文机关署名（「国务院」「省政府办公厅」）不是正文，别当成收尾句
        if len(s) <= 14 and not re.search(r"[。，；：！？]", s):
            continue
        tail = s
        break

    colloc = [(v, w) for v, w in VERB_RE.findall(body)]

    # 字数口径与 check_params.py 一致：只去换行，保留空格与标点，
    # 否则同一篇在两份报告里的字数会差一两个字。
    plain = re.sub(r"\*\*|__|`", "", text)
    n_chars = len(plain.replace("\n", ""))

    return dict(title=title, openers=openers, closers=closers, tail=tail,
                h1=h1_title, h1h=h1_half, h1c=h1_clause, h2=h2, h3=h3,
                colloc=colloc, chars=n_chars)


def build(files):
    docs = []
    for f in files:
        genre = f.name.split("_")[0] if "_" in f.name else "—"
        a = analyse(read_text(f), genre)
        a["file"] = f.name
        a["genre"] = genre
        docs.append(a)

    colloc = Counter()
    for d in docs:
        colloc.update(d["colloc"])
    colloc = clean_colloc(colloc)

    L = []
    L.append("# 真件提炼")
    L.append("")
    L.append(f"由 `scripts/distill_corpus.py` 从 {len(docs)} 篇原文自动化抽出，"
             f"生成于 {datetime.now():%Y-%m-%d %H:%M}。")
    L.append("")
    L.append("> 只抽结构件，不抄内容。每条都标了出自哪一篇，要核对可以回去看原文。")
    L.append("")

    L.append("## 一、篇名结构")
    L.append("")
    L.append("| 文种 | 篇名 | 字数 |")
    L.append("|---|---|---|")
    for d in docs:
        L.append(f"| {d['genre']} | {d['title'][:50]} | {d['chars']} |")
    L.append("")

    L.append("## 二、开头：承启句与授权语")
    L.append("")
    L.append("按文种看前十四行里出现的关键句。承启句是文种指纹，用错等于文种错。")
    L.append("")
    by_genre = defaultdict(list)
    for d in docs:
        by_genre[d["genre"]].append(d)
    for g in sorted(by_genre):
        L.append(f"### {g}")
        L.append("")
        for d in by_genre[g]:
            L.append(f"**{d['title'][:40]}**")
            L.append("")
            if d["openers"]:
                for name, s in d["openers"]:
                    L.append(f"- `{name}`　{s}")
            else:
                L.append("- （未匹配到标准承启句）")
            L.append("")

    L.append("## 三、一级层次：独行式、合体式、条款式")
    L.append("")
    L.append("同一位置「一、二、三」在这一批里有三种用法，性质完全不同，用错就是体例错。")
    L.append("")
    L.append("- **独行式**：后面跟短语（「一、重点任务」），正文另起一行，是目录骨架，用于意见/通知/报告")
    L.append("- **合体式**：标题＋句号＋正文挤在同一段（「一、做好宣传解读。要将《指引》……」），"
             "标题当段首提要用，部门发的部署通知常用")
    L.append("- **条款式**：后面直接跟整句规定（「一、原则同意……请认真组织实施。」），"
             "句号即终点、下面没有正文，用于批复")
    L.append("")
    L.append("判据看句号后面还有没有正文：有就是合体式，没有就是条款式。"
             "另有一种「无分条」（决定类，依据＋名单＋号召直推），不在这三者之内。")
    L.append("")
    for d in docs:
        parts = []
        if d["h1"]:
            parts.append(f"独行式 {len(d['h1'])}")
        if d["h1h"]:
            parts.append(f"合体式 {len(d['h1h'])}")
        if d["h1c"]:
            parts.append(f"条款式 {len(d['h1c'])}")
        kind = "／".join(parts)
        L.append(f"**{d['genre']}（{d['title'][:30]}）**　{kind or '无分条'}"
                 f"　二级 {len(d['h2'])} 个、三级 {len(d['h3'])} 个")
        L.append("")
        for t in d["h1"][:18]:
            L.append(f"- {t}")
        for t in d["h1h"][:18]:
            L.append(f"- （合体）{t}")
        for t in d["h1c"][:8]:
            L.append(f"- （条款）{t[:80]}{'…' if len(t) > 80 else ''}")
        if not any((d["h1"], d["h1h"], d["h1c"])):
            L.append("- （无一级层次）")
        L.append("")

    L.append("## 四、收尾语")
    L.append("")
    L.append("程式化结语（特此通知／此复／请遵照执行）在这一列里出现得越少，"
             "越接近中央级真件的写法。")
    L.append("")
    L.append("| 文种 | 程式化结语 | 最后一段正文 |")
    L.append("|---|---|---|")
    for d in docs:
        fixed = "／".join(name for name, _ in d["closers"]) or "无"
        t = d["tail"]
        t = t[:56] + "…" if len(t) > 56 else t
        L.append(f"| {d['genre']} | {fixed} | {t or '—'} |")
    L.append("")

    L.append("## 五、高频动词搭配")
    L.append("")
    L.append("按「动词＋2~6 字」抽，取前 40。这些是公文的动作词骨架，"
             "换话题不换搭配结构。机器抽取，会有少量噪声，**人工核对过的清单见 `写法提要.md` 第五节**。")
    L.append("")
    L.append("| 搭配 | 次数 | 搭配 | 次数 |")
    L.append("|---|---|---|---|")
    top = colloc.most_common(40)
    for i in range(0, len(top), 2):
        left = top[i]
        right = top[i + 1] if i + 1 < len(top) else None
        ln = f"{left[0][0]}·{left[0][1]}"
        if right:
            rn = f"{right[0][0]}·{right[0][1]}"
            L.append(f"| {ln} | {left[1]} | {rn} | {right[1]} |")
        else:
            L.append(f"| {ln} | {left[1]} |  |  |")
    L.append("")

    L.append("## 六、二级标题样本")
    L.append("")
    L.append("看两件事：是否动词开头（加强／健全／推动），有没有主语"
             "（多数不写，因为主语就是主送机关）。只保留标题本身，"
             "句号后面的正文已截掉。")
    L.append("")
    seen = 0
    for d in docs:
        if d["h2"]:
            brief = "　".join((lambda h: h if len(h) <= 26 else h[:26] + "…")(t.split("。")[0])
                              for t in d["h2"][:8])
            L.append(f"**{d['genre']}**（共 {len(d['h2'])} 个）：{brief}")
            L.append("")
            seen += 1
        if seen >= 6:
            break
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="从公文原文提炼写作要素")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--out", default=None, help="输出路径，默认 <dir>/提炼.md")
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir():
        print(f"找不到目录：{folder}")
        return 2
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in SUFFIX
                   and p.stem.lower() not in META_NAMES)
    if not files:
        print(f"{folder} 里没有可提炼的 .md / .txt")
        return 1

    text = build(files)
    out = Path(args.out) if args.out else folder / "提炼.md"
    out.write_text(text, encoding="utf-8")
    print(f"提炼 {len(files)} 篇 → {out}（{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
