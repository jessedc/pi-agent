"""Render a `pi` session transcript (.jsonl) as a self-contained HTML page.

    uv run pi-agent-transcript sessions/issue-1
    uv run pi-agent-transcript sessions/issue-1/<file>.jsonl --open

Everything stays on disk: the output embeds its own CSS and JS, loads no remote
asset, and the script itself has no dependencies and makes no network calls.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

# --------------------------------------------------------------------------
# transcript model
# --------------------------------------------------------------------------


def load_records(path: Path) -> list[dict[str, Any]]:
    """Parse a .jsonl transcript, skipping lines that aren't valid JSON objects."""
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"{path}:{lineno}: skipping unparseable line ({exc})", file=sys.stderr)
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def index_tool_results(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map toolCallId -> the toolResult message that answered it."""
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        message = record.get("message")
        if isinstance(message, dict) and message.get("role") == "toolResult":
            call_id = message.get("toolCallId")
            if isinstance(call_id, str):
                results[call_id] = message
    return results


def content_text(content: Any) -> str:
    """Flatten a message `content` field (string, or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)
    return "" if content is None else str(content)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# a small markdown subset — enough for assistant prose, and it never leaves here
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^```([\w+-]*)\n(.*?)(?:^```\s*$|\Z)", re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_URL_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*", re.IGNORECASE)
_SAFE_SCHEMES = frozenset({"http", "https"})
_BOLD = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(\S(?:[^*\n]*\S)?)\*(?!\*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_SENTINEL = "\x00c%d\x00"


def _safe_href(url: str) -> str | None:
    """Return a supported link destination, or reject it as inert text."""
    candidate = url
    # HTML references can be nested by transcript text and the renderer's own
    # escaping. Validate the browser-visible form, while preserving the source
    # spelling when it is safe.
    for _pass in range(3):
        decoded = html.unescape(candidate)
        if decoded == candidate:
            break
        candidate = decoded

    # Backslashes and network-path references let browser URL parsers reinterpret
    # a seemingly relative destination as a host navigation.
    if not candidate or "\\" in candidate or candidate.startswith("//"):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None

    colon = candidate.find(":")
    boundary = min(
        (at for mark in "/?#" if (at := candidate.find(mark)) != -1), default=len(candidate)
    )
    if colon == -1 or colon > boundary:
        return url

    scheme = candidate[:colon]
    if _URL_SCHEME.fullmatch(scheme) is None or scheme.lower() not in _SAFE_SCHEMES:
        return None
    return url


def _inline(text: str) -> str:
    """Escape, then apply inline markdown. Code spans are shielded from the rest."""
    escaped = html.escape(text)
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(match.group(1))
        return _SENTINEL % (len(spans) - 1)

    escaped = _INLINE_CODE.sub(stash, escaped)

    def link(match: re.Match[str]) -> str:
        destination = html.unescape(match.group(2))
        href = _safe_href(destination)
        if href is None:
            return f"{match.group(1)} ({html.escape(destination)})"
        return f'<a href="{html.escape(href, quote=True)}" rel="noreferrer">{match.group(1)}</a>'

    escaped = _LINK.sub(link, escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
    for i, span in enumerate(spans):
        escaped = escaped.replace(_SENTINEL % i, f"<code>{span}</code>")
    return escaped


def _blocks(text: str) -> str:
    """Render the non-fenced part of a document: headings, lists, quotes, paragraphs."""
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(line) for line in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
            continue

        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)

        if heading:
            flush_para()
            flush_list()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif _HR.match(line):
            flush_para()
            flush_list()
            out.append("<hr>")
        elif bullet or ordered:
            flush_para()
            wanted = "ul" if bullet else "ol"
            if list_tag != wanted:
                flush_list()
                out.append(f"<{wanted}>")
                list_tag = wanted
            item = bullet.group(1) if bullet else ordered.group(1)  # type: ignore[union-attr]
            out.append(f"<li>{_inline(item)}</li>")
        elif line.lstrip().startswith("&gt;") or line.lstrip().startswith(">"):
            flush_para()
            flush_list()
            out.append(f"<blockquote>{_inline(line.lstrip().lstrip('>').strip())}</blockquote>")
        else:
            flush_list()
            para.append(line)

    flush_para()
    flush_list()
    return "\n".join(out)


def markdown(text: str) -> str:
    """Render a markdown subset. Fenced code is extracted first and left verbatim."""
    out: list[str] = []
    cursor = 0
    for match in _FENCE.finditer(text):
        out.append(_blocks(text[cursor : match.start()]))
        lang = match.group(1) or "text"
        out.append(
            f'<pre class="code" data-lang="{html.escape(lang)}">'
            f"<code>{html.escape(match.group(2))}</code></pre>"
        )
        cursor = match.end()
    out.append(_blocks(text[cursor:]))
    return "\n".join(part for part in out if part)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def render_diff(diff: str) -> str:
    rows: list[str] = []
    for line in diff.split("\n"):
        if line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        rows.append(f'<span class="{cls}">{html.escape(line)}</span>')
    return '<pre class="diff">' + "\n".join(rows) + "</pre>"


def summarise_call(name: str, args: dict[str, Any]) -> str:
    if name == "bash":
        command = str(args.get("command", ""))
        first = command.strip().split("\n", 1)[0]
        return first[:120] + ("…" if len(first) > 120 or "\n" in command.strip() else "")
    for key in ("path", "file_path", "filePath", "pattern", "url", "query"):
        if key in args:
            return str(args[key])[:120]
    compact = json.dumps(args, ensure_ascii=False)
    return compact[:120] + ("…" if len(compact) > 120 else "")


def render_call_body(name: str, args: dict[str, Any], details: Any, limit: int) -> str:
    if name == "bash":
        return f'<pre class="code"><code>{html.escape(str(args.get("command", "")))}</code></pre>'

    if isinstance(details, dict) and isinstance(details.get("diff"), str):
        return render_diff(details["diff"])

    if name == "write" and "content" in args:
        body, clipped = truncate(str(args["content"]), limit)
        note = '<div class="clip">output truncated</div>' if clipped else ""
        return f'<pre class="code"><code>{html.escape(body)}</code></pre>{note}'

    if name == "read" and set(args) <= {"path", "file_path"}:
        return ""

    pretty = json.dumps(args, indent=2, ensure_ascii=False)
    body, clipped = truncate(pretty, limit)
    note = '<div class="clip">arguments truncated</div>' if clipped else ""
    return f'<pre class="code"><code>{html.escape(body)}</code></pre>{note}'


def render_result(result: dict[str, Any] | None, limit: int) -> str:
    if result is None:
        return '<div class="pending">no result recorded</div>'

    text = content_text(result.get("content")).rstrip()
    is_error = bool(result.get("isError"))
    body, clipped = truncate(text, limit)
    lines = text.count("\n") + 1 if text else 0
    label = "error" if is_error else "output"
    meta = f"{lines} line{'s' if lines != 1 else ''} · {human_bytes(len(text.encode()))}"

    if not text:
        return f'<div class="pending">{"error, no output" if is_error else "no output"}</div>'

    note = '<div class="clip">output truncated</div>' if clipped else ""
    open_attr = " open" if is_error else ""
    cls = "result error" if is_error else "result"
    return (
        f'<details class="{cls}"{open_attr}>'
        f'<summary>{label} <span class="meta">{meta}</span></summary>'
        f'<pre class="out"><code>{html.escape(body)}</code></pre>{note}'
        f"</details>"
    )


_SHARD = re.compile(r"-\d{5}-of-\d{5}$")


def pretty_model(message: dict[str, Any]) -> tuple[str, str]:
    """Return (label, full): prefer the requested id over the server's echoed path."""
    requested = str(message.get("model") or "")
    echoed = str(message.get("responseModel") or "")
    label = requested or echoed
    if "/" in label or "\\" in label:
        label = _SHARD.sub("", Path(label.replace("\\", "/")).stem)
    return label, echoed or requested


def render_usage(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    bits: list[str] = []
    for key, label in (("input", "in"), ("output", "out"), ("cacheRead", "cached")):
        value = usage.get(key)
        if isinstance(value, int | float) and value:
            bits.append(f"{label} {int(value):,}")
    cost = usage.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("total"), int | float) and cost["total"]:
        bits.append(f"${cost['total']:.4f}")
    return f'<span class="usage">{" · ".join(bits)}</span>' if bits else ""


def render_message(record: dict[str, Any], results: dict[str, dict[str, Any]], limit: int) -> str:
    message = record["message"]
    role = message.get("role")
    stamp = parse_ts(message.get("timestamp") or record.get("timestamp"))
    clock = stamp.strftime("%H:%M:%S") if stamp else ""

    if role == "user":
        return (
            f'<section class="turn user"><header><span class="who">user</span>'
            f'<span class="clock">{clock}</span></header>'
            f'<div class="body">{markdown(content_text(message.get("content")))}</div></section>'
        )

    if role != "assistant":
        return ""  # toolResults are folded into their call

    parts: list[str] = []
    content = message.get("content")
    blocks = content if isinstance(content, list) else []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")

        if kind == "text" and block.get("text", "").strip():
            parts.append(f'<div class="body">{markdown(block["text"])}</div>')

        elif kind == "thinking" and block.get("thinking", "").strip():
            thought = markdown(block["thinking"])
            parts.append(
                '<details class="thinking"><summary>thinking</summary>'
                f'<div class="body">{thought}</div></details>'
            )

        elif kind == "toolCall":
            name = str(block.get("name", "tool"))
            raw_args = block.get("arguments")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            result = results.get(str(block.get("id")))
            details = result.get("details") if result else None
            failed = bool(result and result.get("isError"))
            badge = '<span class="badge err">failed</span>' if failed else ""
            parts.append(
                f'<div class="tool{" failed" if failed else ""}">'
                f'<div class="tool-head"><span class="tool-name">{html.escape(name)}</span>'
                f'<span class="tool-sum">{html.escape(summarise_call(name, args))}</span>'
                f"{badge}</div>"
                f"{render_call_body(name, args, details, limit)}"
                f"{render_result(result, limit)}"
                f"</div>"
            )

    if not parts:
        return ""

    label, full = pretty_model(message)
    title = f' title="{html.escape(full)}"' if full and full != label else ""
    head = (
        f'<header><span class="who">assistant</span>'
        f'<span class="model"{title}>{html.escape(label)}</span>'
        f"{render_usage(message.get('usage', {}))}"
        f'<span class="clock">{clock}</span></header>'
    )
    return f'<section class="turn assistant">{head}{"".join(parts)}</section>'


def collect_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "session": "",
        "cwd": "",
        "model": "",
        "provider": "",
        "thinking": "",
        "input": 0,
        "output": 0,
        "cached": 0,
        "cost": 0.0,
        "turns": 0,
        "tools": 0,
        "errors": 0,
        "start": None,
        "end": None,
    }

    for record in records:
        stamp = parse_ts(record.get("timestamp"))
        if stamp:
            if stats["start"] is None or stamp < stats["start"]:
                stats["start"] = stamp
            if stats["end"] is None or stamp > stats["end"]:
                stats["end"] = stamp

        kind = record.get("type")
        if kind == "session":
            stats["session"] = str(record.get("id", ""))
            stats["cwd"] = str(record.get("cwd", ""))
        elif kind == "model_change":
            stats["model"] = str(record.get("modelId", ""))
            stats["provider"] = str(record.get("provider", ""))
        elif kind == "thinking_level_change":
            stats["thinking"] = str(record.get("thinkingLevel", ""))
        elif kind != "message":
            continue

        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            stats["turns"] += 1
            usage = message.get("usage")
            if isinstance(usage, dict):
                stats["input"] += int(usage.get("input") or 0)
                stats["output"] += int(usage.get("output") or 0)
                stats["cached"] += int(usage.get("cacheRead") or 0)
                cost = usage.get("cost")
                if isinstance(cost, dict):
                    stats["cost"] += float(cost.get("total") or 0)
            if not stats["model"]:
                stats["model"] = pretty_model(message)[0]
        elif message.get("role") == "toolResult":
            stats["tools"] += 1
            if message.get("isError"):
                stats["errors"] += 1

    return stats


