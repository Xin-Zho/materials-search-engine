"""tools/label_completeness_sample.py — 独立 Auditor 标注入口（2026-08-29）。

AUDIT_R01（pc_001）500 篇 frozen sample 的逐篇交互标注工具。
只负责"读模板 → 显示 → 写 labels"；不做任何统计计算
（统计在 tools/audit_completeness.py --labels，本工具只产出完整 labels）。

功能：
- 逐篇显示 [i/500] Title / Year / DOI / Abstract（abstract 截断显示）
- 键位：R=RELEVANT  I=IRRELEVANT  U=UNCERTAIN  S=保存进度并退出
- 每标一条立即原子写盘（临时文件 + os.replace，防中断损坏）
- 断点续标：重跑自动跳过已标注条目（label != UNRESOLVED）
- UNCERTAIN 时提示填一句 reason（可选，直接回车跳过）
- 全部标完打印 R/I/U 统计 + 下一步命令

用法：
  python tools/label_completeness_sample.py --audit-id pc_001::20260829043656
  python tools/label_completeness_sample.py --labels <path>      # 自定义 labels 文件
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LABELS_DIR = os.path.join(BASE, "data", "exports", "completeness_labels")

VALID = {"RELEVANT", "IRRELEVANT", "UNCERTAIN"}
_FULL = {"R": "RELEVANT", "I": "IRRELEVANT", "U": "UNCERTAIN"}


def _safe_name(audit_id: str) -> str:
    return audit_id.replace(":", "_").replace("/", "_").replace("\\", "_")


def default_label_path(audit_id: str) -> str:
    return os.path.join(DEFAULT_LABELS_DIR, f"{_safe_name(audit_id)}.json")


def load_payload(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _scopus_abstract(doi: str, base: str = BASE) -> str:
    """从 scopus_cache 按 doi 补 abstract（只读；缓存缺失返回 ''）。"""
    if not doi:
        return ""
    import shutil
    import sqlite3
    import tempfile
    src = os.path.join(base, "data", "cache", "scopus_cache.db")
    if not os.path.exists(src):
        return ""
    tmp = os.path.join(tempfile.gettempdir(), "scopus_cache_label.db")
    try:
        shutil.copy2(src, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
        row = con.execute("SELECT normalized_json FROM papers WHERE paper_id=?",
                          (f"scopus:{doi.strip().lower()}",)).fetchone()
        con.close()
        if row:
            return (json.loads(row[0]).get("abstract") or "").strip()
    except Exception:
        return ""
    return ""


def show_paper(i: int, n: int, it: dict, stats: dict) -> None:
    print("=" * 72)
    print(f"[{i} / {n}]   (已标 {stats['done']} | R={stats['R']} I={stats['I']} "
          f"U={stats['U']} | 剩余 {n - stats['done']})")
    print("=" * 72)
    title = (it.get("title") or "").strip() or "(no title)"
    print(f"Title : {title}")
    print(f"Year  : {it.get('year') or '?'}    DOI: {it.get('doi') or '?'}    "
          f"paper_id: {it.get('paper_id')}")
    ab = (it.get("abstract") or "").strip()
    if ab:
        shown = ab[:800] + (" ... [truncated]" if len(ab) > 800 else "")
        print(f"Abstract: {shown}")
    else:
        print("Abstract: (none — 以 title/DOI 判断，拿不准标 U)")
    print("-" * 72)


def ask() -> tuple[str, str]:
    while True:
        try:
            key = input("  [R] RELEVANT  [I] IRRELEVANT  [U] UNCERTAIN  [S] save&exit > ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            return "S", ""
        if key in ("R", "I", "U"):
            reason = ""
            if key == "U":
                reason = input("  reason (optional, Enter to skip): ").strip()
            return key, reason
        if key in ("S", "Q", "E"):
            return "S", ""
        print("  ? 请输入 R / I / U / S")


def adjudicate_uncertain(path: str, payload: dict, items: list) -> None:
    """裁决模式：只遍历 UNCERTAIN 条目（Abstract 缺失时从 scopus_cache 补），
    键位 R/I/S；实时写盘。裁决后 label 更新为 RELEVANT/IRRELEVANT。"""
    short = {"RELEVANT": "R", "IRRELEVANT": "I", "UNCERTAIN": "U"}
    stats = {"done": 0, "R": 0, "I": 0, "U": 0}
    unc = [it for it in items if it.get("label") == "UNCERTAIN"]
    n = len(unc)
    for it in items:
        lab = it.get("label", "UNRESOLVED")
        if lab != "UNRESOLVED":
            stats["done"] += 1
            stats[short[lab]] += 1
    print(f"adjudication: {n} 篇 UNCERTAIN 待裁决（已裁决 R={stats['R']} I={stats['I']}）")
    print("  信息不足型：Abstract 缺失时自动从 scopus_cache 补；仍缺则按 title+DOI 判断")
    print("  键位: [R] RELEVANT  [I] IRRELEVANT  [S] 保存进度并退出")
    print("=" * 72)
    for i, it in enumerate(unc, 1):
        ab = (it.get("abstract") or "").strip()
        if not ab:
            ab = _scopus_abstract(it.get("doi") or "", BASE)
        print("=" * 72)
        print(f"[{i} / {n}]   (本批已裁 {stats['R'] + stats['I']} | "
              f"R={stats['R']} I={stats['I']})")
        print("=" * 72)
        print(f"Title : {(it.get('title') or '').strip() or '(no title)'}")
        print(f"Year  : {it.get('year') or '?'}    DOI: {it.get('doi') or '?'}    "
              f"paper_id: {it.get('paper_id')}")
        if ab:
            shown = ab[:800] + (" ... [truncated]" if len(ab) > 800 else "")
            print(f"Abstract: {shown}")
        else:
            print("Abstract: (无——OpenAlex/Scopus 均无，按 title 判断，仍不确定标 I 前先想清楚)")
        print(f"原 reason: {(it.get('reason') or '').strip()}")
        print("-" * 72)
        while True:
            try:
                key = input("  [R] RELEVANT  [I] IRRELEVANT  [S] save&exit > ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                key = "S"
            if key in ("R", "I"):
                it["label"] = _FULL[key]
                it["adjudicated"] = True
                stats["done"] += 1
                stats[key] += 1
                atomic_write(path, payload)
                break
            if key in ("S", "Q", "E"):
                print(f"\n[SAVED] 已裁 {stats['R'] + stats['I']}/{n}（R={stats['R']} "
                      f"I={stats['I']}）——重跑 --adjudicate-uncertain 继续")
                return
            print("  ? 请输入 R / I / S")
    print(f"\n[OK] {n}/{n} UNCERTAIN 全部裁决完成！R={stats['R']} I={stats['I']}")
    print("重跑统计:")
    print(f"  python tools/audit_completeness.py --audit-id pc_001::20260829043656 "
          f"--labels {path}")


def main():
    ap = argparse.ArgumentParser(description="独立 Auditor 标注入口（只写 labels，不算统计）")
    ap.add_argument("--audit-id", default="pc_001::20260829043656",
                    help="audit_id（默认 pc_001::20260829043656）")
    ap.add_argument("--labels", default=None,
                    help="labels JSON 路径（默认 data/exports/completeness_labels/<audit_id>.json）")
    ap.add_argument("--adjudicate-uncertain", action="store_true",
                    help="裁决模式：只遍历 UNCERTAIN 条目（abstract 缺失自动从 "
                         "scopus_cache 补），R/I 裁决实时写盘")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    path = args.labels or default_label_path(args.audit_id)
    if not os.path.exists(path):
        print(f"[FAIL] labels 文件不存在: {path}")
        print("  先创建 audit: python tools/audit_completeness.py --topic pc_001 --create ...")
        sys.exit(1)

    payload = load_payload(path)
    items = payload.get("labels", [])
    n = len(items)
    if n == 0:
        print("[FAIL] labels 为空——模板异常，先重新导出模板")
        sys.exit(1)

    # ── 裁决模式：只处理 UNCERTAIN（Audit R01 封账用）──
    if args.adjudicate_uncertain:
        adjudicate_uncertain(path, payload, items)
        return

    # 统计当前进度
    short = {"RELEVANT": "R", "IRRELEVANT": "I", "UNCERTAIN": "U"}
    stats = {"done": 0, "R": 0, "I": 0, "U": 0}
    for it in items:
        lab = it.get("label", "UNRESOLVED")
        if lab != "UNRESOLVED":
            stats["done"] += 1
            stats[short[lab]] += 1
    print(f"audit_id: {payload.get('audit_id')} | 样本 {n} 篇 | 已标 {stats['done']} 篇"
          f"（R={stats['R']} I={stats['I']} U={stats['U']}）")
    if stats["done"] == n:
        print("\n[OK] 全部已标注完成。运行统计:")
        print(f"  python tools/audit_completeness.py --audit-id {args.audit_id} "
              f"--labels {path}")
        return

    print("标注标准（详见 docs/2026-08-29-audit-r01-labeling-guide.md）:")
    print("  RELEVANT: 收缩/收缩应力是论文 substantive objective/studied topic")
    print("  IRRELEVANT: 只在 background 提一句 low shrinkage is desirable")
    print("  UNCERTAIN: 信息不足/边缘情况——单独裁决，不强行判 negative")
    print("=" * 72)

    for i, it in enumerate(items, 1):
        if it.get("label", "UNRESOLVED") != "UNRESOLVED":
            continue  # 断点续标：跳过已标注
        show_paper(i, n, it, stats)
        label, reason = ask()
        if label == "S":
            break
        # label 是短键 R/I/U；写入文件必须用完整名（load_labels 校验
        # VALID_LABELS = {RELEVANT, IRRELEVANT, UNCERTAIN}）
        full = _FULL[label]
        it["label"] = full
        if reason:
            it["reason"] = reason
        if not it.get("reviewer"):
            it["reviewer"] = "auditor"
        stats["done"] += 1
        stats[label] += 1
        atomic_write(path, payload)  # 每标一条立即写盘

    if stats["done"] == n:
        print("\n[OK] 500/500 全部标注完成！")
        print(f"  R={stats['R']}  I={stats['I']}  U={stats['U']}")
        print("运行正式统计:")
        print(f"  python tools/audit_completeness.py --audit-id {args.audit_id} "
              f"--labels {path}")
    else:
        print(f"\n[SAVED] 进度已保存: {path}")
        print(f"  已标 {stats['done']}/{n}（R={stats['R']} I={stats['I']} U={stats['U']}）"
              f"——随时重跑本命令继续，自动跳过已标条目")


if __name__ == "__main__":
    main()
