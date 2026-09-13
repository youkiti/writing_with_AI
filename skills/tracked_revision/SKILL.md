---
name: tracked_revision
description: Turn reviewer comments on a Google Doc / docx into a point-by-point response table, revise the Markdown, and render a docx that carries tracked changes against the reviewed version plus the original comments and the author's replies.
---

# Tracked Revision Skill (コメント対応版を「差分 + 返答つき」で返す)

レビュアーがコメントした Google Doc / docx に対して、

1. コメント 1 件 1 行の **対応表** (`response_table.md`) を作り、
2. Markdown 本文を修正し、
3. **前版に対する変更履歴 (track changes) と、元のコメント + 返答コメントが入った docx** を作る

ところまでを行う。「md を直して docx を作り直して送る」と、レビュアーのコメントが消え、
どこを直したかも分からなくなる。この Skill はそれを防ぐための固定手順。

査読対応と同じ考え方 (コメント → 返事 → 修正箇所が本文で分かる) を共著者間でも使う。

## 前提条件

- `pandoc` がインストールされていること (docx の変更履歴とコメントは pandoc が書く)
- Python 3 (標準ライブラリのみ。追加パッケージ不要)
- `rclone` + `gdrive:` があれば Google Doc の取得/アップロードをコマンドで行える。
  無くても、ブラウザで「ファイル → ダウンロード → Word (.docx)」と手動アップロードで代替できる
- レビューに出した時点の Markdown が残っていること (下記「バージョンのスナップショット」)

## 入力

- **必須**: レビュー済み docx (`review/<name>_v1_reviewed.docx`)。Google Doc なら
  `fetch_gdoc_changes` Skill でエクスポートする
- **必須**: レビューに出した版の Markdown (`versions/<name>.v1.md`)
- **必須**: 修正後の Markdown (`<name>.md`、通常は作業中の本文そのもの)
- **任意**: `refs.bib` / CSL / `reference.docx` (いつものレンダリング設定をそのまま使う)

## フォルダ構成 (プロジェクト内)

```text
projects/<name>/
├── draft.md                     # 作業中の本文 (修正後 = 新版)
├── versions/
│   ├── draft.v1.md              # レビューに出した版のスナップショット
│   └── versions.md              # いつ・どの Google Doc に出したか (URL/ID)
├── review/
│   ├── draft_v1_reviewed.docx   # レビュー済み docx
│   ├── report.md / report.json  # parse_docx_changes.py の出力
│   └── response_table_v2.md     # コメント対応表 (この Skill で作る)
└── output/
    ├── draft_v2_tracked.md      # make_tracked_md.py の出力 (中間ファイル)
    ├── draft_v2_tracked.docx    # 送付する docx
    └── report.md / report.json  # 送付前の検証 (review/ の report を上書きしないため別フォルダ)
```

## 実行手順

### 0. バージョンのスナップショット (レビューに出す側の作法)

`render_and_upload` でアップロードした直後に、その時点の Markdown をコピーして残す。

```bash
mkdir -p versions
cp draft.md versions/draft.v1.md
echo "- v1: 2026-09-12 https://docs.google.com/document/d/<FILE_ID>/edit" >> versions/versions.md
```

これが無いと差分の基準が無い。無い場合の最終手段は、レビュー済み docx から
`pandoc draft_v1_reviewed.docx --track-changes=reject -t markdown -o versions/draft.v1.md`
で復元する (引用キーが本文化されるので差分にノイズが出る。次回からはスナップショットを残す)。

### 1. レビュー済み docx を取得してコメントを一覧化する

Google Doc の場合は `fetch_gdoc_changes` Skill の手順で docx をエクスポートし、`review/` に置く。

```bash
python skills/fetch_gdoc_changes/scripts/parse_docx_changes.py review/draft_v1_reviewed.docx
```

`review/report.md` にコメント (ID・著者・アンカー文字列・本文) とレビュアーの suggestion が並ぶ。

### 2. コメント対応表を作る (AI が下書き、著者が確認)

`review/report.md` を読ませて、`review/response_table_v2.md` を次の形式で書く。
**1 列目はコメント ID** (report.md の「コメント N」の N)。列名は固定。

```markdown
| ID | 区分 | アンカー | コメント要旨 | 対応 | 返答 |
|----|------|----------|--------------|------|------|
| 0 | typo | shoud | typo: should | 修正 | |
| 1 | 内容 | uncontrolled use may ... | 根拠を引用するか表現を弱める | 修正 | 表現を can に弱め、評価研究を引用しました (Background 第1段落)。 |
| 4 | 内容 | AI disclosure example | モデルのバージョンを残す | 修正せず | ご指摘のとおり維持します。 |
```

- **区分**: `typo` (誤字・スペース・句読点) / `表記` (用語・書式の統一) / `内容` / `質問`
- **対応**: `修正` / `一部修正` / `修正せず` / `保留`
- **返答**: レビュアーがそのコメントだけ読めば分かる 1〜2 文。`修正せず` `保留` は理由を必ず書く
- `typo` の行は docx にコメントとして載せない (件数だけメッセージで伝える)。それ以外は
  元コメントの直後に返答コメントとして載る
- レビュアーの suggestion (トラック変更) は、受け入れるなら本文に反映するだけでよい。
  受け入れないものは対応表に ID 無しの行を足して理由を書く

AI への依頼文の例:

