# ruff: noqa: E501  (long lines live inside the HTML/CSS template)
"""Render an authoring Test JSON as a self-contained HTML page for manual review.

Usage:
    python -m scripts.review_html --file path/to/test.json [--notes NOTES.md] [--out REVIEW.html]

The page is written next to the JSON by default (REVIEW.html) and references the
asset files (images / mp3) by their file names, so it works when opened straight
from disk as long as the assets sit in the same folder (the ingestion import
folders are laid out that way).

It shows everything a teacher needs to vet a draft BEFORE approving it: every
section's stimulus (passage text, pictures, audio players), every question with its
options, the answer key highlighted, accepted / variant / rejected gap-fill forms and
spelling mode, writing tasks with bullets / word minimum / rubric (and "one of N" for a
choose-one group), the B2-C2 fields (key word transformations: key word, gapped
sentence and every A | B answer pattern; word-formation `stem_word`; `max_words`;
per-item points; the section's `reported_skill`; a matching pool's
`each_option_once` and its own recording), and the ingest
agent's NOTES.md at the top. Each question has an "OK" tick and a comment box; the
marks live in the browser's localStorage and "Скопировать замечания" copies them as
plain text.

Teacher-only material (contains the answer key): never serve it to students.

Every value from the JSON / NOTES.md is HTML-escaped (`esc`) in markup and written
with `js_str` inside the inline script, so a hostile id / title / prompt cannot
inject markup or close the script. `make review-test file=...` runs it.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

from app.core.logging import configure_logging, get_logger

log = get_logger("scripts.review_html")

SKILL_RU = {"reading": "Чтение", "writing": "Письмо", "listening": "Аудирование"}
TYPE_RU = {
    "single_choice": "выбор ответа",
    "gap_fill": "вписать слово",
    "matching": "соотнесение",
    "open_writing": "письменное задание",
    "colour_task": "раскраска",
    "key_word_transformation": "трансформация с ключевым словом",
}
REPORTED_RU = {
    "reading": "Reading",
    "use_of_english": "Use of English",
    "listening": "Listening",
    "writing": "Writing",
}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def js_str(value: object) -> str:
    r"""A JS string literal safe INSIDE an inline <script>.

    `json.dumps` alone leaves `</script>` (and `<!--`) intact, so a hostile test
    id / title could close the script element and inject markup. `<`, `>` and `&`
    are written as < / > / & — the same string for JS, inert for
    the HTML parser (`ensure_ascii` already escapes U+2028 / U+2029)."""
    return (
        json.dumps("" if value is None else str(value), ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def md_to_html(text: str) -> str:
    """A tiny, safe Markdown subset for NOTES.md: headings, bullets, bold, code, tables."""
    out: list[str] = []
    in_list = False
    in_table = False
    for raw in text.splitlines():
        line = raw.rstrip()
        cells = (
            [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("|")
            else None
        )
        if cells is not None:
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not in_table:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append("<table class='md'>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        b = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$", line)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
        elif b:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(b.group(1))}</li>")
        elif not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def inline(text: str) -> str:
    s = esc(text)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s


def media(asset_id: str | None, alt: str = "") -> str:
    if not asset_id:
        return ""
    if asset_id.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
        return f"<audio controls preload='none' src='{esc(asset_id)}'></audio> <code>{esc(asset_id)}</code>"
    return f"<img src='{esc(asset_id)}' alt='{esc(alt)}' title='{esc(asset_id)}'>"


def render_stimulus(st: dict) -> str:
    kind = st.get("kind")
    parts: list[str] = [f"<div class='kind'>стимул: {esc(kind)}</div>"]
    if kind == "audio_asset":
        flags = []
        if st.get("locked"):
            flags.append("заблокировано (без паузы/перемотки)")
        flags.append(f"проигрываний: {esc(st.get('plays'))}")
        parts.append(
            f"<div class='audio'><b>Основная запись</b> ({', '.join(flags)}):<br>{media(st.get('asset_id'))}</div>"
        )
        if st.get("preview_asset_id"):
            parts.append(
                f"<div class='audio'><b>Пробная запись / инструкция</b> (можно переслушивать):<br>{media(st.get('preview_asset_id'))}</div>"
            )
    if st.get("text"):
        parts.append(f"<div class='passage'>{esc(st['text'])}</div>")
    for a in st.get("asset_ids") or []:
        parts.append(media(a))
    for a in st.get("images") or []:
        parts.append(
            media(
                a if isinstance(a, str) else a.get("asset_id"),
                "" if isinstance(a, str) else a.get("alt", ""),
            )
        )
    if kind == "matching_pool":
        audio = st.get("audio") if isinstance(st.get("audio"), dict) else None
        if audio and audio.get("asset_id"):
            parts.append(
                f"<div class='audio'><b>Запись этой части</b> (проигрываний: {esc(audio.get('plays'))}; "
                f"та же запись в соседних разделах = одна дорожка):<br>{media(audio.get('asset_id'))}</div>"
            )
            if audio.get("preview_asset_id"):
                parts.append(
                    f"<div class='audio'><b>Пробная запись / инструкция</b>:<br>{media(audio.get('preview_asset_id'))}</div>"
                )
        parts.append(
            "<div class='sub'>"
            + (
                "Каждый вариант — не больше одного раза (gapped text, один лишний)"
                if st.get("each_option_once")
                else "Варианты можно выбирать повторно"
            )
            + "</div>"
        )
        rows = []
        for o in st.get("options") or []:
            body = (
                esc(o.get("text"))
                if o.get("kind", "text") == "text"
                else media(o.get("asset_id"), o.get("alt") or "")
            )
            rows.append(f"<li><b>{esc(o.get('key'))}</b> — {body}</li>")
        parts.append(
            "<div><b>Варианты для соотнесения:</b><ul class='pool'>" + "".join(rows) + "</ul></div>"
        )
    return "\n".join(parts)


def render_item(it: dict, n: int, pool_keys: list[str], choose_of: int = 0) -> str:
    """One question. `choose_of` = how many optional writing tasks its section offers."""
    t = it.get("item_type")
    rows: list[str] = []
    if it.get("prompt"):
        rows.append(f"<div class='prompt'>{esc(it['prompt'])}</div>")
    if it.get("image"):
        rows.append(media(it["image"]))
    if t == "single_choice":
        opts = []
        for o in it.get("options") or []:
            ok = o.get("key") == it.get("correct")
            body = (
                esc(o.get("text"))
                if o.get("kind", "text") == "text"
                else media(o.get("asset_id"), o.get("alt") or "")
            )
            opts.append(
                f"<li class='{'correct' if ok else ''}'><b>{esc(o.get('key'))}</b> {body}{' ✓' if ok else ''}</li>"
            )
        pics = any(o.get("kind") == "image" for o in it.get("options") or [])
        rows.append(f"<ul class='opts{' pics' if pics else ''}'>" + "".join(opts) + "</ul>")
        if not any(o.get("key") == it.get("correct") for o in it.get("options") or []):
            rows.append(
                f"<div class='warn'>Ключ «{esc(it.get('correct'))}» не совпадает ни с одним вариантом!</div>"
            )
    elif t == "matching":
        rows.append(f"<div class='key'>Ответ: <b>{esc(it.get('correct'))}</b></div>")
        if pool_keys and it.get("correct") not in pool_keys:
            rows.append(
                f"<div class='warn'>Ответа «{esc(it.get('correct'))}» нет среди вариантов раздела!</div>"
            )
    elif t == "gap_fill":
        rows.append(
            f"<div class='key'>Принимается: <b>{esc(' | '.join(it.get('accepted') or []))}</b></div>"
        )
        if it.get("accepted_variants"):
            rows.append(
                f"<div class='sub'>Допустимые написания: {esc(' | '.join(it['accepted_variants']))}</div>"
            )
        if it.get("rejected"):
            rows.append(f"<div class='sub'>Не засчитывать: {esc(' | '.join(it['rejected']))}</div>")
        if it.get("spelling"):
            rows.append(f"<div class='sub'>Орфография: <b>{esc(it['spelling'])}</b></div>")
        if it.get("stem_word"):
            rows.append(f"<div class='sub'>Исходное слово: <b>{esc(it['stem_word'])}</b></div>")
        if it.get("max_words"):
            rows.append(f"<div class='sub'>Не больше слов: {esc(it['max_words'])}</div>")
    elif t == "key_word_transformation":
        rows.append(f"<div>Ключевое слово: <b>{esc(it.get('key_word'))}</b></div>")
        rows.append(f"<div>Второе предложение: {esc(it.get('gapped_sentence'))}</div>")
        patterns = [
            f"<li><b>{esc(' / '.join(a.get('part_a') or []))}</b> | "
            f"<b>{esc(' / '.join(a.get('part_b') or []))}</b></li>"
            for a in it.get("answers") or []
            if isinstance(a, dict)
        ]
        rows.append(
            "<div class='key'>Принимается (половина A | половина B, по 1 баллу):<ul>"
            + "".join(patterns)
            + "</ul></div>"
        )
        if not patterns:
            rows.append("<div class='warn'>Нет ни одного варианта ответа!</div>")
        if it.get("min_words") or it.get("max_words"):
            rows.append(
                f"<div class='sub'>Слов (своё ограничение): {esc(it.get('min_words') or '—')}–"
                f"{esc(it.get('max_words') or '—')}</div>"
            )
    elif t == "open_writing":
        meta = [
            f"минимум слов: {esc(it.get('word_min'))}",
            f"проверка: {esc(it.get('grade_mode'))}",
        ]
        if it.get("optional"):
            meta.append(f"<b>на выбор (одно из {esc(choose_of or 2)})</b>")
        rows.append(f"<div class='sub'>{' · '.join(meta)}</div>")
        if it.get("bullet_points"):
            rows.append(
                "<ul>" + "".join(f"<li>{esc(b)}</li>" for b in it["bullet_points"]) + "</ul>"
            )
        if it.get("rubric"):
            rows.append(
                f"<details><summary>Рубрика для проверяющего</summary><div class='passage'>{esc(it['rubric'])}</div></details>"
            )
    elif t == "colour_task":
        rows.append(media(it.get("asset_id")))
        rows.append(f"<div class='sub'>Палитра: {esc(', '.join(it.get('palette') or []))}</div>")
        rows.append(f"<div class='key'>Ключ: <b>{esc(it.get('key'))}</b></div>")
    if it.get("points"):
        rows.append(f"<div class='sub'>Баллы: {esc(it['points'])}</div>")
    iid = esc(it.get("id"))
    return f"""
