"""check_direct_edits.py — 変更履歴の外で直接書き換えられた箇所を検出する。

レビュアーが「提案モード (Suggesting)」を使わずに直接本文を打ち換えた場合や、
提案を承認 (Accept) 済みの場合、その差分は `parse_docx_changes.py` の
w:ins / w:del としては出てこないので、そのまま黙って失われる。

レビュー済み docx に残っている変更履歴を**すべて Reject** すれば、送付した版
(スナップショット) のテキストに戻るはずである。戻らない箇所があれば、それは
変更履歴を経由しない直接編集 (または承認済み提案) の跡である。

    python check_direct_edits.py --reviewed-docx review/draft_v2_reviewed.docx \
        --snapshot versions/draft.v2.md

pandoc の `--track-changes=reject` で変更履歴をすべて却下したテキストと、
送付したスナップショット Markdown のテキストを段落単位で比較する。

比較にあたっては、引用表記の違いを無視する必要がある。スナップショット側は
pandoc の citekey (`[@key]` / `[-@key]` / 地の文の `@key`) のまま、レビュー済み
docx 側は `--citeproc` + AMA CSL でレンダリング済みなので、上付き数字
(`-t plain --wrap=none` では `¹²` のような Unicode 上付き文字、複数引用は
`^(1,2)` の形) と、末尾に追加された番号付き参考文献リストになっている。
どちらも比較前に正規化で取り除く。

false positive (直接編集ではないのに warning になる) は許容するが、
false negative (直接編集を見逃す) は避ける方針で、判定はやや保守的にしてある。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- pandoc runners
def run_pandoc(args: list[str]) -> str:
    proc = subprocess.run(
        ["pandoc", *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc failed (args={args}): returncode={proc.returncode}\n{proc.stderr}"
        )
    return proc.stdout


def reviewed_plain_text(docx_path: Path) -> str:
    return run_pandoc([str(docx_path), "--track-changes=reject", "-t", "plain", "--wrap=none"])


def snapshot_plain_text(snapshot_path: Path) -> str:
    return run_pandoc([str(snapshot_path), "-t", "plain", "--wrap=none"])


# ---------------------------------------------------------------- normalization
# Unicode 上付き数字・上付きハイフン (AMA CSL が単一/地の文引用に使う)
SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
# 区切り文字 (カンマ・ハイフン・上付きマイナス) は上付き数字の「間」にあるときだけ
# 引用番号の一部として扱う。先頭・単独のカンマだけを誤って食わないように、
# 必ず上付き数字で始まり上付き数字で終わる範囲にマッチさせる。
SUPERSCRIPT_RUN_RE = re.compile(
    rf"[{SUPERSCRIPT_DIGITS}](?:[{SUPERSCRIPT_DIGITS}⁻,\-]*[{SUPERSCRIPT_DIGITS}])?"
)
# 複数引用がまとまったときの `^(1,2)` `^(1-3)` 形式 (pandoc plain の上付き代替表記)
CARET_CITATION_RE = re.compile(r"\^\([0-9,\-‐-―\s]+\)")
# 角括弧の数値引用 [1] [1,2] [1-3] [1–3]
BRACKET_CITATION_RE = re.compile(r"\[\d+(?:\s*[,\-‐-―]\s*\d+)*\]")
# pandoc citekey: [@key] [-@key] [@key1; @key2] (ブラケット形式)
BRACKET_CITEKEY_RE = re.compile(r"\s*\[-?@[^\]]*\]")
# 地の文の @key (narrative citation)。citeproc を通さない側にだけ残る。
# 直前が単語文字 (英数字・_) やピリオドなら email などの一部とみなしてマッチしない
# (例: user@example.com の "@example" を誤って消さない)。
NARRATIVE_CITEKEY_RE = re.compile(r"(?<![\w.])-?@[A-Za-z0-9_][A-Za-z0-9_:.#$%&\-+?<>~/]*")
# 参考文献リストの各項目 (AMA: "1. Author ..." 形式)
NUMBERED_REF_LINE_RE = re.compile(r"^\d+\.\s+\S")
# 表の罫線 (- のみの行) と空行
RULE_LINE_RE = re.compile(r"^[\s\-]+$")
# pandoc plain writer が図表キャプションに使う ": caption" 形式の段落。
# 画像/図を docx に変換して plain に戻すと、代替テキストの段落とは別に
# このキャプション段落が重複して出てくる (画像リソースが見つからない場合に顕著)。
# Markdown 側は `![caption](path)` が単一の `[caption]` 段落になるだけなので、
# 比較のノイズにしかならず、両側で捨てる。
CAPTION_LINE_RE = re.compile(r"^:\s+\S")
# 段落全体が `[...]` 一枚だけで囲われている場合 (pandoc plain が単独の画像/図を
# こう書く) の外側の角括弧。中身を残して比較できるようにする。
SOLO_BRACKET_RE = re.compile(r"^\[(.*)\]$")


def strip_reviewed_citations(text: str) -> str:
    text = CARET_CITATION_RE.sub("", text)
    text = SUPERSCRIPT_RUN_RE.sub("", text)
    text = BRACKET_CITATION_RE.sub("", text)
    return text


def strip_snapshot_citations(text: str) -> str:
    text = BRACKET_CITEKEY_RE.sub("", text)
    text = NARRATIVE_CITEKEY_RE.sub("", text)
    return text


def collapse_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    # 引用除去でできた「語 と句読点の間の空白」を詰める
    text = re.sub(r"\s+([.,;:!?、。」』])", r"\1", text)
    return text.strip()


def paragraphs_from(text: str, *, is_reviewed: bool) -> list[str]:
    raw_paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    paras: list[str] = []
    for p in raw_paragraphs:
        lines = [l for l in p.split("\n") if l.strip() and not RULE_LINE_RE.match(l)]
        if not lines:
            continue
        joined = " ".join(l.strip() for l in lines)
        if CAPTION_LINE_RE.match(joined):
            continue
        m = SOLO_BRACKET_RE.match(joined)
        if m:
            joined = m.group(1)
        if is_reviewed:
            joined = strip_reviewed_citations(joined)
        else:
            joined = strip_snapshot_citations(joined)
        joined = collapse_whitespace(joined)
        if joined:
            paras.append(joined)

    if is_reviewed:
        # citeproc が末尾に追加した番号付き参考文献リストを落とす。
        # 本文の末尾がたまたま番号付きリストで終わる場合との区別はつかないので、
        # 連続する末尾の "N. ..." 段落だけを保守的に落とす。
        while paras and NUMBERED_REF_LINE_RE.match(paras[-1]):
            paras.pop()
    return paras


# ---------------------------------------------------------------- diff
def snippet(text: str, limit: int = 100) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def compare(reviewed_paras: list[str], snapshot_paras: list[str]) -> list[dict]:
    sm = difflib.SequenceMatcher(None, snapshot_paras, reviewed_paras, autojunk=False)
    findings: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        findings.append(
            {
                "kind": tag,  # replace / delete (snapshotのみ) / insert (reviewedのみ)
                "snapshot": [snapshot_paras[i] for i in range(i1, i2)],
                "reviewed": [reviewed_paras[j] for j in range(j1, j2)],
            }
        )
    return findings


def print_findings(findings: list[dict]) -> None:
    labels = {"replace": "変更された段落", "delete": "スナップショットのみ (レビュー版で消失)", "insert": "レビュー版のみ (直接追加された段落)"}
    for i, f in enumerate(findings, 1):
        print(f"### {i}. {labels.get(f['kind'], f['kind'])}")
        for s in f["snapshot"]:
            print(f"  - snapshot: {snippet(s)}")
        for r in f["reviewed"]:
            print(f"  - reviewed: {snippet(r)}")
        print()


# ---------------------------------------------------------------- main
def main() -> int:
    try:  # Windows の cp932 コンソールで日本語出力が落ちないように
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reviewed-docx", required=True, type=Path, dest="reviewed_docx")
    ap.add_argument("--snapshot", required=True, type=Path, help="レビューに出した版の Markdown")
    ap.add_argument("--json", action="store_true", help="結果をJSONで出力する")
    args = ap.parse_args()

    if not args.reviewed_docx.exists():
        print(f"エラー: レビュー済み docx が見つかりません: {args.reviewed_docx}")
        return 1
    if not args.snapshot.exists():
        print(f"エラー: スナップショット Markdown が見つかりません: {args.snapshot}")
        return 1

    try:
        reviewed_raw = reviewed_plain_text(args.reviewed_docx)
        snapshot_raw = snapshot_plain_text(args.snapshot)
    except (RuntimeError, FileNotFoundError) as e:
        print(f"エラー: pandoc の実行に失敗しました: {e}")
        return 1

    reviewed_paras = paragraphs_from(reviewed_raw, is_reviewed=True)
    snapshot_paras = paragraphs_from(snapshot_raw, is_reviewed=False)

    findings = compare(reviewed_paras, snapshot_paras)

    if args.json:
        print(json.dumps({"findings": findings, "ok": not findings}, ensure_ascii=False, indent=2))
    else:
        if not findings:
            print("直接編集の疑いのある箇所はありません (変更履歴を全却下すればスナップショットと一致します)。")
        else:
            print(
                f"直接編集の疑いのある段落が {len(findings)} 件あります"
                "(変更履歴を全却下してもスナップショットと一致しませんでした)。\n"
            )
            print_findings(findings)

    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