```text
@review/report.md を読んで、review/response_table_v2.md をこの Skill の形式で作ってください。
typo と内容を区別し、内容・質問には返答案を書いてください。まだ本文は直さないでください。
```

### 3. 本文 (Markdown) を直す

対応表に従って `draft.md` を修正する。`@` でセクションを指定して小さく直す (README の作法)。

```text
@review/response_table_v2.md の「修正」行に従って @draft.md を直してください。
セクションごとに、どこを変えたか短く報告してください。
```

### 4. 変更履歴 + コメント入りの docx を作る

```bash
python skills/tracked_revision/scripts/make_tracked_md.py \
  --old versions/draft.v1.md --new draft.md \
  --report review/report.json --responses review/response_table_v2.md \
  --author "Taro Yamada" --out output/draft_v2_tracked.md

pandoc output/draft_v2_tracked.md --reference-doc styles/reference.docx \
  --citeproc --bibliography refs.bib --csl styles/american-medical-association.csl \
  -o output/draft_v2_tracked.docx
```

`--author` はレビュアーに見せる自分の名前。`make_tracked_md.py` は

- 段落ごとに v1 と v2 を対応付け、語 (日本語は文字) 単位の差分を pandoc の
  `insertion` / `deletion` span にする
- 元コメントを同じアンカー位置に `comment-start` / `comment-end` として再挿入し、
  直後に `[対応] 返答` を自分名義のコメントとして付ける
- 表は行・セル単位で差分を出す。コードブロック・生 HTML は差分表示できないので新版だけを出す

pandoc が span を Word の変更履歴 (`w:ins` / `w:del`) と `comments.xml` に変換する。
`output/draft_v2_tracked.md.summary.md` に件数と「載せられなかったコメント」が出る。

### 5. 送る前に検証する

```bash
python skills/fetch_gdoc_changes/scripts/parse_docx_changes.py output/draft_v2_tracked.docx
```

`output/report.md` で次を確認する (review/ に対して実行すると入力の report.json を上書きするので注意)。

- 変更追跡の件数が 0 でない (0 なら `--old` と `--new` が同じものを指している)
- コメント件数 = 載せた元コメント数 + 返答数 (summary の数字と一致する)
- summary の「アンカーが見つからず載せられなかったコメント」があれば、その返答は
  送付メッセージに直接書く

### 6. 送る

**新しい Google Doc としてアップロードする** (旧 Doc を上書きしない。レビュアーが旧版と見比べられる)。

```bash
rclone copy output/draft_v2_tracked.docx gdrive:writing_with_AI/<name>/ --drive-import-formats docx
```

Google Docs に取り込むと、変更履歴は「提案」、コメントはコメントとして表示される。
送付メッセージには次を書く (summary の数字をそのまま使う)。

```text
v2 をアップロードしました: <URL>
- 変更は v1 (レビュー版) に対する「提案」として表示されます (変更ブロック N / 追加 N / 削除 N)
- いただいたコメント M 件のうち typo K 件は修正済み、残り M-K 件は元コメントの直後に返答を付けています
- 修正せず/保留: ID 4 (理由: ...)
- 差分表示できない箇所: なし / 表 2 を更新
```

## 注意事項

- **前版のスナップショットが差分の基準**。`draft.md` を直す前に `versions/` にコピーがあることを確認する
- `report.json` のアンカー文字列が新版で消えている (段落ごと書き直した) 場合、コメントは
  削除された段落 (取り消し線) に付く。段落自体が見つからないときは summary に出るので、
  返答をメッセージに書く
- 変更履歴の著者・日時は `--author` と実行時刻。テストの再現には `--date` を使う
- Word / Google Docs での見え方は環境で異なる。初回は自分でアップロードして「提案」と
  「コメント」が出ることを確認してから送る
- 表 (pipe table) 以外の表、コードブロック、画像の差し替えは差分表示されない。summary の
  指示に従ってメッセージで伝える
- `projects/` 配下の実原稿をアップロードするときは、共有範囲を著者が確認する (render_and_upload と同じ)

## 環境が違うときの代替

| 手段 | 必要なもの | 変更履歴 | 元コメント保持 | 備考 |
|---|---|---|---|---|
| この Skill (pandoc + Python) | pandoc, Python 3 | ○ 語単位 | ○ 返答も付く | 推奨。Markdown が正本の人向け |
| `pandiff old.md new.md -o diff.docx` | Node.js, pandoc | ○ 語単位 | × | 著者名が入らない。コメントは別途 |
| Word「比較」 (校閲 → 比較) | Word | ○ | ○ (設定でコメントを含める) | v1 docx と v2 docx を比較。GUI 操作 |
| LibreOffice「文書の比較」 | LibreOffice (無料) | ○ | 要確認 | 新版を開き、旧版を選ぶ |
| Google Docs「ドキュメントを比較」 | ブラウザのみ | ○ 提案として | 選択した側のコメントのみ | v1 Doc を開き v2 Doc を選ぶ。両方 Google Docs 形式が必要 |
| python-redlines / Docxodus | Python (pip) | ○ 移動検出あり | 要確認 | docx 同士を比較。.NET 同梱 wheel |
| Google Docs 上で直接「提案モード」で編集 | ブラウザのみ | ○ | ○ | md を使わない人向け。AI が出した修正文を貼る |

どの手段でも、**対応表 (Step 2) と送付メッセージ (Step 6) は同じ**。
