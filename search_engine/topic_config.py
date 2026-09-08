"""P0-2 主题配置加载器：所有 S6/S7 主脚本经这里读 topic 配置，禁止硬编码主题常量。

约定（用户 2026-09-08 定稿）:
    topics/<topic_id>/
        topic.yaml    主题配置（8 项：topic_id/name/question/rubric/retrieval/labels/
                      identity/assets/legacy）
        rubric.md     relevance rubric 全文（freeze 对象，版本号在 topic.yaml）
        seeds.json    seed corpus
        termbank.json TermBank（词源三层）
        runs/         该主题累积产物（run 级）

用法:
    from search_engine.topic_config import load_topic, list_topics
    t = load_topic("photopolymerization_shrinkage")
    t.rubric_text          # rubric.md 全文
    t.asset_path("termbank")
    t.runs_dir
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_ROOT = os.path.join(BASE, "topics")

# v1.0 唯一的主题是 photopolymerization_shrinkage（其 rubric/termbank 已 materialize
# 进 topics/ 目录）。为兼容旧调用，显式默认主题名。
DEFAULT_TOPIC = "photopolymerization_shrinkage"


class TopicNotFound(FileNotFoundError):
    pass


@dataclass
class TopicConfig:
    topic_id: str
    dir: str
    raw: dict
    rubric_text: str = ""
    _asset_cache: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return (self.raw.get("name") or self.topic_id)

    @property
    def research_question(self) -> str:
        return self.raw.get("research_question") or ""

    @property
    def rubric_version(self) -> str:
        rub = self.raw.get("rubric") or {}
        return rub.get("version") or "UNVERSIONED"

    @property
    def anchor(self) -> str:
        ret = self.raw.get("retrieval") or {}
        return ret.get("anchor") or ""

    @property
    def runs_dir(self) -> str:
        return self.asset_path("runs_dir", must_exist=False)

    @property
    def labels(self) -> dict:
        return self.raw.get("labels") or {}

    def asset_path(self, key: str, must_exist: bool = True) -> str:
        """assets 里声明的相对路径解析为绝对路径。"""
        if key in self._asset_cache:
            return self._asset_cache[key]
        assets = self.raw.get("assets") or {}
        rel = assets.get(key)
        if not rel:
            raise KeyError(f"topic {self.topic_id} assets 未声明: {key}")
        p = os.path.join(self.dir, rel)
        if must_exist and not os.path.exists(p):
            raise FileNotFoundError(f"topic asset 缺失: {p} (topic={self.topic_id})")
        self._asset_cache[key] = p
        return p

    def load_json_asset(self, key: str) -> dict:
        p = self.asset_path(key)
        return json.load(open(p, encoding="utf-8"))


def list_topics() -> list[str]:
    """已注册主题（topics/<id>/topic.yaml 存在）。"""
    out = []
    if os.path.isdir(TOPICS_ROOT):
        for d in sorted(os.listdir(TOPICS_ROOT)):
            if os.path.isfile(os.path.join(TOPICS_ROOT, d, "topic.yaml")):
                out.append(d)
    return out


def topic_dir(topic_id: str) -> str:
    return os.path.join(TOPICS_ROOT, topic_id)


def _strip_rubric_front(text: str) -> str:
    """rubric.md 允许 markdown 头（'#/ >/---' 与空行）；LLM prompt 只取正文（剥离头）。
    约定 rubric 正文不含行首 '# > ---'（freeze 原文为纯文段），故剥离安全。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s == "" or s.startswith("#") or s.startswith(">") or s.startswith("---"):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def load_topic(topic_id: str = DEFAULT_TOPIC) -> TopicConfig:
    """加载主题配置 + rubric 全文。topic 未注册抛 TopicNotFound。"""
    import yaml
    d = topic_dir(topic_id)
    yp = os.path.join(d, "topic.yaml")
    if not os.path.exists(yp):
        raise TopicNotFound(
            f"topic {topic_id} 未注册（缺 {yp}）。用 bootstrap_topic.py 新建空主题，"
            f"或检查 --topic 拼写。已注册: {list_topics()}")
    raw = yaml.safe_load(open(yp, encoding="utf-8")) or {}
    t = TopicConfig(topic_id=topic_id, dir=d, raw=raw)
    # rubric 全文（LLM prompt 用正文，剥离 markdown 头）
    rub = raw.get("rubric") or {}
    rf = rub.get("file")
    if rf:
        rp = os.path.join(d, rf)
        if os.path.exists(rp):
            t.rubric_text = _strip_rubric_front(open(rp, encoding="utf-8").read())
    return t


