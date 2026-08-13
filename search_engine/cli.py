"""命令行接口。

用法:
    # 直接搜索
    python -m search_engine search "TITLE-ABS-KEY(photocuring) AND DOCTYPE(ar)" --limit 40

    # 搜索并导出 CSV
    python -m search_engine search "TITLE-ABS-KEY(photocuring)" --csv results.csv --limit 60

    # 从意图编译搜索
    python -m search_engine compile --keywords photocuring,composite --synonyms "photocuring:photopolymerization,light-curing" --years 2020,2025

    # 检查 Scopus 可访问性
    python -m search_engine check

    # 查看缓存的论文数
    python -m search_engine stats
"""

import asyncio
import argparse
import logging
import sys
from pathlib import Path

from .engine import ScopusSearchEngine, ScopusAccessError
from .models import SearchIntent

logger = logging.getLogger("search_engine")


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def cmd_search(args):
    """执行搜索。"""
    async with ScopusSearchEngine(
        data_dir=args.data_dir,
        headless=not args.visible,
        humanize=args.humanize,
    ) as engine:
        engine.new_session()
        result = await engine.search(
            args.query,
            limit=args.limit,
            sort_by=args.sort,
        )

        if not result.papers:
            print("未找到结果。")
            return

        # 可选：LLM 相关性筛选
        if args.filter:
            from .relevance import RelevanceFilter
            from .llm import create_backend

            backend = create_backend(
                provider=args.provider,
                api_key=args.api_key,
                model=args.model,
            )
            rf = RelevanceFilter(backend)
            question = args.question or result.query
            scored = await rf.filter(
                result.papers,
                research_question=question,
                threshold=args.threshold,
                top_k=args.top_k,
            )
            print(f"\n查询: {result.query}")
            print(f"命中: {result.total_count} 篇 | 获取: {len(result.papers)} 篇")
            print(f"筛选后: {len(scored)} 篇 (阈值 ≥{args.threshold}%, top {args.top_k})")
            print(f"耗时: {result.time_taken:.1f}s\n")

            if args.csv and scored:
                # 导出带评分的筛选后结果
                path = engine.exporter.export_scored(scored, args.csv, args.question or result.query)
                print(f"已导出筛选后 CSV: {path}\n")

            for i, sp in enumerate(scored, 1):
                authors = ", ".join(str(a) for a in sp.paper.authors[:3])
                if len(sp.paper.authors) > 3:
                    authors += " et al."
                cat = f" | {sp.category}" if sp.category else ""
                route = f" | 路线:{sp.route}" if sp.route else ""
                print(f"  {i:2d}. [{sp.score}%] [{sp.paper.year or '?'}]{cat}{route} {sp.paper.title[:100]}")
                if sp.reason:
                    print(f"      {sp.reason}")
                print(f"      {authors}")
        else:
            print(f"\n查询: {result.query}")
            print(f"命中: {result.total_count} 篇 | 获取: {len(result.papers)} 篇 | "
                  f"翻页: {result.pages_fetched} 页 | 耗时: {result.time_taken:.1f}s\n")

            for i, paper in enumerate(result.papers, 1):
                authors = ", ".join(str(a) for a in paper.authors[:3])
                if len(paper.authors) > 3:
                    authors += " et al."
                print(f"  {i:2d}. [{paper.year or '?'}] {paper.title[:100]}")
                print(f"      {authors}")

        # CSV 导出
        if args.csv:
            path = engine.to_csv(result, args.csv)
            print(f"\n已导出 CSV: {path}")

        # 成本
        cost = engine.get_cost_summary()
        print(f"\n成本: {cost.queries} 查询 | {cost.pages_loaded} 页 | "
              f"{cost.total_browser_time:.1f}s 浏览器时间")


