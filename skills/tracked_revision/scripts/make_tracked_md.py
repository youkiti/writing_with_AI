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
    # 普通の段落: 改行は空白に畳む (pandoc も同じ扱い)。ただしハードブレーク
    # (行末が半角スペース2個以上、または `\`) は維持する (A6)。
    parts: list[str] = []
    for idx, line in enumerate(lines):
        is_last = idx == len(lines) - 1
        core = line.rstrip(" ")
        backslash_break = (not is_last) and core.endswith("\\") and not core.endswith("\\\\")
        if backslash_break:
            core = core[:-1]
        trailing_spaces = len(line) - len(line.rstrip(" "))
        space_break = (not is_last) and not backslash_break and trailing_spaces >= 2
        parts.append(core.strip())
        if not is_last:
            parts.append("  \n" if (backslash_break or space_break) else " ")
    return {"kind": "para", "text": "".join(parts)}


# ---------------------------------------------------------------- span helpers
def esc(text: str) -> str:
    """span の中身 (コメント本文など) 用に Markdown 記号をエスケープ。"""
    text = re.sub(r"\s*\n\s*", " ", text.strip())
    return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", text)


def attr_esc(value: str) -> str:
    """pandoc の属性値 (author="..." など) 用に \\ と " をエスケープする。"""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


class Marks:
    def __init__(self, author: str, date: str):
        self.author = author
        self.date = date

    def ins(self, s: str) -> str:
        return self._wrap(s, "insertion")

    def dele(self, s: str) -> str:
        return self._wrap(s, "deletion")

    def _wrap(self, s: str, cls: str) -> str:
        """先頭/末尾の空白 (ハードブレークの "  \\n" を含む) は span の外に出す。
        中に入れたままだと pandoc の docx 変換で失われる (A8)。"""
        if s.strip() == "":
            return s
        lead = s[: len(s) - len(s.lstrip())]
        trail = s[len(s.rstrip()):]
        core = s.strip()
        return (
            f'{lead}[{core}]{{.{cls} author="{attr_esc(self.author)}" date="{attr_esc(self.date)}"}}{trail}'
        )


def comment_start(cid: str, author: str, date: str, body: str) -> str:
    return f'[{esc(body)}]{{.comment-start id="{attr_esc(cid)}" author="{attr_esc(author)}" date="{attr_esc(date)}"}}'


def comment_end(cid: str) -> str:
    return f'[]{{.comment-end id="{attr_esc(cid)}"}}'


PAIRS = {"[": "]", "(": ")", "{": "}"}
UNTRACKABLE: list[str] = []  # 差分表示できずに新版だけを出したブロック (先頭行)
DELETED_UNTRACKABLE: list[str] = []  # 追跡できず出力から除外した削除ブロック (先頭行)

# 強調・打消し線・コードの区切り記号。長い方から先にマッチさせる (** が * より先)。
DELIM_TOKEN_RE = re.compile(r"\*\*|\*|~~|__|_|`+")