def outputs_dir(topic_id: str = DEFAULT_TOPIC) -> str:
    """主题 run 产物目录。pc001(v1.0 有 legacy exports) → 保留 data/exports/terminology
    原址（防 v1.0 复跑静默改变落点）；新主题 → topics/<topic_id>/runs/。"""
    t = load_topic(topic_id)
    lg = (t.raw.get("legacy_v1") or {}).get("exports")
    if lg:
        return os.path.join(BASE, lg)
    os.makedirs(t.runs_dir, exist_ok=True)
    return t.runs_dir


def is_legacy_topic(topic_id: str = DEFAULT_TOPIC) -> bool:
    """v1.0 legacy 主题（topic.yaml 声明 legacy_v1.exports）→ 输出保留冻结原址/原名。
    新主题（无 legacy 声明）→ runs/ 通用名。判断依据是配置而非主题 id（禁领域特判）。"""
    t = load_topic(topic_id)
    return bool((t.raw.get("legacy_v1") or {}).get("exports"))


def resolve_output(topic_id: str, legacy_path: str, new_basename: str) -> str:
    """主题输出路由：legacy 主题 → v1.0 冻结原址（行为等价，防静默漂移）；
    新主题 → topics/<id>/runs/<new_basename>。脚本默认输出统一走这里。"""
    if is_legacy_topic(topic_id):
        return legacy_path
    return os.path.join(outputs_dir(topic_id), new_basename)


def resolve_input(topic_id: str, legacy_path: str, new_basename: str) -> str:
    """主题输入路由（该主题上一阶段产物）：legacy → 冻结原址；新主题 → runs/。"""
    if is_legacy_topic(topic_id):
        return legacy_path
    return os.path.join(topic_dir(topic_id), "runs", new_basename)


def ensure_topic(topic_id: str, name: str = "", question: str = "") -> TopicConfig:
    """bootstrap：注册空主题（生成 topic.yaml + rubric.md 占位 + 空 seeds/termbank + runs/）。
    只写配置与空资产，绝不复制任何 Python 文件。已存在但资产缺失则补齐。"""
    d = topic_dir(topic_id)
    os.makedirs(os.path.join(d, "runs"), exist_ok=True)
    for fname, content in [
        ("seeds.json", json.dumps({"topic_id": topic_id, "n": 0, "seeds": [],
                                   "definition": "seed corpus 待收集"}, ensure_ascii=False, indent=1)),
        ("termbank.json", json.dumps({"topic_id": topic_id, "counts": {"VERIFIED_EXACT": 0,
                                        "SEMANTIC_CANDIDATE": 0, "REJECTED": 0},
                                      "terms": [], "definition": "TermBank 待 term expansion"},
                                     ensure_ascii=False, indent=1)),
    ]:
        p = os.path.join(d, fname)
        if not os.path.exists(p):
            open(p, "w", encoding="utf-8").write(content)
    rp = os.path.join(d, "rubric.md")
    if not os.path.exists(rp):
        open(rp, "w", encoding="utf-8").write(
            f"# Relevance Rubric — {topic_id}\n\nTODO: 撰写判定标准（freeze 后升版本号）。\n")
    yp = os.path.join(d, "topic.yaml")
    if not os.path.exists(yp):
        body = {
            "topic_id": topic_id,
            "name": name or topic_id,
            "research_question": question,
            "rubric": {"version": "TBD", "file": "rubric.md", "frozen_at": None},
            "retrieval": {"backends": ["openalex"], "anchor": "", "default_max_results": 20},
            "labels": {"relevant": "RELEVANT", "uncertain": "UNCERTAIN",
                       "irrelevant": "IRRELEVANT"},
            "identity": {"preserve": ["doi", "scopus_eid", "openalex_id"],
                         "canonical_priority": ["doi", "openalex_id", "scopus_eid"]},
            "assets": {"termbank": "termbank.json", "seeds": "seeds.json",
                       "runs_dir": "runs/"},
            "legacy_v1": {"exports": None},
        }
        import yaml
        open(yp, "w", encoding="utf-8").write(yaml.safe_dump(body, allow_unicode=True,
                                                             sort_keys=False))
    return load_topic(topic_id)
