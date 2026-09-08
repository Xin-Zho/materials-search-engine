"""R06 三层 recall 统计（North Star §4 契约；判定回填后运行）。

输入：盲评 filled payload（labels 每项含 paper_id + label + doi）
     + r06_candidate_db_dois.json（24677：canonical doi ∪ Scopus EID）
     + r06_finalkb_dois.json（208：全 doi——含 Scopus EID-only 老文桥接 doi）

口径（§4.1/§4.2 冻结 + 恒等式自洽）：
  UNCERTAIN 归属双报：
    R1 operational: AuditRelevant = RELEVANT ∪ UNCERTAIN
                    （staging"未排除"语义；UNCERTAIN 也 ∈ FinalKB 对象）
    R2 strict:      AuditRelevant = RELEVANT only
  三层（各自口径）：
    Retrieval = |A∩CandidateDB| / |A|_resolved
    Sens      = |A∩FinalKB|    / |A∩CandidateDB|
    E2E       = |A∩FinalKB|    / |A|_resolved = Retrieval × Sens
  resolved 主口径 + ultra 保守口径（Identity Unknown 全按 retrieval miss）双报。
  Identity Unknown（样本无 doi，无法与 DB/KB 主键 reconcile）→ diagnostic，
  不混入三层分子也不计 clean miss。

用法：
  python tools/compute_r06_recall.py --labels <filled.json>
"""
import argparse
import datetime
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAND = os.path.join(BASE, "data/exports/terminology/r06_candidate_db_dois.json")
FINALKB = os.path.join(BASE, "data/exports/terminology/r06_finalkb_dois.json")
S6_SEEN = os.path.join(BASE, "data/exports/terminology/s6_seen_set.json")
KB_DB = os.path.join(BASE, "data/cache/knowledge_base.db")
OUT = os.path.join(BASE, "data/exports/terminology/r06_recall_report.json")

VALID = {"RELEVANT", "UNCERTAIN", "IRRELEVANT"}


def norm_doi(d):
    if not d:
        return None
    d = str(d).strip().lower()
    d = re.sub(r"^doi:\s*", "", d)
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = d.split("</div")[0].strip()
    return d if d and d not in ("none", "nan", "null") else None


def extra_identity_channels():
    """补身份通道（reconcile，只读）：
    - S6_SEEN 中 openalex W id 键（seen 以 W id 记录的论文，cand doi 文件丢失此形态）
    - KB knowledge_records.record_json 的 openalex_id（KB 论文的 W id）
    返回 (seen_wids, kb_wids)。样本 paper_id(W id) 命中即 retrieved/KB。"""
    import sqlite3

    seen_wids = set()
    try:
        d = json.load(open(S6_SEEN, encoding="utf-8"))
        for k in (d.get("keys") or []):
            k = str(k)
            if k.startswith(("W", "https://openalex")):
                seen_wids.add(k.replace("https://openalex.org/", ""))
    except Exception:
        pass
    kb_wids = set()
    try:
        con = sqlite3.connect(f"file:{KB_DB}?mode=ro&immutable=1", uri=True)
        for pid, rj in con.execute(
                "SELECT paper_id, record_json FROM knowledge_records"):
            r = json.loads(rj)
            oa = (r.get("openalex_id") or "").replace("https://openalex.org/", "")
            if oa:
                kb_wids.add(oa)
        con.close()
    except Exception:
        pass
    return seen_wids, kb_wids


