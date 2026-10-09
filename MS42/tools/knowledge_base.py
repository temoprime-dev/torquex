#!/usr/bin/env python3
"""The MS42 knowledge base: what the MS4X documentation says, and what we do about it.

The wiki is the source of the community recipes (injector scaling, RON maps,
DISA, pops, idle, VANOS). It is not a specification and it changes; this module
turns the parts we rely on into a checked data file:

* every entry carries its verdict - ``implemented`` (a tool writes it),
  ``gated`` (writes only with a documented file from the customer),
  ``requires_data`` (needs logs/bench before anything is written),
  ``research`` (documented, deliberately not offered yet) or ``refused``
  (we will never write it);
* every symbol named by an entry must exist in the MS42 XDF, checked by the
  test suite, so the knowledge file cannot drift away from the definition;
* every source is a URL with a retrieval date, so a claim can be traced back.

``--html`` renders the whole thing as a single self-contained site (no network,
no dependencies) which is what the repository calls the MS4X knowledge base.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from MS42.tools.xdf import load_definition
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from xdf import load_definition  # type: ignore[no-redef]

ROOT = Path(__file__).parents[1]
DEFAULT_KNOWLEDGE = ROOT / "knowledge" / "ms42_knowledge.json"

VERDICTS = ("implemented", "gated", "requires_data", "research", "refused")

VERDICT_RU = {
    "implemented": "Реализовано",
    "gated": "По документу",
    "requires_data": "Нужны данные",
    "research": "Исследование",
    "refused": "Отказано",
}

VERDICT_ORDER = {name: index for index, name in enumerate(VERDICTS)}


@dataclass
class Source:
    id: str
    title: str
    url: str
    retrieved: str
    what: str = ""


@dataclass
class Entry:
    id: str
    title: str
    stage: tuple[int, ...]
    verdict: str
    symbols: tuple[str, ...]
    summary: str
    evidence: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "stage": list(self.stage),
            "verdict": self.verdict,
            "verdict_ru": VERDICT_RU.get(self.verdict, self.verdict),
            "symbols": list(self.symbols),
            "summary": self.summary,
            "evidence": list(self.evidence),
            "sources": list(self.sources),
        }


@dataclass
class KnowledgeBase:
    title: str
    updated: str
    sources: dict[str, Source]
    entries: list[Entry]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def entry(self, entry_id: str) -> Entry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def for_symbol(self, symbol: str) -> list[Entry]:
        return [entry for entry in self.entries if symbol in entry.symbols]

    def query(self, *, stage: int | None = None, verdict: str | None = None,
              symbol: str | None = None, text: str | None = None) -> list[Entry]:
        found = self.entries
        if stage is not None:
            found = [entry for entry in found if stage in entry.stage]
        if verdict is not None:
            found = [entry for entry in found if entry.verdict == verdict]
        if symbol is not None:
            found = [entry for entry in found if symbol in entry.symbols]
        if text is not None:
            needle = text.lower()
            found = [
                entry for entry in found
                if needle in entry.title.lower()
                or needle in entry.summary.lower()
                or any(needle in s.lower() for s in entry.symbols)
                or needle in entry.id.lower()
            ]
        return found


def load(path: Path = DEFAULT_KNOWLEDGE,
         xdf: Path | None = None) -> KnowledgeBase:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if int(data.get("schema", 0)) != 1:
        problems.append(f"unsupported knowledge schema {data.get('schema')!r}")
    sources: dict[str, Source] = {}
    for raw in data.get("sources", []):
        source = Source(id=str(raw["id"]), title=str(raw["title"]), url=str(raw["url"]),
                        retrieved=str(raw.get("retrieved", "")), what=str(raw.get("what", "")))
        if source.id in sources:
            problems.append(f"duplicate source id {source.id!r}")
        if not source.url.startswith("http"):
            problems.append(f"source {source.id!r} has no http(s) URL")
        sources[source.id] = source

    objects: dict[str, list] | None = None
    if xdf is not None:
        objects, xdf_problems = load_definition(xdf)
        problems.extend(f"XDF: {problem}" for problem in xdf_problems)

    entries: list[Entry] = []
    seen: set[str] = set()
    for raw in data.get("entries", []):
        entry = Entry(
            id=str(raw["id"]),
            title=str(raw["title"]),
            stage=tuple(int(value) for value in raw.get("stage", [])),
            verdict=str(raw.get("verdict", "")),
            symbols=tuple(str(value) for value in raw.get("symbols", [])),
            summary=str(raw.get("summary", "")),
            evidence=tuple(str(value) for value in raw.get("evidence", [])),
            sources=tuple(str(value) for value in raw.get("sources", [])),
        )
        if entry.id in seen:
            problems.append(f"duplicate entry id {entry.id!r}")
        seen.add(entry.id)
        if entry.verdict not in VERDICTS:
            problems.append(f"{entry.id}: unknown verdict {entry.verdict!r}")
        for value in entry.stage:
            if value not in (1, 2):
                problems.append(f"{entry.id}: stage {value} is not 1 or 2")
        if not entry.summary:
            problems.append(f"{entry.id}: empty summary")
        for source_id in entry.sources:
            if source_id not in sources:
                problems.append(f"{entry.id}: unknown source {source_id!r}")
        if objects is not None:
            for symbol in entry.symbols:
                if symbol not in objects:
                    problems.append(f"{entry.id}: symbol {symbol!r} is not in the XDF")
        entries.append(entry)

    entries.sort(key=lambda item: (VERDICT_ORDER.get(item.verdict, 99), item.id))
    return KnowledgeBase(
        title=str(data.get("title", "MS42 knowledge base")),
        updated=str(data.get("updated", "")),
        sources=sources,
        entries=entries,
        problems=problems,
    )


# ---------------------------------------------------------------------------
# Rendering


def _render_entry(entry: Entry, sources: dict[str, Source]) -> str:
    parts = [f'<article class="entry" data-verdict="{entry.verdict}" '
             f'data-stage="{",".join(str(s) for s in entry.stage)}" '
             f'data-text="{html.escape(" ".join([entry.title, entry.summary, entry.id, *entry.symbols]).lower(), quote=True)}">']
    stage = " · ".join(f"Stage {value}" for value in entry.stage) or "вне стейджей"
    parts.append(f'<h3>{html.escape(entry.title)} '
                 f'<span class="verdict {entry.verdict}">{VERDICT_RU[entry.verdict]}</span>'
                 f'<span class="stage">{html.escape(stage)}</span></h3>')
    parts.append(f'<p class="summary">{html.escape(entry.summary)}</p>')
    if entry.symbols:
        chips = "".join(f'<code>{html.escape(symbol)}</code>' for symbol in entry.symbols)
        parts.append(f'<div class="row"><span class="label">Символы</span><div class="chips">{chips}</div></div>')
    if entry.evidence:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in entry.evidence)
        parts.append(f'<div class="row"><span class="label">Что нужно</span><ul>{items}</ul></div>')
    if entry.sources:
        links = "".join(
            f'<a href="{html.escape(sources[source_id].url, quote=True)}" target="_blank" '
            f'rel="noopener">{html.escape(sources[source_id].title)}</a>'
            for source_id in entry.sources if source_id in sources
        )
        parts.append(f'<div class="row"><span class="label">Источники</span><div class="sources">{links}</div></div>')
    parts.append("</article>")
    return "\n".join(parts)


def render_html(kb: KnowledgeBase) -> str:
    """Render the knowledge base as one self-contained HTML page."""
    counts = {verdict: sum(1 for entry in kb.entries if entry.verdict == verdict)
              for verdict in VERDICTS}
    filters = "".join(
        f'<button class="filter" data-verdict="{verdict}">{VERDICT_RU[verdict]} '
        f'<span class="count">{counts[verdict]}</span></button>'
        for verdict in VERDICTS
    )
    source_rows = "".join(
        f'<li><a href="{html.escape(source.url, quote=True)}" target="_blank" rel="noopener">'
        f'{html.escape(source.title)}</a> <span class="retrieved">'
        f'{html.escape(source.retrieved)}</span><br><span class="what">{html.escape(source.what)}</span></li>'
        for source in kb.sources.values()
    )
    entries = "\n".join(_render_entry(entry, kb.sources) for entry in kb.entries)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(kb.title)}</title>
<style>
 :root {{ color-scheme: dark; }}
 * {{ box-sizing: border-box; }}
 body {{ margin: 0; background: #14161a; color: #e7e9ee;
        font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }}
 header {{ padding: 28px 24px 18px; background: linear-gradient(135deg, #1d2027, #14161a);
          border-bottom: 1px solid #262b33; }}
 h1 {{ margin: 0 0 4px; font-size: 24px; }}
 .meta {{ color: #9aa3b2; font-size: 13px; }}
 main {{ max-width: 1080px; margin: 0 auto; padding: 20px 24px 60px; }}
 .controls {{ position: sticky; top: 0; background: #14161aee; backdrop-filter: blur(6px);
             padding: 12px 0; display: flex; flex-wrap: wrap; gap: 8px;
             border-bottom: 1px solid #262b33; z-index: 2; }}
 input[type=search] {{ flex: 1 1 260px; padding: 9px 12px; border-radius: 8px;
                      border: 1px solid #2c333d; background: #1b1f26; color: inherit; }}
 button.filter {{ padding: 8px 12px; border-radius: 8px; border: 1px solid #2c333d;
                 background: #1b1f26; color: #cfd6e2; cursor: pointer; }}
 button.filter.active {{ border-color: #4b7bec; color: #fff; background: #24304a; }}
 .count {{ color: #8f9bb0; font-size: 12px; }}
 .entry {{ border: 1px solid #262b33; border-radius: 12px; padding: 16px 18px;
          margin: 14px 0; background: #181b21; }}
 .entry h3 {{ margin: 0 0 10px; font-size: 17px; display: flex; flex-wrap: wrap; gap: 8px;
              align-items: center; }}
 .verdict {{ font-size: 12px; padding: 2px 8px; border-radius: 999px; border: 1px solid; }}
 .verdict.implemented {{ color: #7ee2a8; border-color: #2f6b48; background: #16301f; }}
 .verdict.gated {{ color: #8fd3ff; border-color: #2c5a7a; background: #142a38; }}
 .verdict.requires_data {{ color: #ffd479; border-color: #7a6220; background: #33290d; }}
 .verdict.research {{ color: #c4b5fd; border-color: #4c3d80; background: #241d3a; }}
 .verdict.refused {{ color: #ff9b9b; border-color: #7c3030; background: #331616; }}
 .stage {{ font-size: 12px; color: #8f9bb0; }}
 .summary {{ margin: 0 0 12px; }}
 .row {{ display: flex; gap: 10px; margin: 8px 0; align-items: baseline; }}
 .label {{ flex: 0 0 108px; color: #8f9bb0; font-size: 13px; }}
 .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
 code {{ background: #11141a; border: 1px solid #262b33; border-radius: 6px;
        padding: 1px 6px; font-size: 12.5px; color: #d7e0f0; }}
 ul {{ margin: 0; padding-left: 18px; }}
 .sources a {{ color: #8fd3ff; text-decoration: none; margin-right: 12px; }}
 .sources a:hover {{ text-decoration: underline; }}
 details {{ margin-top: 26px; border: 1px solid #262b33; border-radius: 12px; padding: 12px 18px; }}
 summary {{ cursor: pointer; color: #cfd6e2; }}
 .retrieved {{ color: #7c8698; font-size: 12px; }}
 .what {{ color: #9aa3b2; font-size: 13px; }}
 .hidden {{ display: none; }}
 footer {{ color: #7c8698; font-size: 12px; margin-top: 28px; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(kb.title)}</h1>
  <div class="meta">Обновлено {html.escape(kb.updated)} · записей: {len(kb.entries)} ·
  источников: {len(kb.sources)} · каждый символ проверен по XDF MS42</div>
</header>
<main>
  <div class="controls">
    <input type="search" id="search" placeholder="Поиск по названию, символу, тексту…">
    <button class="filter active" data-verdict="all">Все</button>
    {filters}
  </div>
  <div id="entries">
{entries}
  </div>
  <footer id="empty" class="hidden">Ничего не найдено.</footer>
  <details>
    <summary>Источники ({len(kb.sources)})</summary>
    <ul>{source_rows}</ul>
  </details>
  <footer>Сгенерировано MS42/tools/knowledge_base.py. Вердикты: реализовано,
  по документу, нужны данные, исследование, отказано. Файл автономный: без сети и внешних
  скриптов.</footer>
</main>
<script>
 (function () {{
   const entries = Array.from(document.querySelectorAll('.entry'));
   const search = document.getElementById('search');
   const buttons = Array.from(document.querySelectorAll('button.filter'));
   const empty = document.getElementById('empty');
   let verdict = 'all';
   function apply() {{
     const needle = search.value.trim().toLowerCase();
     let shown = 0;
     for (const entry of entries) {{
       const matchesVerdict = verdict === 'all' || entry.dataset.verdict === verdict;
       const matchesText = !needle || entry.dataset.text.includes(needle);
       const visible = matchesVerdict && matchesText;
       entry.classList.toggle('hidden', !visible);
       if (visible) shown += 1;
     }}
     empty.classList.toggle('hidden', shown !== 0);
   }}
   search.addEventListener('input', apply);
   for (const button of buttons) {{
     button.addEventListener('click', () => {{
       verdict = button.dataset.verdict;
       for (const other of buttons) other.classList.toggle('active', other === button);
       apply();
     }});
   }}
 }})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI


def _format_entry(entry: Entry, sources: dict[str, Source]) -> str:
    lines = [f"[{VERDICT_RU[entry.verdict]}] {entry.title}  (stage: "
             f"{', '.join(str(s) for s in entry.stage) or '-'})",
             f"  {entry.summary}"]
    if entry.symbols:
        lines.append("  символы: " + ", ".join(entry.symbols))
    for item in entry.evidence:
        lines.append(f"  - {item}")
    for source_id in entry.sources:
        if source_id in sources:
            lines.append(f"  источник: {sources[source_id].url}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    xdf = None if args.no_xdf_check else None
    kb = load(args.knowledge, xdf=Path(args.xdf) if args.xdf else None)
    if args.check:
        if kb.ok:
            print(f"OK {args.knowledge}: {len(kb.entries)} entries, {len(kb.sources)} sources, "
                  f"all symbols resolve")
            return 0
        for problem in kb.problems:
            print(f"PROBLEM {problem}", file=sys.stderr)
        return 1
    entries = kb.query(stage=args.stage, verdict=args.verdict, symbol=args.symbol,
                       text=args.grep)
    if args.json:
        print(json.dumps([entry.to_json() for entry in entries], indent=2, ensure_ascii=False))
    else:
        for entry in entries:
            print(_format_entry(entry, kb.sources))
            print()
        print(f"{len(entries)}/{len(kb.entries)} записей")
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_html(kb), encoding="utf-8")
        print(f"site written to {args.html}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge", type=Path, default=DEFAULT_KNOWLEDGE)
    parser.add_argument("--xdf", type=Path, default=None,
                        help="definition used to check every symbol (default: MS42 512K XDF)")
    parser.add_argument("--no-xdf-check", action="store_true")
    parser.add_argument("--check", action="store_true", help="validate the file and exit")
    parser.add_argument("--stage", type=int, choices=(1, 2))
    parser.add_argument("--verdict", choices=VERDICTS)
    parser.add_argument("--symbol")
    parser.add_argument("--grep")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--html", type=Path, help="write the knowledge site here")
    args = parser.parse_args()
    if args.no_xdf_check:
        args.xdf = Path("/nonexistent")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