def format_duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "—"
    seconds = int((end - start).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


CSS = """
*{box-sizing:border-box}
:root{
  --bg:#fbfaf8; --panel:#fff; --ink:#1c1b19; --dim:#6d6a65; --line:#e5e1da;
  --accent:#8a5a2b; --user:#2f5d8a; --code:#f4f1ec; --add:#1f6f3d; --del:#a3312a;
  --err:#a3312a; --errbg:#fdf2f1;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#16151a; --panel:#1e1d23; --ink:#e8e6e1; --dim:#9a958d; --line:#312f38;
  --accent:#d9a066; --user:#7fb0e0; --code:#26252c; --add:#6fce93; --del:#ef8078;
  --err:#ef8078; --errbg:#2a1e1e;
}}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
code,pre,.tool-sum,.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace}
.wrap{max-width:60rem;margin:0 auto;padding:2rem 1.25rem 6rem}

.head{border-bottom:1px solid var(--line);padding-bottom:1.25rem;margin-bottom:2rem}
.head h1{margin:0 0 .35rem;font-size:1.5rem;letter-spacing:-.01em}
.head .sub{color:var(--dim);font-size:.8rem;word-break:break-all}
.stats{display:flex;flex-wrap:wrap;gap:.4rem 1.75rem;margin-top:1.1rem}
.stat{display:flex;flex-direction:column}
.stat b{font-size:1rem;font-weight:600;font-variant-numeric:tabular-nums}
.stat span{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.controls{margin-top:1.1rem;display:flex;gap:.5rem}
button{font:inherit;font-size:.78rem;padding:.3rem .7rem;border:1px solid var(--line);
  border-radius:6px;background:var(--panel);color:var(--dim);cursor:pointer}
button:hover{color:var(--ink);border-color:var(--accent)}

.turn{margin:0 0 1.75rem;padding-left:1rem;border-left:2px solid var(--line)}
.turn.user{border-left-color:var(--user)}
.turn.assistant{border-left-color:var(--accent)}
.turn header{display:flex;align-items:baseline;gap:.75rem;flex-wrap:wrap;margin-bottom:.6rem}
.who{font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;font-weight:700}
.turn.user .who{color:var(--user)} .turn.assistant .who{color:var(--accent)}
.model,.usage,.clock{font-size:.7rem;color:var(--dim);font-variant-numeric:tabular-nums}
.clock{margin-left:auto}

.body>*:first-child{margin-top:0} .body>*:last-child{margin-bottom:0}
.body p{margin:.6rem 0} .body h2,.body h3,.body h4{margin:1.2rem 0 .5rem;font-size:1rem}
.body ul,.body ol{margin:.6rem 0;padding-left:1.4rem} .body li{margin:.2rem 0}
.body blockquote{margin:.6rem 0;padding-left:.8rem;border-left:2px solid var(--line);color:var(--dim)}
.body hr{border:0;border-top:1px solid var(--line);margin:1.2rem 0}
.body a{color:var(--user)}
:is(.body,.tool) code{background:var(--code);padding:.1em .35em;border-radius:4px;font-size:.86em}
pre{margin:.6rem 0;padding:.7rem .85rem;background:var(--code);border-radius:8px;
  overflow-x:auto;font-size:.8rem;line-height:1.55}
pre code{background:none;padding:0;font-size:inherit}

details.thinking{margin:.5rem 0}
details.thinking>summary{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);cursor:pointer;list-style:none}
details.thinking>summary::before{content:"▸ ";}
details.thinking[open]>summary::before{content:"▾ ";}
details.thinking .body{color:var(--dim);font-style:italic;padding-left:.9rem;
  border-left:1px dashed var(--line);margin-top:.4rem}

.tool{margin:.85rem 0;border:1px solid var(--line);border-radius:10px;
  background:var(--panel);padding:.7rem .85rem}
.tool.failed{border-color:var(--err);background:var(--errbg)}
.tool-head{display:flex;align-items:baseline;gap:.6rem}
.tool-name{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;font-weight:700;
  color:var(--accent);flex:none}
.tool-sum{font-size:.8rem;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge.err{margin-left:auto;font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--err);border:1px solid var(--err);border-radius:4px;padding:0 .35rem;flex:none}
.tool pre{margin:.5rem 0 0}
details.result{margin-top:.5rem}
details.result>summary{font-size:.72rem;color:var(--dim);cursor:pointer;list-style:none}
details.result>summary::before{content:"▸ ";}
details.result[open]>summary::before{content:"▾ ";}
details.result .meta{opacity:.7}
details.result.error>summary{color:var(--err)}
pre.out{max-height:26rem;overflow:auto;white-space:pre-wrap;word-break:break-word}
pre.diff span{display:block;white-space:pre-wrap}
pre.diff .add{color:var(--add)} pre.diff .del{color:var(--del)} pre.diff .ctx{color:var(--dim)}
.clip,.pending{font-size:.72rem;color:var(--dim);font-style:italic;margin-top:.35rem}
"""

JS = """
document.querySelectorAll('[data-toggle]').forEach(function (button) {
  button.addEventListener('click', function () {
    var open = button.dataset.toggle === 'open';
    document.querySelectorAll('details').forEach(function (d) { d.open = open; });
  });
});
"""

PAGE = Template(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="$csp">
<title>$title</title>
<style>$css</style>
</head><body><div class="wrap">
<header class="head">
  <h1>$heading</h1>
  <div class="sub mono">$sub</div>
  <div class="stats">$stats</div>
  <div class="controls">
    <button data-toggle="open">expand all</button>
    <button data-toggle="closed">collapse all</button>
  </div>
</header>
<main>$body</main>
</div><script>$js</script></body></html>
"""
)


def render_page(records: list[dict[str, Any]], title: str, limit: int) -> str:
    stats = collect_stats(records)
    results = index_tool_results(records)
    body = "\n".join(
        rendered
        for record in records
        if record.get("type") == "message" and isinstance(record.get("message"), dict)
        for rendered in [render_message(record, results, limit)]
        if rendered
    )

    start: datetime | None = stats["start"]
    tiles = [
        ("turns", f"{stats['turns']:,}"),
        ("tool calls", f"{stats['tools']:,}"),
        ("duration", format_duration(start, stats["end"])),
        ("tokens in", f"{stats['input']:,}"),
        ("tokens out", f"{stats['output']:,}"),
    ]
    if stats["cached"]:
        tiles.append(("cached", f"{stats['cached']:,}"))
    if stats["cost"]:
        tiles.append(("cost", f"${stats['cost']:.4f}"))
    if stats["errors"]:
        tiles.append(("tool errors", f"{stats['errors']:,}"))

    stat_html = "".join(
        f'<div class="stat"><b>{html.escape(value)}</b><span>{label}</span></div>'
        for label, value in tiles
    )

    sub_bits = [
        stats["model"] and f"{stats['provider']}/{stats['model']}".strip("/"),
        stats["thinking"] and f"thinking: {stats['thinking']}",
        stats["cwd"],
        start.astimezone().strftime("%Y-%m-%d %H:%M") if start else "",
        stats["session"],
    ]
    sub = " · ".join(html.escape(str(bit)) for bit in sub_bits if bit)

    script_hash = base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
    csp = (
        "default-src 'none'; "
        f"script-src 'sha256-{script_hash}'; "
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    )

    return PAGE.substitute(
        title=html.escape(title),
        heading=html.escape(title),
        sub=sub,
        stats=stat_html,
        body=body or '<p class="pending">no messages in this transcript</p>',
        css=CSS,
        js=JS,
        csp=html.escape(csp, quote=True),
    )


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def resolve_inputs(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.rglob("*.jsonl"))
    return [target]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", type=Path, help="a .jsonl transcript, or a directory of them")
    parser.add_argument("-o", "--output", type=Path, help="output file (single input only)")
    parser.add_argument(
        "--max-output",
        type=int,
        default=20000,
        metavar="CHARS",
        help="truncate each tool output at N characters (0 = no limit, default: 20000)",
    )
    parser.add_argument("--open", action="store_true", help="open the result in a browser")
    args = parser.parse_args(argv)

    if not args.target.exists():
        parser.error(f"no such path: {args.target}")

    inputs = resolve_inputs(args.target)
    if not inputs:
        parser.error(f"no .jsonl transcripts under {args.target}")
    if args.output and len(inputs) > 1:
        parser.error("--output takes a single input transcript")

    written: list[Path] = []
    for source in inputs:
        records = load_records(source)
        title = f"{source.parent.name}/{source.stem}" if source.parent.name else source.stem
        destination = args.output or source.with_suffix(".html")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_page(records, title, args.max_output), encoding="utf-8")
        written.append(destination)
        print(f"{source} → {destination} ({human_bytes(destination.stat().st_size)})")

    if args.open and written:
        webbrowser.open(written[0].resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
