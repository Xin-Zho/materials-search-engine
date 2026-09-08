"""P0-2: bootstrap 新主题——只生成配置目录，零 Python 复制。

用法:
    python tools/bootstrap_topic.py --topic thermochromic_materials \
        --name "Thermochromic Materials" \
        --question "Identify materials, mechanisms ... thermochromic behavior."

产出（topics/<topic_id>/）:
    topic.yaml / rubric.md / seeds.json(空) / termbank.json(空) / runs/
已注册主题列表: python tools/bootstrap_topic.py --list
"""

import argparse
import sys

from search_engine.topic_config import ensure_topic, list_topics, topic_dir

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser(description="P0-2 bootstrap 新主题（空骨架，不复制代码）")
    ap.add_argument("--topic", help="topic_id（如 thermochromic_materials）")
    ap.add_argument("--name", default="", help="主题名（可选，默认 topic_id）")
    ap.add_argument("--question", default="", help="research question（建议必填）")
    ap.add_argument("--list", action="store_true", help="列出已注册主题")
    args = ap.parse_args()

    if args.list:
        print("已注册 topics:", list_topics() or "(无)")
        return

    if not args.topic:
        ap.error("需要 --topic 或 --list")
    if args.topic == "photopolymerization_shrinkage":
        print("[SKIP] photopolymerization_shrinkage 是 v1.0 既有主题，勿重建。")

    t = ensure_topic(args.topic, name=args.name, question=args.question)
    print(f"[OK] topic {t.topic_id} 就绪: {topic_dir(t.topic_id)}")
    print("  rubric.version =", t.rubric_version)
    print("  下一步: 写 rubric.md → seeds.json → termbank.json，再跑 QA/检索脚本（--topic）")


if __name__ == "__main__":
    main()
