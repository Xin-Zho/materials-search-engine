"""ScopusQueryCompiler — 将结构化的搜索意图翻译为 Scopus 高级搜索语法。

使用方式:
    compiler = ScopusQueryCompiler()
    intent = SearchIntent(
        keywords=["photocuring", "composite"],
        synonyms={"photocuring": ["photopolymerization", "light-curing"]},
        must_include=["degree of conversion"],
        exclude=["coating"],
        year_range=(2020, 2025),
        document_type="ar",
    )
    query = compiler.compile(intent)
    # → TITLE-ABS-KEY((photocuring OR photopolymerization OR "light curing")
    #     AND composite AND "degree of conversion"
    #     AND NOT (coating))
    #     AND PUBYEAR > 2019 AND PUBYEAR < 2026
    #     AND DOCTYPE(ar) AND LANGUAGE(english)
"""

from .models import SearchIntent


class ScopusQueryCompiler:
    """将 SearchIntent 编译为 Scopus 高级搜索语句。"""

    def compile(self, intent: SearchIntent) -> str:
        """根据搜索意图生成完整的 Scopus 查询字符串。"""
        parts: list[str] = []

        # 主体搜索词
        main = self._build_main_query(intent)
        if main:
            parts.append(main)

        # 作者限定
        if intent.author:
            parts.append(f"AUTH({intent.author})")

        # 机构限定
        if intent.affiliation:
            parts.append(f"AFFIL({intent.affiliation})")

        # 年份范围
        if intent.year_range:
            start, end = intent.year_range
            if start and end:
                parts.append(f"PUBYEAR > {start - 1} AND PUBYEAR < {end + 1}")
            elif start:
                parts.append(f"PUBYEAR > {start - 1}")
            elif end:
                parts.append(f"PUBYEAR < {end + 1}")

        # 文献类型
        if intent.document_type:
            parts.append(f"DOCTYPE({intent.document_type})")

        # 学科领域
        if intent.subject_area:
            areas = " OR ".join(intent.subject_area)
            parts.append(f"SUBJAREA({areas})")

        # 语言
        if intent.language:
            parts.append(f"LANGUAGE({intent.language})")

        return " AND ".join(parts)

    def _build_main_query(self, intent: SearchIntent) -> str:
        """构建 TITLE-ABS-KEY(...) 主体部分。"""
        clauses: list[str] = []

        # 关键词及其同义词
        for kw in intent.keywords:
            terms = [self._wrap(kw)]
            if kw in intent.synonyms:
                terms.extend(self._wrap(s) for s in intent.synonyms[kw])
            if len(terms) > 1:
                clauses.append(f"({' OR '.join(terms)})")
            else:
                clauses.append(terms[0])

        # 必须包含的短语
        for phrase in intent.must_include:
            if " PRE/" in phrase or " W/" in phrase:
                # 已经含邻近运算符，直接使用
                clauses.append(phrase)
            elif " " in phrase:
                # 多词短语，用邻近运算符 PRE/0（等同于精确短语）
                words = phrase.split()
                clauses.append(f"{' PRE/0 '.join(self._wrap_token(w) for w in words)}")
            else:
                clauses.append(self._wrap(phrase))

        # 组合子句
        if not clauses:
            return ""

        main = " AND ".join(clauses)

        # 排除词
        if intent.exclude:
            exclude_clause = " OR ".join(self._wrap(e) for e in intent.exclude)
            main = f"({main}) AND NOT ({exclude_clause})"

        return f"TITLE-ABS-KEY({main})"

    @staticmethod
    def _wrap(term: str) -> str:
        """包裹一个词：含空格或特殊字符时用引号，否则直接返回。"""
        term = term.strip().strip('"').strip("'")
        if " " in term or "-" in term:
            return f'"{term}"'
        return term

    @staticmethod
    def _wrap_token(token: str) -> str:
        """包裹邻近运算符中的单个 token。"""
        token = token.strip().strip('"').strip("'")
        return token

    def compile_multi(self, intent: SearchIntent) -> list[str]:
        """生成多个互补查询以最大化覆盖。

        当关键词很多时，把它们分到多条查询中，
        避免单条查询过于复杂导致命中太少。
        """
        queries: list[str] = []

        # 查询 1：主关键词 + 同义词
        queries.append(self.compile(intent))

        # 如果有 must_include 短语，生成专门针对它们的查询
        if intent.must_include:
            focused = SearchIntent(
                keywords=intent.keywords,
                synonyms=intent.synonyms,
                must_include=intent.must_include[:2],  # 每轮 2 个
                year_range=intent.year_range,
                document_type=intent.document_type,
                subject_area=intent.subject_area,
            )
            queries.append(self.compile(focused))

        # 如果设了排除词，也生成一个不带排除的版本（更宽）
        if intent.exclude:
            broad = SearchIntent(
                keywords=intent.keywords,
                synonyms=intent.synonyms,
                must_include=intent.must_include,
                year_range=intent.year_range,
                document_type=intent.document_type,
                subject_area=intent.subject_area,
            )
            queries.append(self.compile(broad))

        return list(set(queries))  # 去重