def pct(a, b):
    return (a / b) if b else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="filled payload json")
    args = ap.parse_args()

    cand = {norm_doi(x) for x in json.load(open(CAND, encoding="utf-8"))}
    cand.discard(None)
    kb = {norm_doi(x) for x in json.load(open(FINALKB, encoding="utf-8"))}
    kb.discard(None)
    seen_wids, kb_wids = extra_identity_channels()
    # 恒等式前提 FinalKB⊆CandidateDB 在 W id 通道同样成立：
    # CandidateDB := seen ∪ FinalKB，故 KB 的 W id 也 ∈ DB
    seen_wids |= kb_wids

    payload = json.load(open(args.labels, encoding="utf-8"))
    labels = payload["labels"]
    judged = [x for x in labels if x.get("label") in VALID]
    unjudged = [x for x in labels if x.get("label") not in VALID]
    print(f"labels {len(labels)} | judged {len(judged)} | unjudged {len(unjudged)}")

    rel = [x for x in judged if x["label"] == "RELEVANT"]
    unc = [x for x in judged if x["label"] == "UNCERTAIN"]
    irr = [x for x in judged if x["label"] == "IRRELEVANT"]

    def has_doi(x):
        return bool(norm_doi(x.get("doi")))

    def in_db(x):
        d = norm_doi(x.get("doi"))
        return (d in cand) or (x.get("paper_id") in seen_wids)

    def in_kb(x):
        d = norm_doi(x.get("doi"))
        return (d in kb) or (x.get("paper_id") in kb_wids)

    def reconcilable(x):
        """identity 可 reconcile：有 doi，或 W id 在系统任一记录中（seen/KB）。
        no-doi 且 W id 完全无记录者 → Identity Unknown（无法排除 EID 通道存在）。"""
        return has_doi(x) or x.get("paper_id") in seen_wids or \
            x.get("paper_id") in kb_wids

    # identity 解析：resolved = reconcilable（doi 或 W id 通道可判）
    rel_w = [x for x in rel if reconcilable(x)]
    rel_n = [x for x in rel if not reconcilable(x)]
    unc_w = [x for x in unc if reconcilable(x)]
    unc_n = [x for x in unc if not reconcilable(x)]
    n_id_un = len(rel_n) + len(unc_n)          # Identity Unknown（A 内不可判）
    n_id_un_rel = len(rel_n)

    # ── R2 strict：A = RELEVANT ──────────────────────────────
    n_r2 = len(rel_w)                          # resolved 分母
    ret_r2 = sum(1 for x in rel_w if in_db(x))
    kb_r2 = sum(1 for x in rel_w if in_kb(x))
    sens_r2 = pct(kb_r2, ret_r2)
    e2e_r2 = pct(kb_r2, n_r2)                  # direct
    e2e_r2_ident = pct(ret_r2, n_r2) * sens_r2  # = Ret × Sens 恒等式
    # ultra：分母含 identity unknown；unknown 中 W 通道命中者也计入分子
    ret_r2_all = ret_r2 + sum(1 for x in rel_n if in_db(x))
    kb_r2_all = kb_r2 + sum(1 for x in rel_n if in_kb(x))
    ret_r2_ultra = pct(ret_r2_all, len(rel))
    sens_r2_ultra = pct(kb_r2_all, ret_r2_all) if ret_r2_all else 0
    e2e_r2_ultra = ret_r2_ultra * sens_r2_ultra

    # ── R1 operational：A = RELEVANT ∪ UNCERTAIN ─────────────
    a1 = rel_w + unc_w
    n_r1 = len(a1)                             # resolved 分母
    ret_r1 = sum(1 for x in a1 if in_db(x))
    kb_r1 = sum(1 for x in a1 if in_kb(x))
    sens_r1 = pct(kb_r1, ret_r1)
    e2e_r1 = pct(kb_r1, n_r1)                  # direct
    e2e_r1_ident = pct(ret_r1, n_r1) * sens_r1  # = Ret × Sens 恒等式
    ret_r1_all = ret_r1 + sum(1 for x in rel_n + unc_n if in_db(x))
    kb_r1_all = kb_r1 + sum(1 for x in rel_n + unc_n if in_kb(x))
    ret_r1_ultra = pct(ret_r1_all, len(rel) + len(unc))
    sens_r1_ultra = pct(kb_r1_all, ret_r1_all) if ret_r1_all else 0
    e2e_r1_ultra = ret_r1_ultra * sens_r1_ultra

    ok_r2 = abs(e2e_r2 - e2e_r2_ident) < 1e-9
    ok_r1 = abs(e2e_r1 - e2e_r1_ident) < 1e-9

    # ── diagnostics ──────────────────────────────────────────
    unc_rate = pct(len(unc), len(judged))
    id_un_rate_rel = pct(n_id_un_rel, len(rel))
    id_un_rate_a1 = pct(n_id_un, len(rel) + len(unc))
    kb_subset_db = kb <= cand                  # 恒等式前提校验

    rep = {
        "audit_id": payload.get("audit_id"),
        "universe_id": payload.get("universe_id"),
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_labels": len(labels),
        "judged": len(judged),
        "counts": {"RELEVANT": len(rel), "UNCERTAIN": len(unc),
                   "IRRELEVANT": len(irr)},
        "identity": {
            "no_doi_RELEVANT": len(rel_n),
            "no_doi_UNCERTAIN": len(unc_n),
            "note": "样本 no-doi 为 OpenAlex MAG-only 记录（cache 顶层/locations 均无 "
                    "DOI，无 scopus id 可桥接）；DB/KB 主键为 doi(+EID)，无法 reconcile "
                    "→ Identity Unknown diagnostic，resolved 排除、ultra 按 miss",
        },
        "R1_operational": {
            "A": "RELEVANT+UNCERTAIN", "n_resolved": n_r1,
            "retrieval": {"hit": ret_r1, "denom": n_r1, "recall": ret_r1 / n_r1,
                          "ultra_denom_all": len(rel) + len(unc),
                          "recall_ultra": ret_r1_ultra},
            "sensitivity": {"kb": kb_r1, "db": ret_r1, "sens": sens_r1},
            "e2e": {"kb": kb_r1, "denom": n_r1, "recall": e2e_r1,
                    "ident_check": e2e_r1_ident, "ident_ok": ok_r1,
                    "recall_ultra": e2e_r1_ultra},
        },
        "R2_strict": {
            "A": "RELEVANT", "n_resolved": n_r2,
            "retrieval": {"hit": ret_r2, "denom": n_r2, "recall": ret_r2 / n_r2,
                          "ultra_denom_all": len(rel),
                          "recall_ultra": ret_r2_ultra},
            "sensitivity": {"kb": kb_r2, "db": ret_r2, "sens": sens_r2},
            "e2e": {"kb": kb_r2, "denom": n_r2, "recall": e2e_r2,
                    "ident_check": e2e_r2_ident, "ident_ok": ok_r2,
                    "recall_ultra": e2e_r2_ultra},
        },
        "diagnostics": {
            "identity_unknown_rate_RELEVANT": id_un_rate_rel,
            "identity_unknown_rate_A_R1": id_un_rate_a1,
            "uncertain_rate": unc_rate,
            "finalkb_subset_candidatedb": bool(kb_subset_db),
        },
        "inputs": {
            "candidate_db_n": len(cand),
            "finalkb_n": len(kb),
            "labels_file": args.labels,
        },
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)

    print(f"\n=== R06 三层 recall（{payload.get('audit_id')}）===")
    print(f"labels {len(labels)} | R {len(rel)} | U {len(unc)} | I {len(irr)}")
    print(f"Identity Unknown: {n_id_un} (A 内 no-doi; rel {n_id_un_rel})"
          f"  [diagnostic]")
    print(f"FinalKB ⊆ CandidateDB: {'✓' if kb_subset_db else '✗ 恒等式前提破坏'}")
    print(f"\n--- R2 strict (A=RELEVANT, resolved n={n_r2}) ---")
    print(f"  Retrieval   = {ret_r2}/{n_r2} = {ret_r2/n_r2:.1%}"
          f"   (ultra /{len(rel)} = {ret_r2_ultra:.1%})")
    print(f"  Sensitivity = {kb_r2}/{ret_r2} = {sens_r2:.1%}")
    print(f"  E2E         = {kb_r2}/{n_r2} = {e2e_r2:.1%}"
          f"   (恒等式 {e2e_r2_ident:.1%} {'✓' if ok_r2 else '✗'}"
          f" | ultra {e2e_r2_ultra:.1%})")
    print(f"\n--- R1 operational (A=RELEVANT+UNCERTAIN, resolved n={n_r1}) ---")
    print(f"  Retrieval   = {ret_r1}/{n_r1} = {ret_r1/n_r1:.1%}"
          f"   (ultra /{len(rel)+len(unc)} = {ret_r1_ultra:.1%})")
    print(f"  Sensitivity = {kb_r1}/{ret_r1} = {sens_r1:.1%}")
    print(f"  E2E         = {kb_r1}/{n_r1} = {e2e_r1:.1%}"
          f"   (恒等式 {e2e_r1_ident:.1%} {'✓' if ok_r1 else '✗'}"
          f" | ultra {e2e_r1_ultra:.1%})")
    print(f"\ndiagnostics: UNCERTAIN rate {unc_rate:.1%} | "
          f"IdentityUnknown(rel) {id_un_rate_rel:.1%} | "
          f"IdentityUnknown(A^R1) {id_un_rate_a1:.1%}")
    print(f"[ok] report -> {OUT}")


if __name__ == "__main__":
    main()