<div class="item" data-id="{iid}">
  <div class="ihead"><span class="num">{n}</span> <code>{iid}</code> <span class="type">{esc(TYPE_RU.get(t, t))}</span>
    <label class="ok"><input type="checkbox" data-ok="{iid}"> OK</label></div>
  {"".join(rows)}
  <textarea class="cm" data-cm="{iid}" placeholder="Замечание (необязательно)"></textarea>
</div>"""


def render(test: dict, notes: str | None) -> str:
    sections_html: list[str] = []
    counts: dict[str, int] = {}
    n = 0
    for s in test.get("sections", []):
        st = s.get("stimulus") or {}
        pool = (
            [o.get("key") for o in st.get("options") or []]
            if st.get("kind") == "matching_pool"
            else []
        )
        choose_of = sum(
            1
            for it in s.get("items", [])
            if it.get("item_type") == "open_writing" and it.get("optional")
        )
        items = []
        for it in s.get("items", []):
            n += 1
            counts[it.get("item_type")] = counts.get(it.get("item_type"), 0) + 1
            items.append(render_item(it, n, pool, choose_of))
        skill = SKILL_RU.get(s.get("skill"), s.get("skill") or "")
        reported = s.get("reported_skill")
        reported_html = (
            f"<div class='sub'>Баллы засчитываются как: <b>{esc(REPORTED_RU.get(reported, reported))}</b></div>"
            if reported
            else ""
        )
        sections_html.append(f"""
