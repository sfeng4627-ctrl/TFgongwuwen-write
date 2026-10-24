#!/usr/bin/env python3
"""个人语料入库器

把你自己投进 `我的语料/原文/` 的公文逐篇算参数，汇总出属于你的中位数与四分位区间，
再跟内置基准对照，告诉你哪几项差得值得换掉基准。

用法:
  python scripts/ingest_corpus.py                  # 扫默认目录
  python scripts/ingest_corpus.py --genre 工作总结  # 标注文种，好跟内置基准对比
  python scripts/ingest_corpus.py --dir 某目录      # 换个目录扫
  python scripts/ingest_corpus.py --min 5          # 放宽最少篇数（默认 10）

设计取舍:
  只算参数、不改原稿、不联网、不留副本。输出的两份文件都在 我的语料/_stats/ 下。
  样本量不足时仍然出结果，但会在报告顶部明确标红——少于 10 篇的中位数没有参考价值。
"""
import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from check_params import analyze, REF  # noqa: E402

ROOT = HERE.parent
DEFAULT_DIR = ROOT / "我的语料" / "原文"
OUT_DIR = ROOT / "我的语料" / "_stats"

# 参与统计的指标，顺序即报告顺序
KEYS = [
    ("chars", "字数", "字", True),
    ("sent", "平均句长", "字", True),
    ("h1", "一级标题", "个", True),
    ("h2", "二级标题", "个", True),
    ("h3", "三级标题", "个", True),
    ("yishi", '"一是二是"', "次", True),
    ("dun", "顿号密度", "‰", True),
    ("qz", "强制词", "个", True),
    ("yq", "要求词", "个", True),
    ("jy", "建议词", "个", True),
    ("pct", "百分比", "处", True),
    ("quote", "引号概念", "处", False),
    ("quote_long", "长引语", "‰", False),
    ("paras", "段落数", "段", False),
    ("para_len", "段落长度", "字", False),
]

SUFFIX = {".md", ".markdown", ".txt", ".text"}


def pct(values, q):
    """线性插值分位数，q 取 0-1。不引 numpy，保持零依赖。"""
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def collect(folder, min_chars=300):
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIX)
    rows, skipped = [], []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = f.read_text(encoding="gbk")
            except Exception:
                skipped.append((f.name, "编码读不出来"))
                continue
        except OSError as e:
            skipped.append((f.name, f"读不了：{e.strerror}"))
            continue
        a = analyze(text)
        if not a:
            skipped.append((f.name, "空文件或解析不出正文"))
            continue
        if a["chars"] < min_chars:
            skipped.append((f.name, f"太短（{a['chars']} 字）"))
            continue
        rows.append((f.name, a))
    return rows, skipped, len(files)


def fmt(v):
    if isinstance(v, float):
        return f"{v:.1f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return str(v)


