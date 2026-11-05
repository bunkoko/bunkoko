# 研究の台帳の書き方（D-034）

台本で「〇〇大学の調査では、〜という傾向がありました」と言えるのは、この台帳に人が書いて確かめた研究の一文だけです。AI が覚えている研究や、作った研究は、検査（G1・G3）で止まります。

## 決まり

- 研究は脇役です。古典の一節の読みを補うために使います。主張を補強するためなら、1回に何度使ってもかまいません。古典の章には渡しません（悩みを見る章・まとめ・研究の章・形式C の悩みの章・形式D の比べる章に渡します）。
- **有料の論文（CC BY でないもの）の本文や要旨は、どこにも保存せず、Claude Code・ChatGPT・翻訳サービスなどのクラウドの AI に貼りません。** 台帳に書くのは、書誌と、あなたが本文を読んで自分の言葉で書いた一文・限界・効果の大きさだけです。
- CC BY・CC0 の論文は、本文を使ってかまいません（license を CC_BY・CC0 にします）。
- 台本で使えるのは、次の全部を満たす行だけです。
  - status が verified
  - verified_from が full_text（論文の本文の「結果」の節で確かめた。要旨や発表文だけなら draft のまま）
  - retraction_checked_at に、取り下げ・訂正を確かめた日がある
  - replication が failed（大きな追試で再現しなかった）でない

## 1行の書き方（config/findings.csv）

| 列 | 書くこと | 例 |
|---|---|---|
| finding_id | FN- で始まる ID | FN-comparison-2019 |
| themes | 合うテーマ（config/themes.yaml の言葉）。2つ以上は `;` で区切る | 嫉妬・比較;承認欲求 |
| claim_ja | 研究の一文。あなたの言葉で、150字まで。台本にはこのまま入る | 人と比べる時間が長い人ほど、自分の暮らしに満足しにくい傾向がありました |
| limits_ja | 限界。台本で一文のすぐあとに添える | アメリカの大学生を調べたもので、働く人にも当てはまるかは分かっていません |
| effect_ja | 効果の大きさ：わずか・小さい・中くらい・大きい（論文に書いてあれば） | 小さい |
| design | theory（考え方）・experiment（実験）・field_experiment（現場での実験）・survey（調査）・longitudinal（長く追いかけた調査）・meta_analysis（多くの研究をまとめた分析）・review（研究のまとめ） | survey |
| population_ja | 調べた人 | アメリカの大学生300人 |
| country | 国 | US |
| affiliation_ja | 語りで言う機関（「〇〇大学の研究では」） | ミシガン大学 |
| authors_ja | 語りで言う著者（機関を言わないとき） | ボリーノら |
| authors・year・title・journal・volume・issue・pages | 書誌（説明欄に載る） | Bolino, M. C.; Turnley, W. H. |
| doi・url | DOI か URL（どちらか） | 10.1002/job.635 |
| license | CC0・CC_BY・COPYRIGHTED | COPYRIGHTED |
| replication | replicated（再現した）・mixed（まちまち）・not_tested（まだ）・failed（再現しなかった） | mixed |
| replication_note | どこで確かめたか（後のメタ分析・追試の論文） | 2022年のメタ分析では効果はまちまち |
| retraction_checked_at | 取り下げ・訂正を確かめた日 | 2026-11-05 |
| verified_from | full_text・abstract・press_release | full_text |
| checked_by・checked_at | 確かめた人と日 | オーナー・2026-11-05 |
| status | verified・draft | verified |
| note | メモ | |

- 語りでは、出典が「ミシガン大学の調査（2019年）」のように入ります（affiliation_ja か authors_ja ＋ design の言葉 ＋ 年）。
- 「必ず」「証明」「誰でも」「すべての人」は一文にも限界にも書けません（読み込みで止まります）。
- 追試がまちまち・まだの研究（mixed・not_tested）は、limits_ja を必ず書きます。

## 確かめ方

1. **本文を読む**：論文の「結果」の節で、一文に書くことを確かめます。大学の図書館、自分の購読、著者が公開した原稿（リポジトリ）などで読みます。発表文（プレスリリース）や要旨は、結果を大きく言いがちなので、それだけでは verified にしません。
2. **取り下げ・訂正**：ブラウザで、論文の DOI のページ（出版社のページ）に取り下げ（Retraction）・訂正（Correction）・懸念の表明（Expression of Concern）が付いていないかを見ます。Crossref の検索（search.crossref.org）や Retraction Watch Database でも調べられます。見た日を retraction_checked_at に書きます。
3. **追試**：論文の題で「replication」「meta-analysis」を足して検索し、後の追試やメタ分析で同じ結果が出たかを見ます。社会心理学には、大きな追試で再現しなかった有名な研究（自我消耗・パワーポーズなど）があります。再現しなければ failed にして使いません。
4. **限界**：誰を調べたか（国・年齢・人数）、相関か実験か、効果の大きさを書きます。「関係がある」研究を「原因」とは書きません。理論の論文は design を theory にします（語りでは「考え方」になります）。

## SSD に入れて見る

```sh
cd /Users/bunkoko/koten
uv run koten findings build
uv run koten findings list
```

- `findings build` は、台帳に決まりに合わない行が1つでもあれば、どの行のどこかを出して止まります（1行も入れません）。
- `findings list` は、○（台本で使える）・×（使わない）と、出典・一文・限界・書誌を出します。
- 企画（`koten plan`）が、テーマの合う研究の一文を章に渡します。台本で使った研究は、確認画面と説明欄（M6）の「この動画で使った研究・データ」に載ります。
- 企画のあとで一文を直したり、使えない状態にしたりすると、台本のときに人の確認（G6）が立ちます。

## 候補の探し方

- docs/research/2026-11-05-stat-source-candidates.md の 7章に、「〇〇大の研究では」に使えそうな候補があります。
- 手本の説明欄（docs/research/2026-11-05-description-and-evidence.md）の論文も候補です。贈与交換の研究（Akerlof 1982・Kube ほか 2013）は、のちの実験で「良い待遇へのお返し」が弱い・続かないという結果もあるので、replication は mixed にし、限界を書きます。