<section id="{esc(s.get("id"))}">
  <h2><span class="skill">{esc(skill)}</span> {esc(s.get("title") or s.get("id"))} <code>{esc(s.get("id"))}</code></h2>
  {reported_html}
  {render_stimulus(st)}
  {"".join(items)}
</section>""")
    toc = "".join(
        f"<a href='#{esc(s.get('id'))}'>{esc(s.get('title') or s.get('id'))}</a>"
        for s in test.get("sections", [])
    )
    summary = ", ".join(f"{esc(TYPE_RU.get(k, k))}: {v}" for k, v in counts.items())
    notes_html = (
        f"<details open class='notes'><summary>Заметки агента (NOTES.md)</summary>{md_to_html(notes)}</details>"
        if notes
        else ""
    )
    key = f"review:{test.get('id')}"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ревью: {esc(test.get("title"))}</title>
<style>
:root {{ --bg:#fafaf7; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#e3e1da; --ok:#e6f4ea; --okb:#2e7d32; --warn:#fdecea; --accent:#3557a5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17181a; --fg:#e8e6e1; --muted:#9a988f; --card:#212226; --line:#33353a; --ok:#1f3a26; --okb:#7cc68a; --warn:#4a2020; --accent:#8fa8e8; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif }}
header {{ position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line); padding:10px 16px }}
header h1 {{ margin:0; font-size:18px }}
.meta {{ color:var(--muted); font-size:13px }}
.toc {{ display:flex; flex-wrap:wrap; gap:4px 10px; font-size:13px; margin-top:6px }}
.toc a {{ color:var(--accent); text-decoration:none }}
main {{ max-width:960px; margin:0 auto; padding:16px }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:16px 0 }}
h2 {{ font-size:17px; margin:0 0 8px }}
.skill {{ background:var(--accent); color:#fff; border-radius:999px; padding:1px 8px; font-size:12px; vertical-align:middle }}
.kind {{ color:var(--muted); font-size:12px }}
.passage {{ white-space:pre-wrap; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:10px; margin:8px 0 }}
img {{ max-width:100%; max-height:360px; border:1px solid var(--line); border-radius:6px; margin:4px; vertical-align:middle; background:#fff }}
.opts img {{ max-height:160px }}
.opts.pics {{ display:flex; flex-wrap:wrap; gap:6px }} .opts.pics li {{ border:1px solid var(--line) }}
audio {{ width:min(100%,520px); vertical-align:middle }}
.audio {{ margin:6px 0 }}
.item {{ border-top:1px dashed var(--line); padding:10px 0 }}
.item.done {{ opacity:.6 }}
.ihead {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap }}
.num {{ font-weight:700 }}
.type {{ color:var(--muted); font-size:12px }}
.ok {{ margin-left:auto; font-size:13px }}
.prompt {{ margin:4px 0; font-weight:500 }}
.opts {{ list-style:none; padding:0; margin:4px 0 }}
.opts li {{ padding:3px 8px; border-radius:6px; margin:2px 0 }}
.correct {{ background:var(--ok); color:var(--okb); font-weight:600 }}
.key {{ background:var(--ok); color:var(--okb); padding:3px 8px; border-radius:6px; display:inline-block; margin:2px 0 }}
.sub {{ color:var(--muted); font-size:13px }}
.warn {{ background:var(--warn); padding:4px 8px; border-radius:6px; font-weight:600 }}
.cm {{ width:100%; min-height:30px; margin-top:6px; font:inherit; font-size:13px; background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:4px 6px }}
.notes {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 16px }}
.notes summary {{ font-weight:700; cursor:pointer }}
table.md {{ border-collapse:collapse; font-size:13px }} table.md td {{ border:1px solid var(--line); padding:3px 6px }}
.bar {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:6px }}
button {{ font:inherit; font-size:13px; padding:4px 10px; border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--fg); cursor:pointer }}
code {{ font-size:12px; color:var(--muted) }}
</style></head>
<body>
<header>
  <h1>Ревью черновика: {esc(test.get("title"))}</h1>
  <div class="meta"><code>{esc(test.get("id"))}</code> · {esc(test.get("level"))} · {esc(test.get("duration_minutes"))} мин · вопросов: {n} ({summary}) · статус: {esc(test.get("status"))}</div>
  <div class="bar"><span id="progress" class="meta"></span>
    <button id="copy">Скопировать замечания</button><button id="reset">Сбросить отметки</button></div>
  <details><summary class="meta">Разделы</summary><nav class="toc">{toc}</nav></details>
</header>
<main>
{notes_html}
{"".join(sections_html)}
</main>
<script>
(function () {{
  var KEY = {js_str(key)};
  var state = {{}};
  try {{ state = JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch (e) {{ state = {{}}; }}
  function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(state)); }} catch (e) {{}} }}
  var items = document.querySelectorAll(".item");
  function progress() {{
    var done = 0; items.forEach(function (el) {{ if ((state[el.dataset.id] || {{}}).ok) done++; }});
    document.getElementById("progress").textContent = "Проверено: " + done + " из " + items.length;
  }}
  items.forEach(function (el) {{
    var id = el.dataset.id, s = state[id] || {{}};
    var box = el.querySelector("[data-ok]"), cm = el.querySelector("[data-cm]");
    box.checked = !!s.ok; cm.value = s.cm || ""; el.classList.toggle("done", !!s.ok);
    box.onchange = function () {{ state[id] = Object.assign({{}}, state[id], {{ ok: box.checked }}); el.classList.toggle("done", box.checked); save(); progress(); }};
    cm.oninput = function () {{ state[id] = Object.assign({{}}, state[id], {{ cm: cm.value }}); save(); }};
  }});
  progress();
  document.getElementById("copy").onclick = function () {{
    var lines = [];
    items.forEach(function (el) {{ var s = state[el.dataset.id] || {{}}; if (s.cm && s.cm.trim()) lines.push(el.dataset.id + ": " + s.cm.trim()); }});
    var text = {js_str(test.get("id"))} + " — замечания (" + lines.length + "):\\n" + (lines.join("\\n") || "нет");
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
      function () {{ alert("Скопировано: " + lines.length + " замечаний"); }},
      function () {{ prompt("Скопируйте вручную:", text); }});
  }};
  document.getElementById("reset").onclick = function () {{
    if (confirm("Сбросить все отметки и замечания для этого теста?")) {{ state = {{}}; save(); location.reload(); }}
  }};
}})();
</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, type=Path, help="authoring Test JSON")
    ap.add_argument("--notes", type=Path, help="NOTES.md (default: next to the JSON, if present)")
    ap.add_argument("--out", type=Path, help="output HTML (default: REVIEW.html next to the JSON)")
    args = ap.parse_args(argv)
    test = json.loads(args.file.read_text(encoding="utf-8"))
    notes_path = args.notes or args.file.with_name("NOTES.md")
    notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else None
    out = args.out or args.file.with_name("REVIEW.html")
    out.write_text(render(test, notes), encoding="utf-8")
    missing = [
        a
        for a in re.findall(r"(?:src)='([^']+)'", out.read_text(encoding="utf-8"))
        if not (out.parent / html.unescape(a)).exists()
    ]
    if missing:
        log.warning(
            "review page %s references %d missing file(s): %s", out, len(missing), missing[:5]
        )
    log.info("review page written: %s (test=%s)", out, test.get("id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