def build_report(rows, genre, skipped, total):
    vals = {k: [r[1][k] for r in rows] for k, *_ in KEYS}
    n = len(rows)
    ref = REF.get(genre) if genre else None

    lines = [f"# 我的语料参数汇总", ""]
    lines.append(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}　·　"
                 f"纳入 **{n}** 篇　·　跳过 {len(skipped)} 项")
    if genre:
        lines.append(f"标注文种：**{genre}**" + ("" if ref else "（内置参数表里没有这个文种，只出你这一侧的数）"))
    lines.append("")

    if n < 10:
        lines += [f"> ⚠️ **样本只有 {n} 篇，不足以定参数。**",
                  "> 少于 10 篇时中位数会被一两篇反常的稿子带跑，这份结果只能看个方向，别直接当基准用。",
                  "> 攒到 15—20 篇再重算。", ""]

    # 一、逐篇明细
    lines += ["## 一、逐篇明细", "",
              "| 篇目 | 字数 | 句长 | 一级 | 二级 | 三级 | 一是 | 顿号‰ | 强制 | 要求 | 建议 | % |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, a in rows:
        lines.append(
            f"| {name} | {a['chars']} | {fmt(a['sent'])} | {a['h1']} | {a['h2']} | {a['h3']} "
            f"| {a['yishi']} | {fmt(a['dun'])} | {a['qz']} | {a['yq']} | {a['jy']} | {a['pct']} |")
    lines.append("")

    # 二、你的区间
    lines += ["## 二、你自己算出来的区间", ""]
    if ref:
        lines += ["| 指标 | 你的中位 | 你的 p25—p75 | 内置中位 | 内置区间 | 判断 |",
                  "|---|---|---|---|---|---|"]
    else:
        lines += ["| 指标 | 你的中位 | 你的 p25—p75 | 极差 |", "|---|---|---|---|"]

    verdicts = {}
    for key, label, unit, track in KEYS:
        v = vals[key]
        mid, lo, hi = statistics.median(v), pct(v, 0.25), pct(v, 0.75)
        if ref and key in ref:
            rlo, rhi, rmid = ref[key]
            gap = mid - rmid
            # 判定阈值：句长 3 字、顿号 3‰、其余按内置区间的四分位宽
            if key == "sent":
                tol = 3
            elif key == "dun":
                tol = 3
            else:
                tol = max((rhi - rlo) / 2, abs(rmid) * 0.25, 1)
            if abs(gap) <= tol:
                verdict = "一致，内置够用"
            elif gap > 0:
                verdict = f"偏高 {fmt(abs(gap))}{unit}"
            else:
                verdict = f"偏低 {fmt(abs(gap))}{unit}"
            verdicts[key] = verdict
            lines.append(f"| {label} | {fmt(mid)} | {fmt(lo)}—{fmt(hi)} "
                         f"| {fmt(rmid)} | {fmt(rlo)}—{fmt(rhi)} | {verdict} |")
        else:
            # 内置参数表里没有这一项，补齐列宽，别让表格错位
            if ref:
                lines.append(f"| {label} | {fmt(mid)} | {fmt(lo)}—{fmt(hi)} "
                             f"| — | — | 内置无此项 |")
            else:
                lines.append(f"| {label} | {fmt(mid)} | {fmt(lo)}—{fmt(hi)} "
                             f"| {fmt(max(v) - min(v))} |")
    lines.append("")

    # 三、结论
    lines += ["## 三、怎么用这份报告", ""]
    if not ref:
        lines += ["没标文种，所以只给了你这一侧的分布。想看跟内置基准的差距，加 `--genre`：", "",
                  "```bash", "python scripts/ingest_corpus.py --genre 调研报告", "```",
                  f"可选：{'、'.join(REF)}", ""]
    else:
        big = [k for k, v in verdicts.items() if v not in ("一致，内置够用",) and not v.startswith("一致")]
        if not big:
            lines += ["各项都跟内置基准对得上。**不用换参数**，直接按 `参数卡.md` 写就行，", 
                      "你这一侧的习惯和通用规律是一致的。", ""]
        else:
            labels = {k: l for k, l, *_ in KEYS}
            lines += [f"有 **{len(big)}** 项跟内置基准差得值得注意：", ""]
            for k in big:
                lines.append(f"- **{labels[k]}**：{verdicts[k]}")
            lines += ["",
                      "这些偏差说明你们系统就是这么写的。**下次写这个文种，以你这列为准。**",
                      "",
                      "具体做法：写之前先看这一节，把你这侧的中位数记在手边；自检报告出来时，",
                      "按你的中位数判断，不要按内置区间判断——内置区间会把你拽回通用平均水平。",
                      ""]
        lines += ["> ⚠️ 只有在样本 ≥15 篇、且篇目确实代表你们系统主流写法时，才该拿自己的数当基准。",
                  "> 混进了不同层级、不同系统的稿子，算出来的中位数是个大杂烩，反而不如内置参数。", ""]

    # 四、跳过清单
    if skipped:
        lines += ["## 四、没纳入的", "", "| 文件 | 原因 |", "|---|---|"]
        for name, why in skipped:
            lines.append(f"| {name} | {why} |")
        lines.append("")

    lines += ["---", "",
              f"> 本报告由 `scripts/ingest_corpus.py` 生成，未改动任何原稿。",
              f"> 扫描目录：`{DEFAULT_DIR.relative_to(ROOT).as_posix()}/`　共发现 {total} 个候选文件。",
              "> 改了语料就重跑一次，报告会整体重算。"]

    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "genre": genre,
        "n": n,
        "metrics": {
            k: {
                "median": statistics.median(vals[k]),
                "p25": pct(vals[k], 0.25),
                "p75": pct(vals[k], 0.75),
                "min": min(vals[k]),
                "max": max(vals[k]),
            } for k, *_ in KEYS
        },
        "verdicts": verdicts,
        "pieces": [{"file": name, "chars": a["chars"]} for name, a in rows],
    }
    return "\n".join(lines), payload


def main():
    ap = argparse.ArgumentParser(description="把个人语料算成你自己的公文参数")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="语料目录，默认 我的语料/原文")
    ap.add_argument("--genre", "-g", help=f"文种，用于跟内置基准对比。可选：{'、'.join(REF)}")
    ap.add_argument("--min", type=int, default=10, help="提醒样本不足的阈值，默认 10")
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir():
        print(f"找不到目录：{folder}", file=sys.stderr)
        print("先把你的公文放进 我的语料/原文/ 再跑", file=sys.stderr)
        return 2

    rows, skipped, total = collect(folder)
    if not rows:
        print(f"在 {folder} 里没找到可解析的公文。", file=sys.stderr)
        print("支持 .md / .txt，单篇至少 300 字。", file=sys.stderr)
        if skipped:
            print("\n发现但没能纳入的：", file=sys.stderr)
            for name, why in skipped:
                print(f"  {name}：{why}", file=sys.stderr)
        return 1

    report, payload = build_report(rows, args.genre, skipped, total)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "参数汇总.md").write_text(report, encoding="utf-8")
    (OUT_DIR / "参数汇总.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 控制台摘要
    print(f"\n纳入 {len(rows)} 篇，跳过 {len(skipped)} 项")
    if len(rows) < args.min:
        print(f"⚠ 样本不足 {args.min} 篇，结果仅供参考")
    for key, label, unit, _ in KEYS[:8]:
        v = [r[1][key] for r in rows]
        print(f"  {label:<10} 中位 {fmt(statistics.median(v)):>7}{unit}"
              f"　区间 {fmt(pct(v, 0.25))}—{fmt(pct(v, 0.75))}{unit}")
    print(f"\n报告已写入：{OUT_DIR / '参数汇总.md'}")
    if args.genre and args.genre in REF:
        diff = [k for k, v in payload["verdicts"].items() if not v.startswith("一致")]
        if diff:
            labels = {k: l for k, l, *_ in KEYS}
            print(f"跟内置「{args.genre}」基准有 {len(diff)} 项偏差：")
            for k in diff:
                print(f"  {labels[k]}：{payload['verdicts'][k]}")
        else:
            print(f"各项都跟内置「{args.genre}」基准对得上，不用换参数")
    return 0


if __name__ == "__main__":
    sys.exit(main())