def balanced(s: str) -> bool:
    """span で包んでも壊れない (括弧・強調・打消し線・コードが同じ範囲内で
    開いて閉じている) か。単純な文字数の偶奇ではなく、区切り記号を
    トークン単位 (**, *, ~~, __, _, `, `` ...) で数える。例えば "**old"
    は '*' が2個で偶数だが、"**" という1トークンしかなく閉じていない。"""
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
    if s.count("$") % 2:
        return False
    counts: dict[str, int] = {}
    for mm in DELIM_TOKEN_RE.finditer(s):
        tok = mm.group(0)
        if tok in ("_", "__"):
            # snake_case の下線は無視し、単語境界にあるものだけ数える
            i0, i1 = mm.start(), mm.end()
            before = s[i0 - 1] if i0 > 0 else ""
            after = s[i1] if i1 < len(s) else ""
            if (before.isalnum() or before == "_") and (after.isalnum() or after == "_"):
                continue
        counts[tok] = counts.get(tok, 0) + 1
    if any(v % 2 for v in counts.values()):
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
        return text  # 包めない: そのまま出力し summary で「新版のみ」と知らせる。
        # 呼び出し側が「削除」の文脈なら、この関数を呼ぶ前に別処理で除外すること。
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
        """丸ごと置換 (コメント付きなら削除ブロックをコメントで挟む)。
        old が code/raw/yaml で安全に追跡できない場合は、削除側を出力せず
        summary に記録する (A3)。"""
        starts = "".join(s for e in entries for s in e["starts"])
        ends = "".join(s for e in reversed(entries) for s in e["ends"])
        if old["kind"] in ("code", "raw", "yaml"):
            DELETED_UNTRACKABLE.append(old["text"].split("\n")[0][:60])
            return starts + ends + wrap_block(new, m.ins)
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
def split_table_row(line: str) -> list[str]:
    """Markdown 表の1行をセルに分割する。`\\|` はセル内のリテラル `|` として扱う (A5)。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        # 直前が \| (エスケープされた |) でなければ末尾の | を落とす
        k = len(s) - 2
        bs = 0
        while k >= 0 and s[k] == "\\":
            bs += 1
            k -= 1
        if bs % 2 == 0:
            s = s[:-1]
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def load_responses(path: Path | None) -> dict[str, dict]:
    """response_table.md の Markdown 表を読む。1 列目 = コメント ID。
    列名は見出し行から拾う (ID / 区分・kind / 対応・action / 返答・reply を想定)。
    必須列が無い・ID が重複している場合は ValueError を送出する (A5)。"""
    if not path:
        return {}
    rows: dict[str, dict] = {}
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip().startswith("|"):
            continue
        cells = split_table_row(line)
        if header is None:
            header = cells
            required = [("ID", "id"), ("区分", "kind"), ("対応", "action"), ("返答", "reply")]
            missing = [jp for jp, en in required if jp not in header and en not in header]
            if missing:
                raise ValueError(f"response_table.md: 必須列が見つかりません: {', '.join(missing)}")
            continue
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            continue
        rec = dict(zip(header, cells))
        cid = cells[0].strip("# ").strip() if cells else ""
        if not cid:
            continue
        if cid in rows:
            raise ValueError(f"response_table.md: ID が重複しています: {cid}")
        rows[cid] = rec
    return rows


def plain_text(md: str) -> str:
    """アンカー検索用にざっくり Markdown 記号を落とす。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[*_`]", "", s)
    return s


ANCHOR_SKIP_KINDS = ("code", "raw", "yaml", "table")


def find_anchor_occurrences(anchor: str, blocks: list[dict]) -> tuple[list[tuple[int, int, int]], bool]:
    """anchor 文字列に一致する (block_idx, char_from, char_to) を全ブロック・
    全出現箇所について返す。表・コード・生HTML・YAML ブロックは対象外 (A2)。
    戻り値の bool は、素の文字列検索では見つからず plain_text() で記号を
    落として初めて見つかった (= ブロック全体を近似アンカーにした / fuzzy)
    かどうか。"""
    exact: list[tuple[int, int, int]] = []
    for bi, b in enumerate(blocks):
        if b["kind"] in ANCHOR_SKIP_KINDS:
            continue
        text = b["text"]
        start = 0
        while True:
            pos = text.find(anchor, start)
            if pos < 0:
                break
            exact.append((bi, pos, pos + len(anchor)))
            start = pos + 1
    if exact:
        return exact, False
    fuzzy: list[tuple[int, int, int]] = []
    for bi, b in enumerate(blocks):
        if b["kind"] in ANCHOR_SKIP_KINDS:
            continue
        if plain_text(b["text"]).find(anchor) >= 0:
            fuzzy.append((bi, 0, len(b["text"])))
    return fuzzy, True


def anchor_in_table_block(anchor: str, blocks: list[dict]) -> bool:
    """anchor が表ブロックの中にしか見つからないかどうか (A2: 表内のコメントは
    位置を特定できないので載せない)。"""
    for b in blocks:
        if b["kind"] != "table":
            continue
        if anchor in b["text"] or plain_text(b["text"]).find(anchor) >= 0:
            return True
    return False


def disambiguate_by_context(
    occurrences: list[tuple[int, int, int]], context: str, blocks: list[dict]
) -> tuple[int, int, int] | None:
    """同じ anchor が複数箇所にある場合、paragraph_context と一意に最も
    類似するブロックがあればその occurrence を返す (A2)。あるブロック内に
    anchor が複数回出現している場合は、その中のどこかまでは絞れないので
    対象にしない。一意に決まらなければ None (呼び出し側でコメントを諦める)。"""
    if not context:
        return None
    by_block: dict[int, list[tuple[int, int, int]]] = {}
    for o in occurrences:
        by_block.setdefault(o[0], []).append(o)
    singles = [bi for bi, occ in by_block.items() if len(occ) == 1]
    if not singles:
        return None
    ratios = sorted(
        (
            (bi, difflib.SequenceMatcher(None, context, blocks[bi]["text"], autojunk=False).ratio())
            for bi in singles
        ),
        key=lambda x: -x[1],
    )
    best_bi, best_r = ratios[0]
    if best_r < 0.5:
        return None
    if len(ratios) > 1 and ratios[1][1] == best_r:
        return None  # 一意に決まらない
    return by_block[best_bi][0]


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
        if b["kind"] in ANCHOR_SKIP_KINDS:
            continue
        r = difflib.SequenceMatcher(None, context, b["text"], autojunk=False).ratio()
        if r > best_r:
            best, best_r = bi, r
    return best if best_r >= 0.5 else None


def pair_blocks_ordered(
    olds: list[int], news: list[int], old_blocks: list[dict], new_blocks: list[dict]
) -> list[tuple[int, int]]:
    """replace ハンクの中で old/new ブロックを対応付ける。貪欲な最良一致では
    交差した対応 (old の並びと new の並びが逆転する) が起こり得るため、
    old/new どちらの並びも保ったまま類似度の合計が最大になる組み合わせを
    重み付き最長共通部分列 (DP) で求める (A4)。閾値 0.4 は既存の値を踏襲。"""
    n, k = len(olds), len(news)
    sim = [[0.0] * k for _ in range(n)]
    for a, bi in enumerate(olds):
        for b, bj in enumerate(news):
            if old_blocks[bi]["kind"] != new_blocks[bj]["kind"]:
                continue
            r = difflib.SequenceMatcher(
                None, old_blocks[bi]["text"], new_blocks[bj]["text"], autojunk=False
            ).ratio()
            if r >= 0.4:
                sim[a][b] = r
    dp = [[0.0] * (k + 1) for _ in range(n + 1)]
    for a in range(n - 1, -1, -1):
        for b in range(k - 1, -1, -1):
            best = max(dp[a + 1][b], dp[a][b + 1])
            if sim[a][b] > 0:
                best = max(best, sim[a][b] + dp[a + 1][b + 1])
            dp[a][b] = best
    pairs: list[tuple[int, int]] = []
    a = b = 0
    while a < n and b < k:
        if sim[a][b] > 0 and dp[a][b] == sim[a][b] + dp[a + 1][b + 1]:
            pairs.append((olds[a], news[b]))
            a += 1
            b += 1
        elif dp[a][b] == dp[a + 1][b]:
            a += 1
        else:
            b += 1
    return pairs


def reconcile_not_carried(
    comments: list[dict], carried_ids: set[str], skipped_ids: set[str], not_carried: list[dict]
) -> None:
    """summary の安全網。skip も not_carried も判定されていないのに、実際の
    出力に comment-start が現れなかったコメントがあれば、理由付きで
    not_carried に追加する (in place)。現行の main() のロジックでは
    アンカー付けに成功したコメントは必ずどこかの block 出力で
    comment-start/comment-end が emit されるはずなので、通常はこの関数が
    何もしない想定。将来の実装変更で emit 漏れが起きても、コメントが
    ID ごと summary から消えないようにするための保険。"""
    not_carried_ids = {item["id"] for item in not_carried}
    for c in comments:
        cid = str(c.get("id", ""))
        if cid in skipped_ids or cid in not_carried_ids or cid in carried_ids:
            continue
        not_carried.append({"id": cid, "reason": "出力に含まれませんでした (内部で位置を失いました)"})
        not_carried_ids.add(cid)


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
    ap.add_argument(
        "--include-own-comments",
        action="store_true",
        help="--author と同じ著者の report.json コメント (前ラウンドの自分の返信) も、"
             "検証・アンカー付けの対象にする。既定では除外する。",
    )
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    date = args.date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = Marks(args.author, date)

    # ---- 入力ファイルの検証 (A5)
    if args.report is not None and not args.report.exists():
        print(f"エラー: --report で指定したファイルが見つかりません: {args.report}", file=sys.stderr)
        return 1
    if args.responses is not None and not args.responses.exists():
        print(f"エラー: --responses で指定したファイルが見つかりません: {args.responses}", file=sys.stderr)
        return 1

    old_blocks = split_blocks(args.old.read_text(encoding="utf-8"))
    new_blocks = split_blocks(args.new.read_text(encoding="utf-8"))

    # ---- コメントを old ブロックに紐付ける
    comments = []
    if args.report is not None:
        comments = json.loads(args.report.read_text(encoding="utf-8")).get("comments", [])

    # ラウンド2以降、report.json には前回自分が書いた返信コメントも
    # 混ざって返ってくる。既定ではそれらを検証・アンカー付けの対象から
    # 除外する (--include-own-comments で無効化できる)。
    own_comment_ids: list[str] = []
    if not args.include_own_comments:
        remaining = []
        for c in comments:
            if (c.get("author") or "").strip() == args.author.strip():
                own_comment_ids.append(str(c.get("id", "")))
            else:
                remaining.append(c)
        comments = remaining

    try:
        responses = load_responses(args.responses)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    skip = {k.strip().lower() for k in args.skip_kind.split(",") if k.strip()}

    if args.responses is not None:
        report_ids = {str(c.get("id", "")) for c in comments}
        unknown = sorted(rid for rid in responses if rid not in report_ids)
        if unknown:
            print(
                f"エラー: response_table.md に report.json にない ID があります: {', '.join(unknown)}",
                file=sys.stderr,
            )
            return 1
        missing_rows = [str(c.get("id", "")) for c in comments if str(c.get("id", "")) not in responses]
        if missing_rows:
            print(
                "エラー: report.json のコメントに対応する行が response_table.md にありません: "
                f"ID {', '.join(missing_rows)}",
                file=sys.stderr,
            )
            return 1
        empty_reply = []
        for rid, rec in responses.items():
            rkind = (rec.get("区分") or rec.get("kind") or "").strip().lower()
            raction = (rec.get("対応") or rec.get("action") or "").strip()
            rreply = (rec.get("返答") or rec.get("reply") or "").strip()
            if rkind in skip and raction == "修正":
                continue  # 修正済み typo は返答不要
            if not rreply:
                empty_reply.append(rid)
        if empty_reply:
            print(
                f"エラー: response_table.md の返答が空です: ID {', '.join(sorted(empty_reply))}",
                file=sys.stderr,
            )
            return 1

    per_block: dict[int, list[dict]] = {}
    summary = {"skipped": [], "not_carried": []}
    numeric_ids = [int(c["id"]) for c in comments if str(c.get("id", "")).isdigit()]
    # 返信コメントの ID が report.json の ID と衝突しないようにする (A5)。
    next_id = ((max(numeric_ids) // 1000) + 1) * 1000 if numeric_ids and max(numeric_ids) >= 1000 else 1000
    fuzzy_ids: list[str] = []
    for c in comments:
        cid = str(c.get("id", ""))
        resp = responses.get(cid, {})
        kind = (resp.get("区分") or resp.get("kind") or "").strip().lower()
        action = (resp.get("対応") or resp.get("action") or "").strip()
        if kind in skip and action == "修正":
            summary["skipped"].append(cid)
            continue

        anchor = re.sub(r"\s+", " ", (c.get("anchor_text") or "").strip())
        loc: tuple[int, int, int] | None = None
        how: str | None = None
        reason: str | None = None
        if anchor:
            occurrences, is_fuzzy = find_anchor_occurrences(anchor, old_blocks)
            if len(occurrences) == 1:
                loc, how = occurrences[0], ("fuzzy" if is_fuzzy else "exact")
            elif len(occurrences) > 1:
                resolved = disambiguate_by_context(occurrences, c.get("paragraph_context", ""), old_blocks)
                if resolved is not None:
                    loc, how = resolved, ("fuzzy" if is_fuzzy else "exact")
                else:
                    reason = "アンカー文字列が複数箇所に一致し、位置を一意に特定できません"
            elif anchor_in_table_block(anchor, old_blocks):
                reason = "アンカーが表内にあり、位置を特定できません"
        if loc is None and reason is None and c.get("paragraph_context"):
            bi = best_block(c["paragraph_context"], old_blocks)
            if bi is not None:
                loc, how = (bi, 0, len(old_blocks[bi]["text"])), "fuzzy"
        if loc is None and reason is None and c.get("anchor_text"):
            bi = best_block(c["anchor_text"], old_blocks)
            if bi is not None:
                loc, how = (bi, 0, len(old_blocks[bi]["text"])), "fuzzy"
        if loc is None:
            summary["not_carried"].append(
                {"id": cid, "reason": reason or "本文中にアンカー文字列が見つかりません"}
            )
            continue
        bi, c0, c1 = loc
        reply = (resp.get("返答") or resp.get("reply") or "").strip()
        entry = {"cid": cid, "c0": c0, "c1": c1, "orig": c, "reply": reply, "action": action, "how": how}
        per_block.setdefault(bi, []).append(entry)
        if how == "fuzzy":
            fuzzy_ids.append(cid)

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
        if wrapped and b["kind"] in ("code", "raw", "yaml"):
            # 削除ブロックは安全に追跡できないので出力しない (A3)
            DELETED_UNTRACKABLE.append(b["text"].split("\n")[0][:60])
            return
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
        else:  # replace: 順序を保ったまま類似度が最大になるように対応付け (A4)
            olds, news = list(range(i1, i2)), list(range(j1, j2))
            pairs = pair_blocks_ordered(olds, news, old_blocks, new_blocks)
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

    body_text = "\n\n".join(out_blocks) + "\n"
    args.out.write_text(body_text, encoding="utf-8", newline="\n")

    # 「文書に載せたコメント」は組み立て段階の帳簿ではなく、実際に出力された
    # テキストに comment-start が現れた元コメント ID を数えて求める (A2)。
    original_ids = [str(c.get("id", "")) for c in comments]
    carried_ids = [cid for cid in original_ids if f'.comment-start id="{cid}"' in body_text]
    carried_fuzzy_ids = [cid for cid in carried_ids if cid in fuzzy_ids]

    # 安全網: skip も not_carried もされていないのに出力に現れなかった
    # コメントがあれば summary から抜け落ちさせない (A2 の安全網)。
    reconcile_not_carried(comments, set(carried_ids), set(summary["skipped"]), summary["not_carried"])

    lines = [
        f"# tracked summary — {args.out.name}",
        "",
        f"- 変更のあったブロック: {stats['mod_blocks']} / 追加: {stats['ins_blocks']} / 削除: {stats['del_blocks']}",
        f"- 文書に載せたコメント: {len(carried_ids)} 件"
        f" (うち段落単位で近似アンカー: {len(carried_fuzzy_ids)} 件 → ID {', '.join(carried_fuzzy_ids) or 'なし'})",
        f"- 自分のコメントとして除外: {len(own_comment_ids)} 件 → ID {', '.join(own_comment_ids) or 'なし'}",
        f"- 載せなかったコメント (区分 {', '.join(sorted(skip))}): {len(summary['skipped'])} 件 → ID {', '.join(summary['skipped']) or 'なし'}",
    ]
    not_carried = summary["not_carried"]
    lines.append(f"- 載せなかったコメント (位置を特定できない): {len(not_carried)} 件")
    for item in not_carried:
        lines.append(f"  - ID {item['id']}: {item['reason']}")
    if not_carried:
        lines.append("  - ↑ は response_table.md の返答を送付メッセージに直接書くこと")
    if DELETED_UNTRACKABLE:
        lines.append(
            f"- 削除されたが追跡できず出力から除外したブロック (コード/生HTML/YAML): {len(DELETED_UNTRACKABLE)} 件 → "
            + "; ".join(f"`{u}`" for u in DELETED_UNTRACKABLE)
        )
    if UNTRACKABLE:
        lines.append(f"- 差分表示できず新版のみ出力したブロック (コード/生HTML/YAML): {len(UNTRACKABLE)} 件 → "
                     + "; ".join(f"`{u}`" for u in UNTRACKABLE))
        lines.append("  - ↑ は送付メッセージで「変更あり」と伝えること")
    summary_text = "\n".join(lines) + "\n"
    Path(str(args.out) + ".summary.md").write_text(summary_text, encoding="utf-8", newline="\n")
    print(summary_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