async def cmd_login(args):
    """手动 SSO 登录（独立流程）。"""
    import json as _json
    from cloakbrowser import launch_async

    data_dir = Path(args.data_dir)
    profile_dir = data_dir / "scopus_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("  Scopus 机构 SSO 登录")
    print("=" * 60)
    print()
    print("  浏览器窗口即将打开。请按以下步骤操作：")
    print("  1. 在 Scopus 页面点击 'Sign in'")
    print("  2. 选择 'Sign in via your institution'")
    print("  3. 搜索并选择你的学校/机构")
    print("  4. 通过学校 SSO 页面完成登录")
    print("  5. 看到 Scopus 搜索页面后，回到此终端")
    print()
    print("=" * 60)
    print()

    browser = await launch_async(headless=False, humanize="none",
                                  args=["--start-maximized"])
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    await page.goto("https://www.scopus.com/search/form.uri?display=advanced")
    await asyncio.sleep(3)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "  登录完成后按 Enter 保存会话...")

    state = await context.storage_state()
    state_path = profile_dir / "state.json"
    state_path.write_text(_json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    await browser.close()

    print(f"\n  会话已保存: {state_path}")
    print("  立即运行: python -m search_engine search \"...\"\n")

async def cmd_record(args):
    """录制手动导出操作，抓取所有网络请求。"""
    import json as _json

    data_dir = Path(args.data_dir)
    output_dir = data_dir / "cache"
    output_dir.mkdir(parents=True, exist_ok=True)

    network_log: list[dict] = []

    def make_logger(page):
        async def on_request(request):
            url_lower = request.url.lower()
            if any(kw in url_lower for kw in
                   ["export", "download", "csv", "cto", "search-results", "document-search"]):
                network_log.append({
                    "type": "request",
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                })
                print(f"  → {request.method} {request.url[:250]}")

        async def on_response(response):
            url_lower = response.url.lower()
            if any(kw in url_lower for kw in
                   ["export", "download", "csv", "cto", "search-results", "document-search"]):
                try:
                    body = await response.text()
                    body_preview = body[:800]
                except Exception:
                    body_preview = "<无法读取>"
                network_log.append({
                    "type": "response",
                    "url": response.url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "body_preview": body_preview,
                })
                print(f"  ← {response.status} ({len(body_preview)}b) {response.url[:200]}")

        page.on("request", on_request)
        page.on("response", on_response)

    print("\n" + "=" * 60)
    print("  录制模式 — 抓取 Scopus 导出 API")
    print("=" * 60)

    # 用一个可见浏览器，加载已有会话
    async with ScopusSearchEngine(
        data_dir=args.data_dir,
        headless=False,  # 可见
        humanize="none",
    ) as engine:
        make_logger(engine._page)

        # 执行搜索
        print(f"\n  搜索: {args.query[:100]}...")
        await engine._navigate_and_search(args.query)
        count = await engine._get_result_count()
        print(f"  搜索结果: {count} 篇")

        print()
        print("  请手动操作：")
        print("  1. 勾选 'All' 全选")
        print("  2. 点击 'Export' → 'CSV'")
        print("  3. 选择字段 → 确认导出")
        print()
        print("  完成后按 Enter 保存抓取到的 API 请求...")
        print("=" * 60)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "  按 Enter 保存...")

        log_path = output_dir / "scopus_export_network_log.json"
        log_path.write_text(_json.dumps(network_log, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  已保存 {len(network_log)} 条请求: {log_path}")
        print("  完成。")
    """手动 SSO 登录（独立流程，不走 async with）。"""
    import sys as _sys
    from cloakbrowser import launch_async

    data_dir = Path(args.data_dir)
    profile_dir = data_dir / "scopus_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("  Scopus 机构 SSO 登录")
    print("=" * 60)
    print()
    print("  浏览器窗口即将打开。请按以下步骤操作：")
    print()
    print("  1. 在 Scopus 页面点击 'Sign in'")
    print("  2. 选择 'Sign in via your institution'")
    print("  3. 搜索并选择你的学校/机构")
    print("  4. 通过学校的 SSO 页面完成登录")
    print("  5. 看到 Scopus 搜索页面后，回到此终端")
    print()
    print("=" * 60)
    print()

    browser = await launch_async(
        headless=False,
        humanize="none",
        args=["--start-maximized", "--window-size=1280,900"],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        device_scale_factor=1,
    )
    page = await context.new_page()

    await page.goto("https://www.scopus.com/search/form.uri?display=advanced")
    await asyncio.sleep(3)

    # 自动处理 cookie 弹窗（改用 JS 点击，跨不同实现更稳定）
    try:
        cookie_texts = ["Accept all cookies", "Accept all", "I agree", "Accept", "同意", "同意所有"]
        for text in cookie_texts:
            try:
                await page.evaluate(f"""
                    [...document.querySelectorAll('button, a')]
                    .find(el => el.textContent.trim().toLowerCase() === '{text.lower()}')
                    ?.click()
                """)
                await asyncio.sleep(1)
            except Exception:
                pass
    except Exception:
        pass

    # 保存截图以便排查
    screenshot_path = data_dir / "cache" / "scopus_page_after_login.png"
    await page.screenshot(path=str(screenshot_path), full_page=False)
    print(f"  页面截图已保存: {screenshot_path}")

    # 检查是否停在 Scopus Preview → 需要点 "Check access"
    try:
        html = await page.content()
        if "Scopus Preview" in html or "Check access" in html:
            print("\n  检测到 Scopus Preview 模式，尝试获取完整机构访问...")
            check_btn = await page.query_selector(
                "a:has-text('Check access'), button:has-text('Check access'), "
                "a:has-text('检查访问'), button:has-text('检查访问')"
            )
            if check_btn:
                await check_btn.click()
                await asyncio.sleep(5)
                print("  已点击 'Check access'，请确认页面是否跳转到完整版 Scopus")
            else:
                print("  ⚠ 请手动点击页面上的 'Check access' 按钮获取完整访问权限")
    except Exception as e:
        print(f"  (机构访问检查: {e})")

    # 阻塞等待用户手动登录
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "  登录完成后按 Enter 保存会话...")

    # 保存登录状态
    state = await context.storage_state()
    import json
    state_path = profile_dir / "state.json"
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    await browser.close()

    print()
    print(f"  会话已保存: {state_path}")
    print("  后续搜索将自动使用此会话 (python -m search_engine search ...)")
    print()


async def cmd_generate(args):
    """LLM 生成 Scopus 查询。"""
    import os as _os
    from .query_generator import QueryGenerator
    from .llm import create_backend

    # 确定 API key
    api_key = args.api_key or _os.environ.get("DEEPSEEK_API_KEY")
    provider = args.provider

    if provider == "deepseek" and not api_key:
        print("错误: DeepSeek 需要 API key。\n"
              "  --api-key sk-...  或设置环境变量 DEEPSEEK_API_KEY")
        return

    backend = create_backend(
        provider=provider,
        api_key=api_key,
        model=args.model,
    )

    # 获取领域知识
    from .knowledge import get_domain_context
    domain_context = get_domain_context(args.domain)

    generator = QueryGenerator(backend)

    print(f"\n研究问题: {args.question}")
    print(f"领域: {args.domain} ({len(domain_context)} 字符领域知识)")
    print(f"后端: {provider}")
    print(f"\n正在生成 {args.n_queries} 条查询...\n")

    queries = await generator.generate(
        args.question,
        domain_context=domain_context,
        n_queries=args.n_queries,
    )

    for i, q in enumerate(queries, 1):
        print(f"  [{i}] {q}")

    if not queries:
        print("  (未生成有效查询)")
        return

    print()

    # 可选：立即搜索（迭代模式）
    if args.search:
        print("-" * 60)
        from .iterative_searcher import IterativeSearcher
        from .knowledge import get_domain_context

        async with ScopusSearchEngine(
            data_dir=args.data_dir,
            headless=True,
        ) as engine:
            engine.new_session()

            if args.filter:
                # 迭代搜索模式
                print(f"迭代搜索: 目标 {args.top_k} 篇 ≥ {args.threshold}%\n")
                from .citation_tracker import CitationTracker
                citation_tracker = CitationTracker(mailto=args.mailto) if args.citations else None
                searcher = IterativeSearcher(
                    backend=backend,
                    engine=engine,
                    citation_tracker=citation_tracker,
                )
                scored = await searcher.search(
                    research_question=args.question,
                    domain_context=get_domain_context(args.domain),
                    target_count=args.top_k,
                    threshold=args.threshold,
                    max_rounds=3,
                )

                if scored and args.csv:
                    path = engine.exporter.export_scored(scored, args.csv, args.question)
                    print(f"\n导出: {path}")

                print("\n结果:")
                for i, sp in enumerate(scored, 1):
                    authors = ", ".join(str(a) for a in sp.paper.authors[:3])
                    if len(sp.paper.authors) > 3:
                        authors += " et al."
                    cat = f" | {sp.category}" if sp.category else ""
                    route = f" | 路线:{sp.route}" if sp.route else ""
                    print(f"  {i:2d}. [{sp.score}%] [{sp.paper.year or '?'}]{cat}{route} {sp.paper.title[:100]}")
                    if sp.reason:
                        print(f"      {sp.reason}")
                    print(f"      {authors}")

                cost = engine.get_cost_summary()
                print(f"\n总计: {len(scored)} 篇达标 | "
                      f"{cost.queries} 查询 | {cost.total_browser_time:.0f}s")

            else:
                # 无过滤：简单多查询搜索
                all_papers: list = []
                for i, q in enumerate(queries):
                    print(f"\n[{i+1}/{len(queries)}] 搜索: {q[:100]}...")
                    result = await engine.search(q, limit=args.limit)
                    print(f"  命中: {result.total_count} | 获取: {len(result.papers)}")
                    all_papers.extend(result.papers)

                seen = set()
                unique = []
                for p in all_papers:
                    if p.paper_id not in seen:
                        seen.add(p.paper_id)
                        unique.append(p)
                print(f"\n去重后: {len(unique)} 篇")

                if args.csv:
                    path = engine.exporter.export(unique, args.csv)
                    print(f"导出: {path}")

                cost = engine.get_cost_summary()
                print(f"\n总计: {len(unique)} 篇 | "
                      f"{cost.queries} 查询 | {cost.total_browser_time:.0f}s")

async def cmd_check(args):
    """检查 Scopus 连接。"""
    try:
        async with ScopusSearchEngine(
            data_dir=args.data_dir,
            headless=not args.visible,
        ) as engine:
            print("✅ Scopus 可访问（机构 IP 已识别）")
    except ScopusAccessError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except ImportError as e:
        print(f"❌ {e}")
        sys.exit(1)


async def cmd_compile(args):
    """编译搜索意图为 Scopus 查询（不执行搜索）。"""
    from .compiler import ScopusQueryCompiler

    compiler = ScopusQueryCompiler()

    keywords = [k.strip() for k in args.keywords.split(",")]
    synonyms = {}
    if args.synonyms:
        for pair in args.synonyms.split("|"):
            key, values = pair.split(":", 1)
            synonyms[key.strip()] = [v.strip() for v in values.split(",")]

    must_include = [m.strip() for m in args.must_include.split(",")] if args.must_include else []
    exclude = [e.strip() for e in args.exclude.split(",")] if args.exclude else []

    year_range = None
    if args.years:
        parts = args.years.split(",")
        if len(parts) == 2:
            year_range = (int(parts[0]), int(parts[1]))

    intent = SearchIntent(
        keywords=keywords,
        synonyms=synonyms,
        must_include=must_include,
        exclude=exclude,
        author=args.author,
        year_range=year_range,
        document_type=args.doctype,
    )

    print("SearchIntent:")
    print(f"  keywords:     {intent.keywords}")
    print(f"  synonyms:     {intent.synonyms}")
    print(f"  must_include: {intent.must_include}")
    print(f"  exclude:      {intent.exclude}")
    print(f"  author:       {intent.author}")
    print(f"  year_range:   {intent.year_range}")
    print(f"  doctype:      {intent.document_type}")
    print()

    if args.multi:
        queries = compiler.compile_multi(intent)
        print(f"生成 {len(queries)} 条互补查询:\n")
        for i, q in enumerate(queries, 1):
            print(f"  [{i}] {q}")
    else:
        query = compiler.compile(intent)
        print(f"Scopus 查询:\n\n  {query}")


def cmd_stats(args):
    """缓存统计。"""
    from .cache import SearchCache
    cache = SearchCache(args.data_dir + "/cache/scopus_cache.db")
    cache.init_tables()
    papers = cache.get_all_papers()
    print(f"缓存论文: {len(papers)} 篇")
    cache.close()


def main():
    parser = argparse.ArgumentParser(
        description="材料学科知识库 — Scopus 搜索组件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data-dir", default="data", help="数据目录 (默认: data)")
    parser.add_argument("--visible", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--humanize", default="careful",
                        choices=["default", "careful", "none"],
                        help="人类化级别 (默认: careful)")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")

    sub = parser.add_subparsers(dest="command", help="子命令")

    # search
    p_search = sub.add_parser("search", help="执行 Scopus 高级搜索")
    p_search.add_argument("query", help="Scopus 高级搜索字符串")
    p_search.add_argument("--limit", type=int, default=20, help="最多返回论文数")
    p_search.add_argument("--sort", default="relevance",
                           choices=["relevance", "date", "citations"])
    p_search.add_argument("--csv", help="导出 CSV 文件名")
    p_search.add_argument("--filter", action="store_true", help="LLM 相关性筛选")
    p_search.add_argument("--question", help="筛选时的研究问题（默认用查询字符串）")
    p_search.add_argument("--threshold", type=int, default=70, help="相关性阈值 (0-100)")
    p_search.add_argument("--top-k", type=int, default=20, help="最多保留篇数")
    p_search.add_argument("--provider", default="ollama",
                           choices=["deepseek", "ollama"], help="筛选用 LLM 后端")
    p_search.add_argument("--api-key", help="DeepSeek API key")
    p_search.add_argument("--model", help="筛选用模型名")

    # login
    p_login = sub.add_parser("login", help="手动 SSO 登录 Scopus（首次使用）")

    # record
    p_record = sub.add_parser("record", help="录制手动导出操作（抓取 API 请求）")
    p_record.add_argument("query", help="Scopus 高级搜索字符串")

    # generate
    p_gen = sub.add_parser("generate", help="LLM 生成 Scopus 查询（自然语言 → 查询）")
    p_gen.add_argument("question", help="研究问题（自然语言）")
    p_gen.add_argument("--provider", default="deepseek",
                        choices=["deepseek", "ollama"], help="LLM 后端")
    p_gen.add_argument("--api-key", help="DeepSeek API key (或环境变量 DEEPSEEK_API_KEY)")
    p_gen.add_argument("--model", help="模型名 (deepseek: deepseek-chat, ollama: qwen3:8b)")
    p_gen.add_argument("--n-queries", type=int, default=4, help="生成查询数")
    p_gen.add_argument("--search", action="store_true", help="生成后立即执行搜索")
    p_gen.add_argument("--limit", type=int, default=20, help="搜索返回数")
    p_gen.add_argument("--csv", help="导出 CSV 文件名")
    p_gen.add_argument("--domain", default="photocuring", help="领域 (photocuring/ml/motor)")
    p_gen.add_argument("--filter", action="store_true", help="LLM 相关性筛选结果")
    p_gen.add_argument("--threshold", type=int, default=70, help="相关性阈值 (0-100)")
    p_gen.add_argument("--top-k", type=int, default=20, help="最多保留篇数")
    p_gen.add_argument("--citations", action="store_true", help="启用引文追踪（OpenAlex 向前/向后/共被引）")
    p_gen.add_argument("--mailto", help="OpenAlex polite pool 邮箱")

    # check
    p_check = sub.add_parser("check", help="检查 Scopus 连接/登录状态")

    # compile
    p_compile = sub.add_parser("compile", help="编译 SearchIntent → Scopus 查询")
    p_compile.add_argument("--keywords", required=True, help="关键词，逗号分隔")
    p_compile.add_argument("--synonyms", help='同义词映射: "kw:syn1,syn2|kw2:syn1"')
    p_compile.add_argument("--must-include", help="必须包含短语，逗号分隔")
    p_compile.add_argument("--exclude", help="排除词，逗号分隔")
    p_compile.add_argument("--author", help="作者")
    p_compile.add_argument("--years", help="年份范围: 2020,2025")
    p_compile.add_argument("--doctype", choices=["ar", "re", "cp"], help="文献类型")
    p_compile.add_argument("--multi", action="store_true", help="生成多条互补查询")

    # stats
    p_stats = sub.add_parser("stats", help="缓存统计")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "login":
        asyncio.run(cmd_login(args))
    elif args.command == "record":
        asyncio.run(cmd_record(args))
    elif args.command == "generate":
        asyncio.run(cmd_generate(args))
    elif args.command == "search":
        asyncio.run(cmd_search(args))
    elif args.command == "check":
        asyncio.run(cmd_check(args))
    elif args.command == "compile":
        asyncio.run(cmd_compile(args))
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
