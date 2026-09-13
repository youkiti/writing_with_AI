"""make_tracked_md.py — 2 つの Markdown の差分を pandoc の tracked-change span に変換し、
元原稿のコメント (report.json) と返答 (response_table.md) を同じ位置に埋め込んだ
Markdown を出力する。標準ライブラリのみ。

    python make_tracked_md.py --old v1.md --new v2.md \
        --report report.json --responses response_table.md \
        --author "Taro Yamada" --out v2_tracked.md

出力した v2_tracked.md を、いつもの pandoc コマンドで docx にすると、
Word では「変更履歴 + コメント」、Google Docs に取り込むと「提案 + コメント」になる。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- tokenizer
CJK = r"　-ヿ㐀-䶿一-鿿豈-﫿＀-￯"
TOKEN_RE = re.compile(rf"\s+|[^\s{CJK}]+|[{CJK}]")


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


# ---------------------------------------------------------------- blocks
FENCE_RE = re.compile(r"^(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6}\s+)(.*?)(\s*#*\s*)$")
LIST_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
QUOTE_RE = re.compile(r"^(>\s?)(.*)$")
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
SEP_ROW_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_blocks(text: str) -> list[dict]:
    """空行区切りでブロック化。YAML front matter / fenced code / 表は 1 ブロック。"""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    i, n = 0, len(lines)

    # YAML front matter
    if lines and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() in ("---", "..."):
                blocks.append({"kind": "yaml", "text": "\n".join(lines[: j + 1])})
                i = j + 1
                break

    buf: list[str] = []

    def flush():
        nonlocal buf
        if buf:
            blocks.append(classify("\n".join(buf)))
            buf = []

    while i < n:
        line = lines[i]
        if FENCE_RE.match(line):
            flush()
            fence = [line]
            i += 1
            while i < n:
                fence.append(lines[i])
                if FENCE_RE.match(lines[i]):
                    i += 1
                    break
                i += 1
            blocks.append({"kind": "code", "text": "\n".join(fence)})
            continue
        if line.strip() == "":
            flush()
            i += 1
            continue
        buf.append(line)
        i += 1
    flush()
    return blocks


def classify(text: str) -> dict:
    lines = text.split("\n")
    if all(TABLE_LINE_RE.match(l) or SEP_ROW_RE.match(l) for l in lines) and len(lines) >= 2:
        return {"kind": "table", "text": text}
    if len(lines) == 1 and HEADING_RE.match(lines[0]):
        return {"kind": "heading", "text": text}
    if all(LIST_RE.match(l) or l.startswith("  ") for l in lines):
        return {"kind": "list", "text": text}
    if all(QUOTE_RE.match(l) for l in lines):
        return {"kind": "quote", "text": text}
    if lines[0].lstrip().startswith("<") or lines[0].startswith("$$"):
        return {"kind": "raw", "text": text}
    if len(lines) == 1 and re.match(r"^!\[.*\]\(.*\)\s*(\{.*\})?$", lines[0]):
        return {"kind": "figure", "text": text}
    # 普通の段落: 改行は空白に畳む (pandoc も同じ扱い)
    return {"kind": "para", "text": " ".join(l.strip() for l in lines)}


# ---------------------------------------------------------------- span helpers
def esc(text: str) -> str:
    """span の中身 (コメント本文など) 用に Markdown 記号をエスケープ。"""
    text = re.sub(r"\s*\n\s*", " ", text.strip())
    return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", text)


class Marks:
    def __init__(self, author: str, date: str):
        self.author = author
        self.date = date

    def ins(self, s: str) -> str:
        return s if s.strip() == "" else f'[{s}]{{.insertion author="{self.author}" date="{self.date}"}}'

    def dele(self, s: str) -> str:
        return s if s.strip() == "" else f'[{s}]{{.deletion author="{self.author}" date="{self.date}"}}'


def comment_start(cid: str, author: str, date: str, body: str) -> str:
    return f'[{esc(body)}]{{.comment-start id="{cid}" author="{author}" date="{date}"}}'


def comment_end(cid: str) -> str:
    return f'[]{{.comment-end id="{cid}"}}'


PAIRS = {"[": "]", "(": ")", "{": "}"}
UNTRACKABLE: list[str] = []  # 差分表示できずに新版だけを出したブロック (先頭行)


def balanced(s: str) -> bool:
    """span で包んでも壊れない (括弧・強調・コードが閉じている) か。"""
    depth = {"[": 0, "(": 0, "{": 0}
    for ch in s:
        if ch in depth:
            depth[ch] += 1
        elif ch in PAIRS.values():
            k = [a for a, b in PAIRS.items() if b == ch][0]
            depth[k] -= 1
            if depth[k] < 0:
                return False
    if any(v != 0 for v in depth.values()):
        return False
    if s.count("`") % 2 or s.count("$") % 2:
        return False
    if s.count("*") % 2:
        return False
    # 単語境界の _ だけ数える (snake_case は無視)
    if len(re.findall(r"(?<!\w)_|_(?!\w)", s)) % 2:
        return False
    return True


# ---------------------------------------------------------------- inline diff
def inline_diff(old: str, new: str, m: Marks, cstarts: dict, cends: dict) -> str | None:
    """単語単位の差分を span 化。cstarts/cends: old トークン index → [comment markup]。
    バランスが取れない差分があれば None (呼び出し側でブロック丸ごと置換にフォールバック)。"""
    a, b = tokenize(old), tokenize(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[str] = []
    pending_end: list[str] = []

    def emit_starts(i_from: int, i_to: int):
        for i in range(i_from, i_to):
            out.extend(cstarts.get(i, []))

    def collect_ends(i_from: int, i_to: int):
        for i in range(i_from, i_to):
            pending_end.extend(cends.get(i, []))

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_s = "".join(a[i1:i2])
        new_s = "".join(b[j1:j2])
        if tag == "equal":
            # コメント開始/終了は等価領域ではトークン単位で正確に置ける
            for i in range(i1, i2):
                out.extend(cstarts.get(i, []))
                out.append(a[i])
                out.extend(cends.get(i, []))
            continue
        if tag == "replace" and old_s.strip() == new_s.strip():
            emit_starts(i1, i2)
            out.append(new_s)
            collect_ends(i1, i2)
            out.extend(pending_end)
            pending_end.clear()
            continue
        if (tag in ("delete", "replace") and not balanced(old_s)) or (
            tag in ("insert", "replace") and not balanced(new_s)
        ):
            return None
        emit_starts(i1, i2)
        if tag in ("delete", "replace"):
            out.append(m.dele(old_s))
        if tag in ("insert", "replace"):
            out.append(m.ins(new_s))
        collect_ends(i1, i2)
        out.extend(pending_end)
        pending_end.clear()
    return "".join(out)


# ---------------------------------------------------------------- block wrappers
def wrap_block(block: dict, fn) -> str:
    """ブロック全体を insertion / deletion で包む (行頭記号は外に出す)。"""
    kind, text = block["kind"], block["text"]
    if kind in ("code", "raw", "yaml"):
        UNTRACKABLE.append(text.split("\n")[0][:60])
        return text  # 包めない: そのまま (削除ブロックなら残ってしまうので summary で知らせる)
    if kind == "table":
        rows = []
        for line in text.split("\n"):
            if SEP_ROW_RE.match(line):
                rows.append(line)
            else:
                cells = line.strip().strip("|").split("|")
                rows.append("| " + " | ".join(fn(c.strip()) if c.strip() else "" for c in cells) + " |")
        return "\n".join(rows)
    out = []
    for line in text.split("\n"):
        for rx in (HEADING_RE, LIST_RE, QUOTE_RE):
            mm = rx.match(line)
            if mm:
                out.append(mm.group(1) + fn(mm.group(2)) + (mm.group(3) if rx is HEADING_RE else ""))
                break
        else:
            out.append(fn(line))
    return "\n".join(out)


def diff_block_pair(old: dict, new: dict, m: Marks, entries: list[dict]) -> str:
    kind = new["kind"]
    def fallback() -> str:
        """丸ごと置換 (コメント付きなら削除ブロックをコメントで挟む)。"""
        starts = "".join(s for e in entries for s in e["starts"])
        ends = "".join(s for e in reversed(entries) for s in e["ends"])
        return starts + wrap_block(old, m.dele) + ends + "\n\n" + wrap_block(new, m.ins)
    if kind in ("code", "raw", "yaml"):
        # 差分表示できないブロック: 新版だけを出し、summary で知らせる
        UNTRACKABLE.append(new["text"].split("\n")[0][:60])
        return new["text"]
    if old["kind"] != kind:
        return fallback()  # 種類が変わった → 丸ごと置換
    if kind == "table":
        return diff_table(old["text"], new["text"], m) or fallback()
    if kind == "para":
        cstarts, cends = char_to_tokens(entries, old["text"])
        res = inline_diff(old["text"], new["text"], m, cstarts, cends)
        return res if res is not None else fallback()
    # heading / list / quote / figure: 行を対応付けてから、行ごとに prefix を外して比較
    ol, nl = old["text"].split("\n"), new["text"].split("\n")
    offsets = []
    acc = 0
    for lo in ol:
        offsets.append(acc)
        acc += len(lo) + 1
    out = []
    sm = difflib.SequenceMatcher(None, ol, nl, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k, lo in enumerate(ol[i1:i2]):
                po, co = split_prefix(lo)
                cstarts, cends = char_to_tokens(entries, co, offsets[i1 + k] + len(po))
                toks = tokenize(co)
                parts = []
                for ti, t in enumerate(toks):
                    parts.extend(cstarts.get(ti, []))
                    parts.append(t)
                    parts.extend(cends.get(ti, []))
                out.append(po + "".join(parts))
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for k, (lo, ln) in enumerate(zip(ol[i1:i2], nl[j1:j2])):
                po, co = split_prefix(lo)
                pn, cn = split_prefix(ln)
                if po != pn:
                    return fallback()
                cstarts, cends = char_to_tokens(entries, co, offsets[i1 + k] + len(po))
                res = inline_diff(co, cn, m, cstarts, cends)
                if res is None:
                    return fallback()
                out.append(pn + res)
        else:
            for k, lo in enumerate(ol[i1:i2]):
                po, co = split_prefix(lo)
                cstarts, cends = char_to_tokens(entries, co, offsets[i1 + k] + len(po))
                starts = "".join(x for key in sorted(cstarts) for x in cstarts[key])
                ends = "".join(x for key in sorted(cends) for x in cends[key])
                out.append(po + starts + m.dele(co) + ends)
            for ln in nl[j1:j2]:
                pn, cn = split_prefix(ln)
                out.append(pn + m.ins(cn))
    return "\n".join(out)


def place_marks_lines(text: str, entries: list[dict]) -> str:
    """変更のないブロックに、文字範囲どおりコメントマークを置く (行頭記号は外す)。"""
    out = []
    offset = 0
    for line in text.split("\n"):
        prefix, content = split_prefix(line)
        cstarts, cends = char_to_tokens(entries, content, offset + len(prefix))
        toks = tokenize(content)
        parts = []
        for ti, t in enumerate(toks):
            parts.extend(cstarts.get(ti, []))
            parts.append(t)
            parts.extend(cends.get(ti, []))
        out.append(prefix + "".join(parts))
        offset += len(line) + 1
    return "\n".join(out)


def split_prefix(line: str) -> tuple[str, str]:
    for rx in (HEADING_RE, LIST_RE, QUOTE_RE):
        mm = rx.match(line)
        if mm:
            return mm.group(1), mm.group(2)
    return "", line


def diff_table(old: str, new: str, m: Marks) -> str | None:
    ol, nl = old.split("\n"), new.split("\n")
    sm = difflib.SequenceMatcher(None, ol, nl, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ol[i1:i2])
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for lo, ln in zip(ol[i1:i2], nl[j1:j2]):
                if SEP_ROW_RE.match(lo) or SEP_ROW_RE.match(ln):
                    out.append(ln)
                    continue
                co = [c.strip() for c in lo.strip().strip("|").split("|")]
                cn = [c.strip() for c in ln.strip().strip("|").split("|")]
                if len(co) != len(cn):
                    return None
                cells = []
                for x, y in zip(co, cn):
                    if x == y:
                        cells.append(y)
                    else:
                        r = inline_diff(x, y, m, {}, {})
                        if r is None:
                            return None
                        cells.append(r)
                out.append("| " + " | ".join(cells) + " |")
        else:
            for lo in ol[i1:i2]:
                out.append(lo if SEP_ROW_RE.match(lo) else wrap_row(lo, m.dele))
            for ln in nl[j1:j2]:
                out.append(ln if SEP_ROW_RE.match(ln) else wrap_row(ln, m.ins))
    return "\n".join(out)


def wrap_row(line: str, fn) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return "| " + " | ".join(fn(c) if c else "" for c in cells) + " |"


# ---------------------------------------------------------------- comments
def load_responses(path: Path | None) -> dict[str, dict]:
    """response_table.md の Markdown 表を読む。1 列目 = コメント ID。
    列名は見出し行から拾う (区分 / 対応 / 返答 を想定)。"""
    if not path or not path.exists():
        return {}
    rows: dict[str, dict] = {}
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            continue
        rec = dict(zip(header, cells))
        cid = cells[0].strip("# ")
        if cid:
            rows[cid] = rec
    return rows


def plain_text(md: str) -> str:
    """アンカー検索用にざっくり Markdown 記号を落とす。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[*_`]", "", s)
    return s


def find_anchor(anchor: str, blocks: list[dict]) -> tuple[int, int, int] | None:
    """anchor 文字列を含む old ブロックと文字範囲 (block_idx, char_from, char_to) を返す。"""
    anchor = re.sub(r"\s+", " ", anchor.strip())
    if not anchor:
        return None
    for bi, b in enumerate(blocks):
        if b["kind"] in ("code", "raw", "yaml"):
            continue
        text = b["text"]
        pos = text.find(anchor)
        if pos >= 0:
            return (bi, pos, pos + len(anchor))
        if plain_text(text).find(anchor) >= 0:
            # 記号を落とした座標は使えないので、ブロック全体をアンカーにする
            return (bi, 0, len(text))
    return None


def char_to_tokens(entries: list[dict], text: str, offset: int = 0) -> tuple[dict, dict]:
    """文字範囲 (ブロック内座標) を、text (ブロック内 offset から始まる部分文字列) の
    トークン index に変換し、inline_diff 用の cstarts / cends を返す。"""
    toks = tokenize(text)
    cstarts: dict[int, list[str]] = {}
    cends: dict[int, list[str]] = {}
    if not toks:
        return cstarts, cends
    bounds = []
    acc = 0
    for t in toks:
        bounds.append((acc, acc + len(t)))
        acc += len(t)
    lo, hi = offset, offset + len(text)
    for e in entries:
        c0, c1 = max(e["c0"], lo) - offset, min(e["c1"], hi) - offset
        if c1 <= c0:
            continue
        t0 = next((i for i, (a, b) in enumerate(bounds) if b > c0), 0)
        t1 = next((i for i, (a, b) in enumerate(bounds) if b >= c1), len(toks) - 1)
        if e["c0"] >= lo:  # 開始がこの text 内にある場合だけ開始マークを置く
            cstarts.setdefault(t0, []).extend(e["starts"])
        if e["c1"] <= hi:
            cends.setdefault(t1, [])[0:0] = e["ends"]
    return cstarts, cends


def best_block(context: str, blocks: list[dict]) -> int | None:
    best, best_r = None, 0.0
    for bi, b in enumerate(blocks):
        if b["kind"] in ("code", "raw", "yaml"):
            continue
        r = difflib.SequenceMatcher(None, context, b["text"], autojunk=False).ratio()
        if r > best_r:
            best, best_r = bi, r
    return best if best_r >= 0.5 else None


# ---------------------------------------------------------------- main
def main() -> int:
    try:  # Windows の cp932 コンソールで日本語の summary が落ちないように
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, type=Path, help="レビューに出した版の Markdown")
    ap.add_argument("--new", required=True, type=Path, help="修正後の Markdown")
    ap.add_argument("--report", type=Path, help="parse_docx_changes.py の report.json")
    ap.add_argument("--responses", type=Path, help="response_table.md (ID / 区分 / 対応 / 返答)")
    ap.add_argument("--author", default="Author", help="変更履歴・返答コメントの著者名")
    ap.add_argument("--skip-kind", default="typo", help="この区分のコメントは文書に載せない (カンマ区切り)")
    ap.add_argument("--date", help="変更履歴の日時 (ISO 8601 UTC)。省略時は現在時刻。テストの再現用")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    date = args.date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = Marks(args.author, date)
    old_blocks = split_blocks(args.old.read_text(encoding="utf-8"))
    new_blocks = split_blocks(args.new.read_text(encoding="utf-8"))

    # ---- コメントを old ブロックに紐付ける
    comments = []
    if args.report and args.report.exists():
        comments = json.loads(args.report.read_text(encoding="utf-8")).get("comments", [])
    responses = load_responses(args.responses)
    skip = {k.strip().lower() for k in args.skip_kind.split(",") if k.strip()}

    per_block: dict[int, list[dict]] = {}
    summary = {"carried": [], "skipped": [], "unanchored": [], "fuzzy": []}
    next_id = 1000
    for c in comments:
        cid = str(c.get("id", ""))
        resp = responses.get(cid, {})
        kind = (resp.get("区分") or resp.get("kind") or "").strip().lower()
        if kind in skip:
            summary["skipped"].append(cid)
            continue
        loc = find_anchor(c.get("anchor_text", ""), old_blocks)
        how = "exact"
        if loc is None and c.get("paragraph_context"):
            bi = best_block(c["paragraph_context"], old_blocks)
            if bi is not None:
                loc, how = (bi, 0, len(old_blocks[bi]["text"])), "fuzzy"
        if loc is None and c.get("anchor_text"):
            bi = best_block(c["anchor_text"], old_blocks)
            if bi is not None:
                loc, how = (bi, 0, len(old_blocks[bi]["text"])), "fuzzy"
        if loc is None:
            summary["unanchored"].append(cid)
            continue
        bi, c0, c1 = loc
        reply = (resp.get("返答") or resp.get("reply") or "").strip()
        action = (resp.get("対応") or resp.get("action") or "").strip()
        entry = {"cid": cid, "c0": c0, "c1": c1, "orig": c, "reply": reply, "action": action, "how": how}
        per_block.setdefault(bi, []).append(entry)
        summary["carried" if how == "exact" else "fuzzy"].append(cid)

    def marks_for(bi: int) -> list[dict]:
        """ブロック bi に付くコメントを [{c0, c1, starts:[..], ends:[..]}] で返す。"""
        nonlocal next_id
        entries = []
        for e in per_block.get(bi, []):
            c = e["orig"]
            starts = [comment_start(e["cid"], c.get("author", "Reviewer"), c.get("date") or date, c.get("text", ""))]
            ends = [comment_end(e["cid"])]
            if e["reply"] or e["action"]:
                rid = str(next_id)
                next_id += 1
                body = f"[{e['action']}] {e['reply']}" if e["action"] else e["reply"]
                starts.append(comment_start(rid, args.author, date, body))
                ends.insert(0, comment_end(rid))
            entries.append({"c0": e["c0"], "c1": e["c1"], "starts": starts, "ends": ends})
        return entries

    # ---- ブロック対応付け
    old_keys = [b["text"] for b in old_blocks]
    new_keys = [b["text"] for b in new_blocks]
    sm = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    out_blocks: list[str] = []
    stats = {"ins_blocks": 0, "del_blocks": 0, "mod_blocks": 0}

    def emit_old_block(bi: int, wrapped: bool):
        b = old_blocks[bi]
        entries = marks_for(bi)
        if not wrapped and entries and b["kind"] not in ("code", "raw", "yaml", "table"):
            # 変更のないブロック: アンカー位置にトークン単位で正確にコメントを置く
            out_blocks.append(place_marks_lines(b["text"], entries))
            return
        body = wrap_block(b, m.dele) if wrapped else b["text"]
        if entries:
            starts = "".join(s for e in entries for s in e["starts"])
            ends = "".join(s for e in reversed(entries) for s in e["ends"])
            # ブロック全体を挟む形でコメントを付ける (削除ブロック内には入れない)
            if b["kind"] in ("para",):
                body = starts + body + ends
            else:
                first_prefix, first_rest = split_prefix(body.split("\n")[0])
                lines = body.split("\n")
                lines[0] = first_prefix + starts + first_rest
                lines[-1] = lines[-1] + ends
                body = "\n".join(lines)
        out_blocks.append(body)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for bi in range(i1, i2):
                emit_old_block(bi, wrapped=False)
        elif tag == "delete":
            for bi in range(i1, i2):
                emit_old_block(bi, wrapped=True)
                stats["del_blocks"] += 1
        elif tag == "insert":
            for bj in range(j1, j2):
                out_blocks.append(wrap_block(new_blocks[bj], m.ins))
                stats["ins_blocks"] += 1
        else:  # replace: 類似度で貪欲に対応付け
            olds, news = list(range(i1, i2)), list(range(j1, j2))
            pairs = []
            for bi in olds:
                best, best_r = None, 0.0
                for bj in news:
                    if any(p[1] == bj for p in pairs):
                        continue
                    if old_blocks[bi]["kind"] != new_blocks[bj]["kind"]:
                        continue
                    r = difflib.SequenceMatcher(None, old_keys[bi], new_keys[bj], autojunk=False).ratio()
                    if r > best_r:
                        best, best_r = bj, r
                if best is not None and best_r >= 0.4:
                    pairs.append((bi, best))
            # 出力順は new 側の順序に従う。対応のない old は削除、対応のない new は挿入
            paired_old = {p[0]: p[1] for p in pairs}
            paired_new = {p[1]: p[0] for p in pairs}
            emitted_old = set()
            for bj in news:
                if bj in paired_new:
                    bi = paired_new[bj]
                    # この bj より前に来る未出力の削除 old ブロック
                    for bk in olds:
                        if bk < bi and bk not in paired_old and bk not in emitted_old:
                            emit_old_block(bk, wrapped=True)
                            emitted_old.add(bk)
                            stats["del_blocks"] += 1
                    out_blocks.append(diff_block_pair(old_blocks[bi], new_blocks[bj], m, marks_for(bi)))
                    emitted_old.add(bi)
                    stats["mod_blocks"] += 1
                else:
                    out_blocks.append(wrap_block(new_blocks[bj], m.ins))
                    stats["ins_blocks"] += 1
            for bk in olds:
                if bk not in emitted_old:
                    emit_old_block(bk, wrapped=True)
                    stats["del_blocks"] += 1

    args.out.write_text("\n\n".join(out_blocks) + "\n", encoding="utf-8")

    lines = [
        f"# tracked summary — {args.out.name}",
        "",
        f"- 変更のあったブロック: {stats['mod_blocks']} / 追加: {stats['ins_blocks']} / 削除: {stats['del_blocks']}",
        f"- 文書に載せたコメント: {len(summary['carried']) + len(summary['fuzzy'])} 件"
        f" (うち段落単位で近似アンカー: {len(summary['fuzzy'])} 件 → ID {', '.join(summary['fuzzy']) or 'なし'})",
        f"- 載せなかったコメント (区分 {', '.join(sorted(skip))}): {len(summary['skipped'])} 件 → ID {', '.join(summary['skipped']) or 'なし'}",
        f"- アンカーが見つからず載せられなかったコメント: {len(summary['unanchored'])} 件 → ID {', '.join(summary['unanchored']) or 'なし'}",
    ]
    if summary["unanchored"]:
        lines.append("  - ↑ は response_table.md の返答を送付メッセージに直接書くこと")
    if UNTRACKABLE:
        lines.append(f"- 差分表示できず新版のみ出力したブロック (コード/生HTML/YAML): {len(UNTRACKABLE)} 件 → "
                     + "; ".join(f"`{u}`" for u in UNTRACKABLE))
        lines.append("  - ↑ は送付メッセージで「変更あり」と伝えること")
    Path(str(args.out) + ".summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
