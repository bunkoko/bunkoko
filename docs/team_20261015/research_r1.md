# 調査役 第 1 回報告（方針を決めるための材料集め）

書いた日: 2026-10-15。調べた人: 調査役。出典は約 75 本（URL は各項目に添えた）。

## 0. 先に断っておくこと（信頼度の付け方）

- **原典は 1 本も読めていない。** このクラウドでは WebFetch が名前解決に失敗し（`ENOTFOUND`）、curl も出口の方針で 403 だった。
  使えたのは WebSearch だけで、返ってくるのは検索結果の要約（論文の概要欄・公式ページの抜粋・解説記事）。
  なので **「確認済み（原典を読んだ）」は 0**。以下の信頼度は次の 3 段階で書く。
  - **二次（公式）**: 公式サイト（JPX・J-Quants・金融庁・SBI など）のページ抜粋を検索結果で見た。数字は信頼してよいが、現行の条件は使う前に原文で確かめる
  - **二次（論文の概要）**: 論文の概要欄や出版社・大学リポジトリの要約を見た。主張は確か、効果の大きさの数字は取れないことが多い
  - **二次（解説）**: 個人ブログ・証券会社の解説・まとめサイト。数字は参考まで
  - **推測**: 私の考え。または私の知識で、今回の検索では確かめていないもの（そう書く）
- 「見つからなかった」ものは、そう書く。Mac なら原典に届く可能性が高いので、最後の「Mac での確認」に原典を読むべき順を書いた。

---

## 結論 10 行

1. **M2（日本株の 5 日・機械学習）に当たる外部の証拠は無かった。** 日本で「1 週間」「コスト後」「機械学習」をそろえて検証した研究は見つからず、あるのは月次（Abe & Nakayama 2018）と米国（Krauss ほか、Gu ほか）。米国の機械学習は回転率が高く、コストが決め手になる。M2 は台帳での先のデータ確認を続けるしかない。
2. **E1（予想の引き上げ後）**: 日本の経営者予想の研究は「期初予想が楽観的で期中に下方修正が多い」「予想の裁量が大きい会社は後で負ける」（Kato-Skinner-Kunimura、Kitagawa-Shuto）まで。**修正後のドリフトを直接測った日本の研究は見つからなかった。** 決算後ドリフト（PEAD）は日本で 2002〜2020 年に弱まったが、個人投資家の持ち分が多い株には残る（Jinushi 2023）。E1 は「根拠は間接的に揃う・直接の追試は無い」。
3. **11〜4 月（T3/T4）・出来高（F1）**: 11〜4 月は日本で 2020 年代の追試が見つからず、米国 2003 年以降は消えたという報告もある。出来高の急増後に上がる効果は 41 か国で報告（Kaniel ほか 2012）だが、日本の追試は見つからず、C1/C3 は「日本の割安株は 2000 年代後半から弱い」「2023 年の東証要請後に日本のモメンタムが復活」と**局面が変わった**という証拠が出ている。
4. **B の中で最も筋が良いのは「自社株買いの発表後」。** 日本で発表時の超過収益が 2〜5%（二次）、TOPIX500 内に件数が十分ありそうで、経済的な理由（過小評価のシグナル）がある。ただし「発表の翌日の寄りで買う」前提では発表時の跳ねは取れない。長い期間のドリフトを測った日本の研究は見つからなかった。
5. **指数の入れ替え・優待・増配・公募増資・資本コスト要請は、そのままでは試す価値が低い。** 指数効果は米国で 7.4% → 1% 未満に消えた（Greenwood-Sammon）。日経 225 は採用が年に数銘柄で統計にならない。TOPIX の見直しは小型株が対象で、TOPIX500 の外。優待の権利日の前の値上がりは学術研究が見つからなかった。公募増資は「買わない」にしか使えない。資本コスト要請は事象研究が見つからず、「局面」の話。
6. **夜と昼（N2）の実コスト**: 国内株の手数料は SBI（ゼロ革命）・楽天（ゼロコース）とも条件つきで 0 円（二次・公式）。寄り・引けの板寄せは売り買いの差を払わない仕組み（推測。公式の約定規則は未確認）。**往復 0.10% の仮定は、手数料 0 の口座ならむしろ高めの可能性が高い**が、実測の根拠は見つからなかった。S株は別物（証券会社が相手方。差があるはず）。実測が最も早い。
7. **商品 CFD の「まだ試していないもの」（basis momentum・skewness・ヘッジ圧力）は、どれも 20 以上の商品を並べる横断の手法**で、金・銀・WTI・ブレントの 4 つでは作れない。COT（ヘッジ圧力）は試し済み。単品の使い方は WTI のキャリーで試し済み。**新しい筋は無い。**
8. **暗号資産**: Zarattini-Pagani-Barbon（Swiss Finance Institute 2025）の「ドンチアンの合成 × 流動性上位 20 銘柄」が最新（手数料後シャープ 1.5 超・対ビットコインのアルファ 10.8%、二次）。ただし 2018〜 の強気相場を含み、20 銘柄を持つ前提。曜日・時間帯の効果は一貫した結果が無い（Baur ほか）。
9. **IR を読む AI**: 題名の埋め込みが効かなかったのは自然（題名にほぼ情報が無い）。日本の有価証券報告書を LLM で採点すると将来リターンと**有意な負**の相関（Okada ほか 2025、TOPIX500 に絞ると大きい）。ただし LLM の学習データに 2014〜2023 年が入っているので**先読みが混ざる恐れ**が強い。先読みの対策（学習の締め切り後だけで測る・時点を守る小さなモデル）は研究が出ている。
10. **データ**: J-Quants は Free（2 年・12 週遅れ）／Light 月 1,650 円（5 年）／Standard 月 3,300 円（10 年・信用取引）／Premium 月 16,500 円（20 年）。**TDnet の追加（月 11,000 円・Light 以上・個人のみ・過去 5 年）が 2026-05-18 に出た。** 個人の私的利用のみ・データの再配布は禁止（公開リポジトリにデータを入れてはいけない）。自動売買への使用は規約に明記が無い。税は株 20.315%（損失は 3 年繰越・毎年申告が要る）、商品 CFD も 20.315%（先物の雑所得、株とは通算不可）。

---

## A. 「まだ判断できない」ものの外部の証拠（日本を優先）

### A1. 日本株の短期（1 週間前後）の反転・機械学習（M2 に当たる）

| 項目 | 内容 |
|---|---|
| 主張 | 日本で過去に下がった株は戻る（反転）。機械学習で銘柄を選ぶと勝てる |
| 日本の反転 | 加藤・宮崎（OR 学会誌、1990 年代後半〜2000 年代前半）で日本株の反転を確認。ただし 1 週間の保有に絞った分析ではない（二次、解説）。日経 300 の 2001〜2005 では「反転はあるがコストを引くと期待しにくい」（repo の stocks_short.md の記述と同じ）。アジア太平洋（日本・豪・ベトナム）の大きな 1 日の値動きの後は反転するが、大きく下がった株の利益は「パッシブ運用より小さく取りにくい」（Emerald の 1 章、二次） |
| 日本の機械学習 | Abe & Nakayama（Nomura AM・東大、arXiv 1801.01777、2018）が日本の 1 か月先のリターンを深層学習で予測。深いネットは浅いネットや既存の機械学習に勝つ。**概要欄に収益の数字は無く、取れなかった**（二次）。**1 週間・コスト込みの日本の研究は見つからなかった** |
| 米国・国際の機械学習 | Gu-Kelly-Xiu（RFS 2020）は月次。「月に 100% 以上の回転が要る」と日本語の JAFEE 論文が指摘（二次、解説）。Azevedo-Hoegner-Velikov（米国）は回転率 50% 超でも、**ネットの月次 1.42% まで**（二次）。Hanauer & Kalsbach（新興国）は「コスト 300bp までは長短で有意」（TUM の版、二次）。Tobek & Hronec（153 の要因、JFM 2021）は「アジア太平洋は粗い収益が高いが、取引の摩擦が高く、粗の数字は割り引く」（CXO の要約、二次） |
| 論文の発表後に弱まったか | McLean & Pontiff 2016: 97 の予測要因で、標本の外で −26%、発表後 −58%（二次）。Jacobs & Müller（JFE 2020）: 39 市場の 138 の異常で、**米国以外の 38 市場に有意な発表後の低下は無い**（二次）。つまり日本では「発表後に消える」の根拠は弱い。ただし Krauss ほか（米国の機械学習）は年を追って下がり、2010〜2015 は粗でもマイナス |
| 私たちの条件 | 日本の大型株を 5 日持ち、往復 0.10%。Novy-Marx & Velikov（RFS 2016）: 月の片道回転率が 50% を超える異常は、コスト後にほぼ残らない（二次）。5 日保有は週 1 回の入れ替えなので回転率は桁違いに高い。**M2 の年 +3.7%（t 2.47、後半 1.41）はこの型の予想どおり「小さく、割れ目が薄い」** |
| 必要なデータ | 値段と出来高があれば足りる（repo で作成済み） |
| 信頼度 | 二次（論文の概要・解説）。日本の 1 週間の機械学習は**見つからなかった** |

出典:
- Abe & Nakayama 2018: https://arxiv.org/pdf/1801.01777
- Krauss ほかの要約と日本の反転: https://www.jstage.jst.go.jp/article/jafee/21/0/21_43/_html/-char/ja
- アジア太平洋の大きな値動きの後: https://emerald.com/insight/content/doi/10.1016/S1569-3767(07)00010-6
- Jacobs & Müller: https://www.hemf.wiwi.uni-due.de/forschung/publikationen/anomalies-across-the-globe-once-public-no-longer-existent-14591
- McLean & Pontiff（要約）: https://Www.Gwern.net/doc/economics/2016-mclean.pdf
- Novy-Marx & Velikov: https://www.nber.org/papers/w20721
- Tobek & Hronec の要約: https://www.cxoadvisory.com/equity-premium/exploitability-of-stock-anomalies-worldwide
- Hanauer & Kalsbach 周辺: https://alphaarchitect.com/machine-learning-and-emerging-market-stock-returns/
- Azevedo ほか: https://kluedo.ub.rptu.de/frontdoor/index/index/year/2024/docId/7871

**M2 についての見立て（推測）**: 外部の証拠は M2 の判断を動かさない。米国の機械学習は回転率の高さが問題で、日本に限った 5 日の追試が無いため、「効く」とも「効かない」とも外から言えない。台帳での先のデータ確認が正しい手順。

### A2. 会社の業績予想の修正の後の値動き（E1）。日本の経営者予想

| 項目 | 内容 |
|---|---|
| 日本の特殊さ | ほぼ全社が決算短信で通期予想を出す。東証の要請・開示制度で、重要な変化が見込まれれば修正を出す。ほとんどの上場会社が点推定で出す（CARF F367、NCCU 論文、二次） |
| 予想の偏り | Kato, Skinner & Kunimura（2009、日本の「事実上の義務」予想）: **期初予想は系統的に楽観的で、期中に下げて、決算でのサプライズが非負になるよう調整される**（二次）。Caton ほか（2011）: 修正は決算日に近づくほど下方に動き、時系列相関がある（二次）。他方 Takehara（神戸大）は**下方バイアスで過度に保守的な会社もある**と報告し、日本の開示制度に特有かもしれないと述べる（二次） |
| 裁量と株価 | Kitagawa & Shuto（RIEB 2013-30、JBFA 2024）: 初回予想に裁量が大きい（予想と前期実績の差が不自然な）会社は、**その後 12 か月の超過収益がマイナス**。投資家は発表時に裁量の意味を十分に理解せず、過大評価する（二次）。Pan（2019）: 上方修正する会社は裁量の余地が大きく、下方より「管理」しやすいと論じる（二次） |
| 株価の反応 | Conroy-Eades-Harris（JF 2000）: 日本の株価は決算サプライズ、特に**来期の経営者予想**に強く反応する。配当サプライズの影響は小さい（二次）。現代ビジネスの簡易検証: TOPIX 構成銘柄で今期・来期の営業利益予想がともに上方修正された株は、2023-10〜2025-10 に翌月も上がる傾向（雑誌記事。検定ではない、二次（解説）） |
| 修正後のドリフト | **日本で修正後のドリフトを直接測った研究は見つからなかった**（日本語・英語とも 3 回検索）。NLI 研究所の資料は「上方修正を額面どおりに受け取るのは危険。会計発生高の大きい会社ほど後のリターンが低い」と述べる（二次（解説）） |
| 決算後ドリフト（PEAD）の最新 | Jinushi（Japanese Accounting Review 2023、2002〜2020 年、60,124 社年）: **PEAD は期間を通じて低下**。ただし**外国人持ち分が低い・個人持ち分が高い株では低下しない**。Kasahara & Zhong（阪大）: PEAD は日本で今も存在するが、流動性と規模で調整すると弱まる（二次） |
| 論文の発表後に弱まったか | PEAD は弱まった（Jinushi）。E1 のような「修正」そのものは対象になっていない |
| 私たちの条件 | TOPIX500 は外国人持ち分が高い大型株なので、Jinushi の「消えない」領域（個人持ち分の高い株）から外れる。**E1 の +4.8%（t 2.27）が出ているのは、外から見ると「弱まった PEAD の仲間」であり、大型株では小さくなる側** |
| 必要なデータ | 予想修正の開示（TDnet）。repo は上場会社情報サービスから 2016-10〜の 22,310 件を取得済み。J-Quants の TDnet 追加は過去 5 年のみ（D 章） |
| 信頼度 | 二次（論文の概要）。直接の追試は**見つからなかった** |

出典:
- Kitagawa & Shuto（RIEB DP 2013-30）: https://www.rieb.kobe-u.ac.jp/academic/ra/dp/English/dp2013-30.html
- Kitagawa & Shuto（JBFA 2024）周辺の整理: https://ideas.repec.org/p/kob/dpaper/dp2013-30.html
- CARF F367: https://www.carf.e.u-tokyo.ac.jp/old/pdf/workingpaper/fseries/F367.pdf
- Kato-Skinner-Kunimura の要旨: https://academicnewsletter.sufe.edu.cn/info/416033
- Caton ほか 2011: https://ideas.repec.org/a/eee/reveco/v20y2011i3p376-387.html
- Pan 2019: https://ah.lib.nccu.edu.tw/bitstream/140.119/136094/1/177.pdf
- Takehara（TJAR）: https://www.rieb.kobe-u.ac.jp/tjar/conference/9th/pdf/CA1_HitoshiTakehara.pdf
- Conroy-Eades-Harris: https://ideas.repec.org/a/bla/jfinan/v55y2000i3p1199-1227.html
- Jinushi 2023: https://www.jstage.jst.go.jp/article/tjar/13/1/13_1/_article/-char/en
- Kasahara & Zhong: https://ideas.repec.org/p/osk/wpaper/2125.html
- NLI 研究所: https://www.nli-research.co.jp/files/topics/38181_ext_18_0.pdf?site=nli
- 現代ビジネス（簡易検証）: https://gendai.media/articles/-/160232

**E1 についての見立て（推測）**: 日本の制度の特殊さ（ほぼ全社が予想を出し、期初は楽観的で下方修正が多い）から、**5% 以上の上方修正は珍しく、情報量が大きい**という筋は通る。一方で、予想の裁量が大きい会社は後で負けるという証拠もあるので、「上方修正がどんな理由か（為替・一過性・本業）」で分けると差が出る見込みがある。理由の文章を読むのは C 章の IR 読解の出番。

### A3. 出来高の急増（F1）、11〜4 月（T3/T4）、日本の割安株（C3）、米国のモメンタム（C1）

| 項目 | 主張・数字 | 発表後に弱まったか | 条件への当てはまり | 信頼度 |
|---|---|---|---|---|
| **F1 出来高の急増** | Gervais-Kaniel-Mingelgrin（JF 2001）: 1 日〜1 週間の出来高が異常に多い（少ない）株は、次の 1 か月に上がる（下がる）。Kaniel-Ozoguz-Starks（JFE 2012）: **41 か国で確認、リスクや流動性では説明できない** | 日本の追試・最近の追試は**見つからなかった**。韓国の「低出来高プレミアム」は 2025 年の arXiv が測定上の見かけと反論（未査読）。repo の結果（急増後は逆に −0.05%、t −1.79）は論文と逆向きで、文献の中でも「例外」 | 保有期間 1 か月は 5 日より長い。TOPIX500 の大型株は「注目されない」ことによる効果が出にくい（推測） | 二次（論文の概要） |
| **T3/T4 11〜4 月** | Bouman & Jacobsen（2002）: 37 か国中 36 で冬が高い。Jacobsen & Zhang: 108 市場で長期に確認、平均差は過去 50 年で年 6.25%（本人たちの主張）。日本の差が最大 8.3% | Dichtl & Drobetz（2013・2014）: **2003 年以降は 5 つの主要指数で有意でなくなった**（CXO の要約）。一方 1998〜2012 の追試は「消えていない」（FAJ）。**2020 年代の追試と日本の追試は見つからなかった** | 年 1 回の売買でコストは問題にならない。ただし 2 つの半期の差を取る検定は 1 年 1 標本（27 標本ほど）で、検出力が低い（推測） | 二次（要約） |
| **C3 日本の割安株** | Fama-French 系は日本で強い。**Cadamuro & Iwaisako（TCER e180、2023）: 2000 年代後半からの低下は、割安株の評価が GFC 後に予想外に下がったため。予測できる部分は安定**。Tsuji（2020）: 1990〜2020 でも効果は続くが期間で強さが変わる | 米国と同様の低下。State Street（2024-03）: 日本のバリューは強いまま。SuMi Trust（2024 中）: 2020 年以降バリューが有効 | 月 5% の入れ替えで 0.10% コストなら負担は小さい。**生存者バイアスが無いフレンチのデータで測っているので、結論は変わりにくい**（repo の設計どおり） | 二次（概要） |
| **C1 米国のモメンタム** | 古典は月 1%近く。NBER の論文: **2002 年央を境に月 0.92% → 0.16%**（二次）。Swedroe（2016）: UMD は 2010〜2015 でプラス 5/6 年（二次） | **弱まった**という証拠が優勢。2016〜2025 年の追試は**見つからなかった** | 米国株は往復 0.30% のコスト。月次の入れ替え 35%で年に約 1.3% のコスト負担 | 二次（要約） |
| **日本のモメンタム（参考）** | 日本は個別株のモメンタムが効かない国として有名だが、**2023 年 3 月の東証要請の後に復活**した（State Street 2024-03）。MSCI Japan Momentum は 2024 年 +32.28% vs MSCI Japan +21.15%、2023 年は 26.19% vs 29.04%（MSCI のファクトシート）。2025 年は一貫しない（FTSE Russell） | 局面が変わった証拠。**2023 年以降だけの話なので、期間 B だけ良いものと同じ型の落とし穴に注意** | C2 を repo でもう一度測る価値はある（推測） | 二次（公式・解説） |

出典:
- Gervais ほか／Kaniel ほか: https://academicnewsletter.sufe.edu.cn/info/361520 、https://academicnewsletter.sufe.edu.cn/info/356889
- 低出来高プレミアムの反論: https://www.arxiv.org/pdf/2512.14134
- Halloween 追試: https://www.cxoadvisory.com/calendar-effects/halloween-indicator-out-of-sample-test 、https://www.bnains.org/backtest/periode/The_Halloween_Indicator_everywhere_and_all_the_time_-_Ben_Jacobsen_%26_Cherry_Y._Zhang.pdf
- Cadamuro & Iwaisako: https://ideas.repec.org/p/tcr/wpaper/e180.html
- 日本の価値効果ほか: https://ideas.repec.org/a/rfa/journl/v8y2020i4p39-50.html
- 米国モメンタムの減衰: https://www.nber.org/system/files/working_papers/w28624/revisions/w28624.rev2.pdf 、https://www.etf.com/sections/index-investor-corner/swedroe-momentum-really-dead
- 日本のモメンタム復活: https://www.ssga.com/library-content/assets/pdf/global/systematic/2024/monthly-march-2024.pdf 、https://www.msci.com/documents/10199/77cc4823-54d2-9ab2-62c7-fcecf21bf620 、https://www.lseg.com/content/dam/ftse-russell/en_us/documents/market-insights/equity-factor/equity-factor-insights-q1-2025.pdf

---

## B. まだ試していない、根拠の強い方向

### B1. 日本株のイベント（いちばん上が優先）

| イベント | 主張・数字 | 発表後に弱まったか | 私たちの条件（TOPIX500・5 日・買いだけ・往復 0.10%） | 必要なデータ・入手 | 信頼度 |
|---|---|---|---|---|---|
| **自社株買いの発表の後** | 日本の発表時の超過収益: Hitotsubashi WP「発表の 1 日前〜30 日後で +2.0%、低評価・小型株ほど大きい」。Zhang（2001/2002、東証）「発表時 約 5%、買い比率が大きく、規模・PBR・過去リターンが小さいほど大きい」。Takezawa（1997 年以降 184 件）。**長期（数か月〜数年）のドリフトを測った日本の研究は見つからなかった**。米国の古典は Ikenberry-Lakonishok-Vermaelen 1995（知識。今回は検索で未確認） | 日本の最近の追試は**見つからなかった**。2023 年の東証要請の後に自社株買いは急増（推測） | **発表日の跳ねは取れない**（TDnet は多くが場中や引け後に出る）。翌日の寄りで買って 5〜20 日持つ形になる。TOPIX500 内の件数は十分ありそう（推測、数えていない）。経済的な理由（過小評価のシグナル、需給）がある | TDnet の「自己株式の取得に係る事項の決定」。J-Quants の TDnet 追加（5 年）か、上場会社情報サービス（1 秒 1 件） | 二次（論文の概要） |
| **指数の入れ替え** | 日経 225: 2000-04 の 30 銘柄入れ替えで採用銘柄は発表後 5 日で +19%（Kobe 大の論文）、発表で動いて発表後に反転（価格圧力）、小型株ほど大きい（二次）。TOPIX の段階的な除外（2022〜2025 の 10 回）: 除外される株は一貫して負、特に小型・低流動（Harada & Nguyen 2025、TCER e217、二次）。米国: 追加の超過収益は 1990 年代 7.4% → 過去 10 年で 1% 未満（Greenwood & Sammon、JF 2025、二次） | **消えた**（米国）。日経 225 の古い数字は 2000 年の特殊な年 | 日経 225 の採用は**年 2 回・数銘柄**で、統計にならない。TOPIX の第 2 段階（2026-10-07 発表、除外予定 683 社、2026-10-30 から段階的に）は**小型株の話で TOPIX500 の外**。買いだけでは除外を空売りで取れない | 日経・JPX の発表（公開） | 二次（要約） |
| **株主優待の権利日の前** | **権利日前の値上がりを測った学術研究は見つからなかった**。Yasutake（2012、ハワイ大の博士論文）: 権利落ちで優待分は織り込み済み（二次）。日経（2020-04 以降に導入した 113 社）: 導入発表の翌日に有意な超過収益（二次）。解説記事: 優待が魅力的な株ほど権利落ちで下がる | 不明 | 優待は約 3 割の上場会社（立命館の論文）。**権利確定日のデータが要る**（IR から。公的な一括データは見つからなかった）。先回り買い（40 営業日前から上がる）は個人の間の経験則。repo の C5（日本の配当月）は発表後に消えていたので同類の疑い | 各社の優待の開示（公式データは見つからなかった） | 二次（解説）・学術は無し |
| **増配・配当の開始** | 日本の配当サプライズは株価への影響が小さい（Conroy ほか 2000）。Fukuda（2000）は配当の変更と将来の利益の関係を見ているが概要で数字は取れず。**増配後のドリフトを測った日本の研究は見つからなかった** | 不明 | 日本は決算と配当が同時に出るので、配当だけを切り出せない（二次） | 決算短信 | 二次（概要）・研究は薄い |
| **公募増資の後** | 日本の公募増資の発行体は長期に負ける（1975〜1990 の 650 件、1975〜1992 の別の研究）。2020 の見直しは「時価加重では勝つ基準もある」と結論が割れる（二次） | 標本が 1990 年代まで。**最近の追試は見つからなかった** | 買いだけでは**「避ける」にしか使えない**（空売りできない） | 開示 | 二次（概要） |
| **資本コストや株価を意識した経営の要請（2023-03〜）への対応の開示** | **開示した会社の超過収益を測った事象研究は見つからなかった**。野村系資料は「低 PBR 会社の自社株買いなど PBR 改善の期待が 2023 年前半の上昇の一因」と定性的に述べる。KPMG は「PBR 1 倍の一律の基準は適切か」「改善策が評価されずに低迷する懸念」 | – | これは「異常」ではなく**政策の追い風という局面**。局面は変わりうる | 開示（東証の一覧が公開されている。取得の可否は未確認） | 二次（解説） |
| **決算発表をまたいで持つ（発表プレミアム）** | **日本の発表プレミアムの研究は見つからなかった**（今回の検索では）。米国は Frazzini & Lamont などが報告（知識。今回は検索で未確認）。Conroy ほか（2000）は日本の株価が決算・予想に強く反応することを示す | 不明 | **repo にすでにデータがある**（決算発表日）。発表またぎの 2〜3 日の平均が他の日より高いかは、追加のデータ無しで測れる（推測）。ただしサプライズの符号に関係なく平均で見る型 | 手元のデータで可 | 推測（日本の文献が無い） |
| **信用残・空売り残高** | Lee & Ko（2014、Japan and the World Economy）: 日本の空売りは直近の一時的な下落に反応し、短期の下落を先取りし、**長期の永続的な下落は起こさない**（二次）。25 か国の日次（2006〜2010）では多くの空売り指標が将来リターンを予測するが、集中貸借市場（日本はこの型）と低流動性で弱まる（ABFER 論文、二次） | 不明 | 空売り残高（0.5% 以上）は毎営業日 17:00 ごろ、2 営業日遅れで JPX が公開（無料）。信用取引現在高は週次。J-Quants V2 にも margin-interest・short-ratio の入口がある（Standard 以上と思われるが未確認） | 無料（JPX）か Standard | 二次（公式・概要） |
| **投資部門別の売買** | 株ごとではなく**市場全体**の週次・月次の統計。月次は第 8 営業日に公開（JPX）。**予測力を測った研究は見つからなかった**（ロイターの報道は同時の動きの説明にとどまる） | – | 銘柄選びには使えない。指数の時期選択（日経平均・TOPIX の配分）なら使えるが、標本は 1 つの時系列で検出力が低い（推測） | JPX（無料）、J-Quants /equities/investor-types | 二次（公式） |

出典:
- 自社株買い（Hitotsubashi）: https://www.sba.hub.hit-u.ac.jp/assets/files/research/fs_wp/FS-2008-E-002.pdf
- Zhang（Pacific-Basin Finance Journal 2002）: https://ideas.repec.org/a/eee/pacfin/v10y2002i3p287-305.html
- 日本の自社株買い（ICU）: https://icu.repo.nii.ac.jp/record/2959/files/Share%20Buybacks%20in%20Japan.pdf
- 日経 225 の入れ替え: https://www.b.kobe-u.ac.jp/papers_files/2004_21.pdf 、https://ideas.repec.org/a/ibf/ijbfre/v6y2012i4p59-71.html
- 日経 225 の見直しルール（年 2 回・4 月と 10 月）: https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_guidebook_jp.pdf
- The Disappearing Index Effect: https://papers.ssrn.com/abstract=4294297 、https://ideas.repec.org/a/bla/jfinan/v80y2025i2p657-698.html
- TOPIX 段階除外（Harada & Nguyen）: https://ideas.repec.org/p/tcr/wpaper/e217.html
- TOPIX 見直しの規則・時程: https://www.jpx.co.jp/english/markets/indices/revisions-indices/ 、https://www.jpx.co.jp/english/markets/indices/topix/tvdivq00000030ne-att/e_cal2_30_topix_rev.pdf
- TOPIX 第 2 段階の発表（2026-10-07）: https://theedgemalaysia.com/node/820885
- 優待（Yasutake）: https://scholarspace.manoa.hawaii.edu/items/2743ff39-1781-434a-bd6f-e2ac2cfe99a8
- 優待（日経 QUICK）: https://www.nikkei.co.jp/nikkeiinfo/en/global_services/quick/shareholder-benefits-still-boosting-stock-prices.html
- 優待の解説: https://www.all-senmonka.jp/moneyizm/77372
- 配当（Fukuda 2000）: https://ideas.repec.org/a/eee/pacfin/v8y2000i1p53-66.html
- 公募増資: https://business.oregonstate.edu/faculty-and-research/research/publications-list/long-horizon-seasoned-equity-performance-pacific 、https://emerald.com/insight/content/doi/10.1108/S2514-465020200000008006/full/html
- 資本コスト要請: https://www.dir.co.jp/report/research/capital-mkt/securities/20230301_023664.html 、https://kpmg.com/jp/ja/insights/2023/11/pbr202311.html
- 空売り（Lee & Ko）: https://ideas.repec.org/a/eee/jjieco/v34y2014icp89-97.html
- 空売りの国際比較: https://www.abfer.org/media/abfer-events-2015/annual-conference/investment-finance/are-shorts-equally-informed-a-global-perspective.pdf
- JPX 空売り集計: https://www.jpx.co.jp/markets/public/short-selling/01.html
- JPX 投資部門別: https://www.jpx.co.jp/markets/statistics-equities/investor-type/02-01.html

### B2. 夜と昼のリターンの差（N2）と、往復の実コスト

結論: **0.10% が高すぎるかどうかは、外の資料では決まらない。実測が必要。** ただし分かる部分は次のとおり。

| 項目 | 分かったこと | 信頼度 |
|---|---|---|
| 夜に出る | Tsutsui（2003）、Lou-Polk-Skouras（JFE 2019、夜と昼が反対向きに続く「綱引き」。**大型・高価格の株で強い**）。日本の昼間と夜間の相互作用: Miwa（Review of Economic Dynamics 2019）は、取引時間を延ばした日本の先物で昼間の「閉場への過剰反応」が大きい。**2024・2025 年の日本株の夜と昼の分解は見つからなかった** | 二次（概要） |
| 手数料 | **SBI 証券**: ゼロ革命。報告書の電子交付など条件を満たせば、約定代金にかかわらず国内株（現物・信用・S 株）の売買手数料が 0 円（2023-09-30〜）。SBI の公式ページが条件つきと明記。**楽天証券**: ゼロコースは約定代金にかかわらず国内株の現物・信用が 0 円。**条件は R クロスと SOR の利用が必須**。2026 年の変更は見つからなかった | 二次（公式の抜粋＋解説） |
| 板寄せ | 東証は 2024-11-05 から後場が 15:30 まで。15:25 にザラ場を終え、15:25〜15:30 は注文受付（売買は成立しない）、**15:30 に板寄せで終値を決める**。想定終値が配信され、約 70% の注文が引け前の受付時間に入る（JPX の資料）。JPX の検討会は「悪意ある操作は確認していない」「受付時間中の想定値の大きな変動を和らげる改善が必要」「15:29:30〜15:30:00 の間でランダムに締める案」。**板寄せで約定した価格インパクト・売り買いの差を測った研究は見つからなかった** | 二次（公式・報道） |
| 売り買いの差 | 板寄せで成行を出すと、その回の約定価格は全員同じ。**売り買いの差は払わないが、自分の注文が決めた値段に混ざる影響はある**（推測。小口なら無視できる大きさ）。**TOPIX500 の実効スプレッド（bp）は見つからなかった**。呼値は 2027-03-01 から流動性（STR）ベースの新しい表になる（JPX 通知）。現行は TOPIX500 向けの細かい表 | 二次（公式）＋推測 |
| S 株 | repo の stocks_short.md は「SBI の S 株は 10:30〜13:30 の注文が引け値、13:30〜翌 7:00 が翌朝の寄り値で約定」と書いている。**今回の検索では確かめられなかった**（SBI の公式 S 株ページが出なかった）。S 株の売買差（スプレッド）の数字も見つからなかった。S 株は証券会社が相手方になるので**差があるはず**（推測） | 未確認 |
| 信用取引 | SBI の制度信用: 買方金利 2.80%・貸株料 1.10%（2026 年のブログの比較表、公式は未確認）。一般信用の日計り: 当日反対売買なら手数料・金利・貸株料が無料、翌日に持ち越すと買方金利 1.80%・貸株料 1.80%（SBI の公式キャンペーンページ、2025-12-12 時点）。**1 晩の金利は 2.8%÷365 ≒ 0.008%**（推測で計算）で、0.08% の損益分岐に比べて小さい。ただし逆日歩・品貸料・証拠金が要る | 二次（公式＋解説） |
| NISA での売買 | 成長投資枠は年の買付額に上限があるため、**毎日買う形は枠を使い切る**（知識。今回の検索で未確認）。短期売買は特定口座か信用になり、税は 20.315%（E 章） | 推測 |

出典:
- ゼロ革命: https://go.sbisec.co.jp/lp/zero_revolution_2023.html 、https://kabukiso.com/column/news/sbi_zero_kakumei.html
- 楽天ゼロコース: https://www.rakuten-sec.co.jp/web/domestic/stock/commission.html 、https://money.it-trend.jp/articles/investment/securities/rakuten-securities/05-0108
- 信用金利: https://go.sbisec.co.jp/lp/margin_220906.html 、https://money-lifehack.com/article/133
- 東証のクロージング・オークション: https://www.jpx.co.jp/corporate/news/news-releases/1030/jr4eth0000002b72-att/pressrelease_extension_of_trading_hours_ja.pdf 、https://media.moneyforward.com/articles/9636
- 引け後の改善案: https://www.jpx.co.jp/english/corporate/news/news-releases/1030/vk0khi00000182u2-att/vk0khi00000186me.pdf
- 呼値の見直し（2027-03-01）: https://www.jpx.co.jp/english/equities/trading/domestic/tvdivq0000008dfl-att/vk0khi000001vl0e.pdf
- Lou-Polk-Skouras: https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- Miwa 2019: https://ideas.repec.org/a/eee/reveco/v64y2019icp572-585.html

**見立て（推測）**: 手数料 0 円の口座で、寄り・引けの板寄せの成行を使えば、実際のコストは「約定価格と Yahoo の始値・終値の差」だけになるはず。Yahoo の終値は板寄せの値段なので、ほぼ同じ。したがって 0.10% は保守的で、実コストは 0.02〜0.05% かもしれない。**ただし repo の結果は「夜だけ持つ形は持ち続けに負ける」**で、コスト 0 でも持ち続けに勝てるかは別の問題。実コストの実測は安いが、それだけで N2 が採用に進むわけではない。

### B3. 商品 CFD のトレンドフォローで、まだ試していないもの

| 候補 | 主張・数字 | なぜ私たちには当てはまりにくいか | 信頼度 |
|---|---|---|---|
| **basis momentum**（Boons & Prado、JF 2019） | 期近と次の限月の「ベーシスの勢い」（期近のリターン − 2 番目の限月のリターンの動き）が、商品の現物と期間のプレミアムを予測する。需給の不均衡（投機家や仲介業者が市場を清算できない状況）と、変動のリスクへの報酬と解釈（NBER 版）。**長短の収益・シャープの数字は取れなかった** | 横断（多数の商品を順位づけ）で作る手法。4 商品では順位づけの意味が薄い。限月ごとのデータは EIA（WTI のみ、repo のキャリー検証で使用）しか無い。ブレントと金・銀の限月データの出所が無い | 二次（概要） |
| **skewness**（Fernandez-Perez, Frijns, Fuertes & Miffre、JBF 2018） | 直近 12 か月の日次リターンの歪度が小さい（負の歪み）商品を買い、大きい商品を売る。27 商品（1987〜2014）で年 8.01%、t 4.08（Quantpedia の要約。出版版は違うかもしれない） | 27 商品の五分位の長短。4 商品では作れない。**repo の universe.md/diversified.md の結論「市場を広げても CFD のコストで成り立たない」と同じ壁** | 二次（解説） |
| **ヘッジ圧力**（Basu & Miffre、JBF 2013） | CFTC の建玉（ヘッジャーと投機家）で順位づけした長短のポートフォリオ。シャープは長いだけより一貫して高い。ボラティリティが高いほど報酬が上がる（EDHEC 要約） | 横断。**COT は repo で試し済み**（cross_asset.md の cot）。本物の追加は無い | 二次（概要） |
| 商品の時系列モメンタムの最近の追試 | 2008 年以降に弱い（Georgopoulou & Wang 2016、Satchell & Grant 2020、Huang ほか 2020 の整理、2025-12 の Nova の修士論文）。Baltas & Kosowski（2017）: 資産間の連動の高まりで説明、相関で調整すると 2008 年以降も改善。**線形の個別ラグが標準 TSMOM より良く、非線形は上乗せ無し**（2025 の修士論文、未査読） | – | 二次（概要） |
| トレンドフォロー全体の最近 | SG Trend Index: 2024 年 4 月末で +14.48%。2025 年は苦戦（4 月末で −9.3%、5 月末で −11% 超）、2025 年 9 月に回復。2026 年は IASG CTA 指数が 6 月に −1.34%、年初来 +5.03%。「金属とエネルギーのトレンドが崩れた」（Institutional Investor） | 局面の参考 | 二次（解説） |

出典:
- Boons & Prado: https://ideas.repec.org/a/bla/jfinan/v74y2019i1p239-279.html 、https://conference.nber.org/conf_papers/f89296/f89296.pdf
- Fernandez-Perez ほか: https://openaccess.city.ac.uk/id/eprint/17843/1/SSRN-id2671165_July2017.pdf 、https://quantpedia.com/strategies/skewness-effect-in-commodities
- Basu & Miffre: https://ideas.repec.org:443/a/eee/jbfina/v37y2013i7p2652-2664.html 、https://climateimpact.edhec.edu/publications/capturing-risk-premium-commodity-futures
- 商品 TSMOM の最近: https://run.unl.pt/bitstreams/3a74d44e-542e-440d-83b5-b5f44247f18f/download 、https://www.cmegroup.com/education/files/demystifiing-time-series-momentum-strategies.pdf
- CTA の成績: https://www.alternativeswatch.com/2025/06/02/trend-following-hedge-funds-endure-more-pain-societe-generale-indices/ 、https://www.institutionalinvestor.com/article/ctas-suffer-june-setback-commodity-trends-unravel

**見立て（推測）**: 「4 商品・日足・MT5 で動く」という制約のもとで、論文の新しい筋は見つからなかった。唯一あり得る形は **WTI だけの basis momentum を、キャリーと同じように月次の配分に使う**もの。ただし carry.md の結論（「本物だが小さく、日々の入口の絞り込みには向かない」）と同じ運命の可能性が高い。

### B4. 暗号資産

| 項目 | 内容 | 信頼度 |
|---|---|---|
| 日足のトレンドフォローの最新 | **Zarattini, Pagani & Barbon「Catching Crypto Trends」（Swiss Finance Institute 25-80、2025-10 改訂）**: 異なる期間のドンチアン・チャネルを合成し、変動で量をそろえる。流動性上位 20 銘柄の回転で、手数料後シャープ **1.5 超**、対ビットコインの年率アルファ 10.8%。Concretum の追加（日中の長短、2018〜2025）: 手数料前でシャープ約 1.6、変動をそろえたビットコイン買い持ちは 0.8 弱 | 二次（概要） |
| 他 | arXiv 2602.11708（プレプリント）: 2022〜2024 の標本外、taker 手数料 4bp・スリッページ・永久スワップの資金調達料を入れる。適応型でシャープ 2.1〜2.4、1 か月の時系列モメンタム 0.65、買い持ち 0.17（**表の列名が食い違っているので注意**）。Detzel ほか（2018）: SMA でシャープ 2.2〜2.6、損益分岐のコスト 1.1〜4.5%（2010〜2017、粗い、二次）。為替の技術的ルールは標本内 0.66 → 標本外 0.06 に落ちた（警告として） | 二次（概要） |
| 私たちの条件 | GMOコインの取り扱い銘柄数と、レバレッジの保有コスト（repo の crypto.md は年 14.6%）。20 銘柄の回転は現物・取引所で組めるかが未確認。検証期間が 2018 年以降の強気相場を含む。**repo の記録だけの売買（判定 2027-10）が、まさにこの先のデータ確認** | – |
| 週末・時間帯 | Baur, Cahill, Godfrey & Liu: 7 つの取引所の 1,500 万件超の価格・出来高で、時間・曜日・月の効果は**時期で変わり、一貫して続くものは無い**（市場は効率的と結論）。Caporale & Plastun: ビットコインは月曜が高い。CESifo: ビットコインだけ月曜が高く、他の暗号資産は無い。UCD: 曜日の効果は無いが**週末はリスクが低い** | 二次（概要） |
| GMOコインの費用 | 取引所のレバレッジ取引は最大 2 倍（2020 年の法改正）。レバレッジ手数料は 2017 年のページで 0.05%/日（古い）。**現行の料率・板の厚さは今回見つからなかった**（公式を直接確認する必要がある） | 二次（解説、古い） |

出典:
- Catching Crypto Trends: https://abarbon.com/papers/catching-crypto-trends 、https://ideas.repec.org/p/chf/rpseri/rp2580.html 、https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/
- arXiv 2602.11708: https://arxiv.org/pdf/2602.11708
- Detzel ほか（SMA）の要約: https://www.cxoadvisory.com/currency-trading/timing-bitcoin-with-smas/
- 暗号資産の曜日・時間: https://quantpedia.com/Blog/Details/are-there-any-simple-calendar-effects-in-bitcoin-market 、https://cesifo.org/DocDL/cesifo1_wp6716.pdf 、https://researchrepository.ucd.ie/server/api/core/bitstreams/3fe12e0f-f1c4-4226-937e-b7fc034dfcd9/content
- GMOコイン: https://diamond.jp/crypto/exchange/gmocoin/

---

## C. IR を読む AI（言語モデル）の研究

### C1. 研究の地図

| 研究 | 内容 | 信頼度 |
|---|---|---|
| **Lopez-Lira & Tang**（arXiv 2304.07619、v6 2025-10） | 米国の見出し（2021-10〜2022-12）を ChatGPT で良い・悪い・無関係に採点すると翌日リターンと相関。GPT-1/2・BERT は予測できない。**LLM の普及とともに戦略の収益が下がる**（一覧サイトの要約）。シャープの数字は取れなかった | 二次（概要） |
| **Chen, Kelly & Xiu / Lopez-Lira**（repo の stocks_short.md が引用） | 効くのはその会社についてのニュース、小型株・悪いニュースで強く、使う人が増えるほど弱まる | repo の記述 |
| **先読みの検出: Gao, Jiang & Yan**（arXiv 2512.23847） | 「先読みの傾向」(LAP)の指標。Llama-3.3 で上の実験をやり直すと、**モデルの記憶が予測力を大きく増幅**。学習の締め切りより後の標本では交互作用が有意でなくなる。要約では見かけの効果の約 37%（二次の二次、要確認） | 二次（概要） |
| **Somoza, Didisheim & Fraschini**（Economics Letters 2025） | 文脈なしで過去のリターンを思い出させて記憶を測る。**小さいモデルと粒度が細かいデータでは偏りが無視できる** | 二次（概要） |
| **Benhenda「Look-Ahead-Bench」**（arXiv 2601.13770） | 時点を守るモデルと一般のモデルを比べる標準の検査 | 二次（概要） |
| **時点を守るモデル: He, Lv, Manela & Wu「Chronologically Consistent LLMs」**（arXiv 2502.21206） | ChronoBERT/ChronoGPT。その時点で存在した文章だけで学習。ニュースから翌日のリターンを予測するシャープは、大きな Llama と同等（版により書き方が違う）。先読みの偏りは「モデルと用途による」と結論。後続の arXiv 2510.11677 は命令調整版で 2007-01〜2023-07 の長短 | 二次（概要） |
| **Kim, Muhn & Nikolaev「Bloated Disclosures」**（arXiv 2306.10224） | ChatGPT の要約は原文より 70% 以上短く、株価反応をよく説明する。情報の水増し（bloat）が大きいと価格効率が低い | 二次（概要） |
| 米国の最近のプレプリント | 決算説明会 168,139 件を時点を守る LLM で 1 つの信号にして、月 1.73% の長短・シャープ 2.26（Substack の紹介。原論文を特定できず、**未確認**）。10-K の MD&A（255 社の S&P500）を FinBERT で埋め込み、長短のシャープ 0.86（arXiv 2606.xxxx、プレプリント） | 二次（解説）・未確認 |
| **日本: Okada, Nakasuji, Tsukioka & Yamasaki「From words to returns」**（PeerJ Computer Science 2025-11-18） | 東証の全上場会社の有価証券報告書 2014〜2023 年（11,135 社年・7,000 万語超）を ChatGPT・Claude・Gemini で採点。**辞書の方法は無関係、LLM の感情は将来リターンと有意な負の相関**（良く書いた会社は過大評価）。JSAI 2025 の続報: **TOPIX100/500 に絞ると LLM の感情は予測し、アルファが大きい** | 二次（概要） |
| 日本: 松田・津田（同志社大紀要 2023） | 決算短信の文を SBERT でベクトルにして株価変動を予測。先行研究: 白方（2018）は極性値だけでは予測が難しい、山本ほか（2020）は肯定語・否定語の点数で有意な正のスプレッド | 二次（概要） |
| 日本: Tsuji（慶應 IES） | 決算短信の「経営成績に関する分析」は数字と独立で、数字の外の情報を足しうる（環境認識の文だけを数値化） | 二次（概要） |
| JPX のワーキングペーパー | **決算短信の文章を扱うものは見つからなかった**（一覧にあったのは TCFD 開示のゼロショット分類 Vol.43 など） | 二次（公式） |
| 人工知能学会の金融情報学研究会 | 検索では直接の資料に届かなかった（言語処理学会 2025 の E5-2 などは出た） | – |

出典:
- Lopez-Lira & Tang: https://arxiv.org/abs/2304.07619v5
- Gao ほか: https://arxiv.org/pdf/2512.23847 、https://arxiv.org/html/2512.23847v1
- Somoza ほか: https://faculty.essec.edu/research/16148-ais-predictable-memory-in-financial-analysis
- Look-Ahead-Bench: https://arxiv.org/pdf/2601.13770
- Chronologically Consistent LLMs: https://arxiv.org/html/2502.21206v2 、https://arxiv.org/pdf/2510.11677
- Bloated Disclosures: https://arxiv.org/html/2306.10224v1
- From Text to Alpha: https://arxiv.org/pdf/2510.03195
- Substack の紹介: https://substack.com/@quantitativo/note/c-253874660
- Okada ほか: https://peerj.com/articles/cs-3349 、https://www.kwansei.ac.jp/research-activity/news/en/01626.html 、https://www.anlp.jp/proceedings/annual_meeting/2025/pdf_dir/E5-2.pdf
- 同志社: https://doshisha.repo.nii.ac.jp/record/2000182/files/023064040005.pdf
- 慶應 IES: https://ies.keio.ac.jp/upload/41fdbf88079d8ac20278f6472cb9b43a.pdf
- JPX WP 一覧: https://www.jpx.co.jp/english/corporate/research-study/working-paper/index.html

### C2. 先読み（学習データに未来が入る問題）と対処

- 問題: LLM は学習の時点までの出来事を記憶している。2014〜2023 年の日本企業の文章を採点して同じ期間のリターンと比べると、**モデルが結果を覚えている分だけ「予測できた」ように見える**（Gao ほか、Lopez-Lira 系の追試）。Okada ほかは ChatGPT・Claude・Gemini を使い、標本期間が学習期間と重なる。**この研究の負の相関は、先読みの混入を疑って読むべき**（推測。概要欄だけで、論文が先読みを検査しているかは見ていない）。
- 対処は 3 つ見つかった。
  1. **学習の締め切りより後だけで測る**（Gao ほか）。モデルごとに締め切りが決まっているので、2026 年 10 月から見て締め切り後の標本は短く、日本株の 1 年分に届かないことが多い。
  2. **時点を守る小さなモデル**（He ほか）。ChronoBERT などは英語。日本語の同等品は**見つからなかった**。日本語の BERT（2019〜2020 年版など、学習が古いもの）を埋め込みに使い、それ以降の期間だけで測れば同じ効果が得られる（推測）。
  3. **小さいモデルと細かいデータは偏りが小さい**（Somoza ほか）。
- 記憶の検査（Gao ほかの LAP）を入れる。

### C3. 題名の埋め込み（A1）が効かなかったことと、何なら効く見込みがあるか

- **効かなかったのは自然**（推測）。適時開示の題名は「業績予想の修正に関するお知らせ」「2026 年 3 月期第 2 四半期決算短信〔日本基準〕(連結)」のように定型で、**文字として良し悪しの情報がほとんど無い**。Chen ほか・Lopez-Lira の成功は「見出しが内容を述べる」英語のニュース。
- 効く見込みがある順（推測。文献の裏づけは弱い）:
  1. **E1/E2 の修正理由の文章**（「為替の影響」「一過性の特別利益」「受注の増加」など）。数字が同じでも、理由が本業か一過性かで持続性が違う。数字では見分けられない情報。Tsuji の「経営成績の分析は数字と独立」が裏づけの方向。
  2. **構造化した抽出（理由の分類、数字の根拠の有無）**。感情（良い・悪い）の採点より、事実の抽出のほうが記憶の影響を受けにくく、検証しやすい（推測）。
  3. **前回の決算短信との差分**（変化の検出）。arXiv 2510.03195「From Text to Alpha」の関心と同じ方向（概要のみ）。
  4. 学習の締め切り前後で分けた評価（C2）。
- 効く見込みが薄いもの: 題名の埋め込み・辞書の感情・全社の一括採点（Okada ほかの辞書は無関係）。
- **repo のルールとの兼ね合い**: TDnet の公開サイトから自動で取らない決まりがある。J-Quants の TDnet 追加（D 章）は、公式の配信で過去 5 年分。E1 の 22,310 件はすでに上場会社情報サービスから取得済みで、文章は取っていなければ別途取得が要る。

---

## D. データの出所と費用・利用規約

### D1. J-Quants API（個人向け、JPX 総研）

| プラン | 月額（税込） | 履歴 | 1 分あたり回数 | 主な中身 |
|---|---|---|---|---|
| Free | 0 円 | 直近 12 週間を除く 2 年分 | 5 | 上場銘柄一覧、株価（四本値）、財務情報（サマリーのみ） |
| Light | 1,650 円 | 5 年 | 60 | TOPIX 四本値などを追加。**アドオンが付けられる** |
| Standard | 3,300 円 | 10 年 | 120 | 指数、信用取引データ、日経 225 オプション |
| Premium | 16,500 円 | 20 年（株価は 2008-05-07 以降） | 500 | 前場速報、財務諸表、配当金、売買内訳、先物・オプション |

- **2025-12: V2 が始まった**（API キー方式。V1 のトークンは廃止）。
- **2026-01-19: CSV の一括配信**（Light 以上）。**分足・ティックのデータ**（株式のみ、先物は除く）が追加（月 5,500 円のアドオン、Light 以上）。
- **2026-05-18: TDnet 開示文書のアドオン**。**月 11,000 円（税込）、Light 以上、API と CSV 一括、過去 5 年分、個人投資家のみで法人・学術目的は不可。**
- **2026-07-27: J-Quants Pro**（法人向け）に「大量保有報告（EDINET）」「大株主」「政策保有」のデータ（Ownership Data）。**個人向け API にはこの追加は見つからなかった**。
- V2 の入口に `/equities/bars/daily`、`/fins/summary`、`/equities/investor-types`、`/markets/margin-interest`（信用取引週末残高）、`/markets/short-ratio`（業種別空売り比率）、`/markets/breakdown`（売買内訳）がある。**プランごとの対応表は今回取れなかった。**
- **規約・使い方（公式ヘルプ）**: 個人の**私的利用に限る**（法人は営利・非営利を問わず不可）。**取得したデータそのものを閲覧できる形で配布・共有することは禁止**。**自分の分析結果や分析手法を公開するのは構わない**。データを使ったアプリを他人に提供するのは不可。運営者のサーバーに蓄積・中継する構成も私的利用に当たらない。**自動売買に関する明記は見つからなかった**（確かめたいなら j-quants@jpx.co.jp に問い合わせ。ただし問い合わせは外部への連絡なので、指令役の判断が要る）。
- **この repo は公開なので、データの CSV をコミットしてはいけない**（既存の約束と一致）。

信頼度: 二次（公式）。

出典:
- 料金・履歴: https://jpx-jquants.com/en/spec/data-spec 、https://jpx-jquants.com/ja/help/about
- レート制限: https://jpx-jquants.com/spec/rate-limits
- V1 → V2、入口一覧: https://jpx-jquants.com/en/spec/migration-v1-v2 、https://jpx-jquants.com/en/spec/bulk-list/endpoints
- 2026-01-19 の発表: https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html
- 2026-05-18 の TDnet アドオン: https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260518-01.html
- 2026-07-27 の Ownership Data: https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260727-01.html
- 利用規約・ヘルプ: https://jpx-jquants.com/termsofservice 、https://jpx-jquants.com/help/usage

### D2. EDINET API

- v2。金融庁の EDINET にメールで登録し、連絡先を入れるとキーが表示される。2023-08 に新 API（キー認証）が始まった。
- XBRL の原本 ZIP（type=1）、PDF（2）、英訳（4、一部）、CSV 変換済み（5、UTF-16・タブ区切り）が取れる。
- 規約: 機能の仕様を予告なく変更でき、負荷に応じてアクセスを制限することがある。**公式のレート制限は明示されていない**。民間の慣例では連続リクエストの間に 3〜5 秒。商用の可否は民間サイトが「可能」と書くが、**再配布を含む正式な条件は取れなかった**。
- 信頼度: 二次（公式の説明会資料の抜粋＋民間解説）。

出典: https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html 、https://zenn.dev/akb428/articles/7c1c05d3cc1e21 、https://edinetdb.jp/blog/edinet-system-overview

### D3. JPX の公開データ

| データ | 分かったこと | 信頼度 |
|---|---|---|
| 投資部門別売買状況 | 株式・ETF・REIT・海外投資家の地域別。週間・月間・年間。**月間は毎月第 8 営業日の 15:30 ごろ掲載**。週間の公開日は取れなかった | 二次（公式） |
| 信用取引現在高 | 過去推移表あり。掲載は毎週水曜 15:00 または火曜 16:00 の 2 通りの記載があり、どちらが現行か要確認 | 二次（公式） |
| 信用取引売買比率 | 毎日 16:30 ごろ | 二次（公式） |
| 空売り残高 | 残高割合 0.5% 以上を掲載（報告は 0.2% 以上）。**毎営業日 17:00 ごろ。取引日の 2 営業日後**に銘柄別。市場全体の空売り比率は翌営業日夕方 | 二次（公式＋解説） |
| 指数の構成銘柄の過去 | **過去の時系列は見つからなかった**。公式ファクトシートは特定時点のスナップショット（2025-10-31 時点 1,673 銘柄）。TOPIX500 の呼値用の構成は月次の通知で公開（JPX の 2026-01、2026-07 の通知）。**無料で一括の履歴は見つからなかった**。有料の端末（Bloomberg、日経 NEEDS、QUICK）が一般的だが今回は未確認。index@jpx.co.jp に問い合わせ先がある | 二次（公式）／推測 |
| CSV・Excel で無料ダウンロードできるか | **確かめられなかった**（検索では出なかった）。Mac から JPX のページを開いて確認する必要がある | – |
| 自動で取ってよいか | JPX の統計ページの利用規約は今回確認できていない | 未確認 |

**生存者バイアスを減らす案（推測）**: J-Quants の上場銘柄一覧は日付を指定できるはずで、その時点の規模区分（TOPIX Core30/Large70/Mid400）が付いていれば、**時点ごとの TOPIX500 を再構成できる**。Premium（20 年・月 16,500 円）を 1 か月だけ契約して、全期間の株価・財務・上場銘柄一覧を取得し、解約する案。**この機能は今回確認できていない。Free（2 年・12 週遅れ）で、日付指定の上場銘柄一覧に規模区分が付くか、先に無料で確かめられる**。

出典:
- 投資部門別: https://www.jpx.co.jp/markets/statistics-equities/investor-type/02-01.html
- 信用: https://www.jpx.co.jp/markets/statistics-equities/margin/04.html
- 空売り: https://www.jpx.co.jp/markets/public/short-selling/01.html
- TOPIX のファクトシート・規則: https://www.jpx.co.jp/markets/indices/topix/tvdivq00000030ne-att/cal2_30_topix_rev.pdf
- 構成銘柄の通知: https://www.jpx.co.jp/english/news/1030/20260123-01.html 、https://www.jpx.co.jp/english/news/1030/20260717-02.html

### D4. Kenneth French の日本のデータ

- 日本の要因・ポートフォリオが先進国のデータとしてある。サイズ × 割安（25 組）は 1990-07〜2026-05（キャッシュの版）。サイズ × モメンタムは 2025-02・03 まで（キャッシュの版で食い違うので、**現行のページの終点は直接確認が必要**）。5 要因（サイズ・割安・収益性・投資）は地域ごとの区切りで作られる。
- 日本のデータはドル建て（repo の記述と一致）。つぶれた会社も含む（生存者バイアスが無い）。
- 取得: このクラウドからは 403。Mac では取れる（repo の stocks.md が実績あり）。
- 個人が自動で取ってよいか: 学術用に公開されたデータで、利用規約は今回確認していない。

信頼度: 二次（公式ページの抜粋）。

出典: https://mba.tuck.dartmouth.edu/pages/Faculty/ken.french/Data_Library/f-f_5developed.html 、https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/tw_5_ports_developed.html 、https://mba.tuck.dartmouth.edu/pages/Faculty/Ken.French/Data_Library/tw_5_ports_momentum_developed.html

---

## E. 現実的な期待値

| 項目 | 内容 | 信頼度 |
|---|---|---|
| 個人の短期売買の成績 | Chague, De-Losso & Giovannetti「Day Trading for a Living?」（ブラジルの指数先物、2013〜2015 開始の個人、約 19,646 人）: **300 日以上続けた人の 97% が損**。銀行員の日給（約 54 ドル）を超えたのは 0.4%（CXO の要約は 0.5%）。学習した形跡は無い。Barber-Lee-Liu-Odean（台湾）: 個人の約 1% が日中取引の 50% を占める。**過去に利益があった人は 1.6% で取引量の 12%、損の履歴がある人は 24% で取引量の 54%**。個人の集計では年 −3.8 ポイント、機関は +1.5 ポイント | 二次（概要） |
| 日本の個人の売買のコスト後の成績 | **見つからなかった**（金融庁・JPX・JSDA の統計は探したが成績に届かなかった） | – |
| アクティブ運用 | SPIVA Japan 年末 2025: 5 区分のうち日本の大型株以外は 80% 超が基準に負けた。**日本の大型株は 2025 年は 49% が負け（2017 年以来の低さ）だが、10 年・15 年では 80% 超が負け** | 二次（公式） |
| 指数投資の費用 | eMAXIS Slim 全世界株式（オルカン）の信託報酬は年 0.05775%（2026 年 9〜10 月の販売サイト、目論見書で要確認）。TOPIX 連動の最安は今回比較していない | 二次（解説） |
| NISA | 新 NISA の枠（年 360 万・生涯 1,800 万・成長 240 万）は**今回の検索では取れなかった**（私の知識で記す。要確認）。2026 年度税制改正で **2027-01 からつみたて投資枠の年齢制限をなくす（18 歳未満: 年 60 万・総額 600 万、12 歳以降で払い出し可）、対象を株式または公社債に主に投資する投資信託に広げる**（金融庁資料・三菱 UFJ のレポート、二次） | 二次（公式）／推測 |
| 株の税金 | 上場株式の譲渡益・配当は 20.315%（所得税 15% + 復興特別所得税 0.315% + 住民税 5%。この数字は検索には出ず、私の知識）。**譲渡損失は翌年以降 3 年、譲渡益と配当から控除。源泉徴収あり特定口座でも、繰越控除のためには確定申告が要る。取引の無い年も申告を続ける。給与所得とは通算できない** | 二次（国税庁・証券会社） |
| 商品 CFD の税金 | 店頭 CFD は「先物取引に係る雑所得等」の申告分離課税 20.315%（2012 年以降、店頭と取引所の税制が一本化）。**同じ先物の区分の他の取引とは通算でき、株・投信とは通算できない**。損失は翌年以降 3 年繰越（連続して申告が必要）。含み益は課税されない | 二次（証券会社の案内）。フィリップ証券の案内そのものは未確認 |
| 暗号資産の税金 | 現在は雑所得（総合課税）。**2026-03-31 に所得税法等の改正が成立。申告分離課税 20.315%（国内登録業者の「特定暗号資産」）、損失の 3 年繰越が見込まれる。金商法の改正の施行（2027 年見込み）の翌年 1 月 1 日から＝2028 年以降**。ある記事は 7 月 15 日に参院で成立と書くが一次で未確認。海外の取引所や DEX は総合課税のまま | 二次（解説） |

出典:
- Chague ほか: https://econpapers.repec.org/RePEc:fgv:eesptd:525 、https://www.cxoadvisory.com/investing-expertise/day-trading-a-bust/
- Barber ほか: https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf 、https://ideas.repec.org/a/oup/rfinst/v22y2009i2p609-632.html
- SPIVA Japan: https://www.spglobal.com/spdji/en/documents/spiva/spiva-japan-year-end-2025.pdf
- オルカン: https://fund.monex.co.jp/detail-ideco/0331418A/1000?type=list
- 2027 のつみたて投資枠: https://www.fsa.go.jp/news/r7/sonota/20251226-2/01.pdf 、https://www.sc.mufg.jp/market/mnl/pdf/mnl_financialplan/mnl_financialplan_202603.pdf
- 譲渡損失の繰越: https://www.nta.go.jp/taxes/shiraberu/shinkoku/tebiki/2023/pdf/Q/Q5.pdf 、https://faq.sbisec.co.jp/answer/5efd99684a6766001122d28a/
- CFD の税: https://www.rakuten-sec.co.jp/web/cfd/tax_returns.html 、https://kabu.com/item/365cfd/tax.html
- 暗号資産: https://www.dir.co.jp/report/research/law-research/tax/20260206_025575.html 、https://coinpost.jp/crypto/separate-taxation/

---

## 指令役への提案: 次に深く調べる価値が高いもの 5 つ

1. **自社株買いの発表後（B1）を、E1 と同じ型で事前登録する案を作る。** 根拠: 日本で発表時の超過収益が +2〜5%（二次）、TOPIX500 内に件数が十分ありそう、経済的な理由がある。調べること: Hitotsubashi WP と Zhang を原典で読み、**長期のドリフトを測った日本の研究が本当に無いか**をもう一度探す。件数（TOPIX500 内、年あたり）と、発表の時刻（引け後なら翌日の寄りで買える）を repo のデータで数える。発表時の跳ねが取れないなら、効果の大半は取れない前提で書く。
2. **E1 の補強: 日本の経営者予想の文献を原典で読み、修正理由で分ける設計を作る。** 読む順: Kitagawa & Shuto（JBFA 2024）、Kato-Skinner-Kunimura、Caton ほか、Pan 2019、Jinushi 2023。決める点: (a) 決算と同時の修正か期中の修正か、(b) 修正の理由（本業か為替・一過性か）、(c) 個人持ち分の多寡（Jinushi の「消えない」領域から外れるのでドリフトは小さい見込み）。**Mac でできる**。理由の文章を読むのが C3 の「効く見込みが最も高い」使い方でもある。
3. **生存者バイアスの除去: J-Quants の上場銘柄一覧が時点指定で規模区分を返すかを、Free プランで確かめる。** 返すなら Premium を 1 か月（16,500 円）契約して 20 年分を取る。M2・F1・C3・E1 の「今の TOPIX500」という弱点（review.md で「プラスの差は割り引く」と書いた部分）を直接解消する。規約は個人の私的利用なら可、データの再配布は不可。**最も費用対効果が高いと見る。**
4. **夜と昼の実コスト（N2）の実測計画。** 少額で数回、引け成行で買い翌寄り成行で売り、約定価格と Yahoo の終値・始値の差を測る。SBI の S 株の約定規則を公式で確かめる。ただし repo の結果は「夜だけ持つ形は持ち続けに負ける」で、**コストが 0.03% でも N2 が採用に進むとは限らない**ことを先に書く。実測は安いが、優先は 3 より低い。
5. **IR 読解の「先読みを避けた」再設計（C2・C3）。** (a) 古い日本語 BERT で、それ以降の期間だけで測る、(b) 修正理由・決算短信の「経営成績の分析」の差分、を 1 本ずつ事前登録する。Okada ほか 2025（PeerJ CS）を原典で読み、**先読みの検査をしているか**を確かめるのが先。題名の埋め込みの再挑戦はしない。
   - 次点: 信用残・空売り残高（B1）。データが無料（JPX の空売り残高）か Standard（月 3,300 円）で手に入り、日本の集中貸借市場で予測力が弱まるという文献もあるので、優先度は上の 5 つより下。

**やらなくてよいと見るもの**: 指数の入れ替え（日経 225 は標本が足りない、TOPIX の見直しは TOPIX500 の外）、優待の権利日の前（学術研究が無い、データの出所が無い）、増配（同時の決算に埋もれる）、公募増資（買いだけでは使えない）、商品の basis momentum・skewness・ヘッジ圧力（4 商品では作れない、COT は試し済み）。

## Mac での確認（原典を読む順）

このクラウドから原典に届かなかったので、次の順で Mac から読むと、上の「二次」を「確認済み」に変えられる。
1. J-Quants の公式ヘルプと規約（https://jpx-jquants.com/help/usage 、 https://jpx-jquants.com/termsofservice ）と、データ仕様のページ（ https://jpx-jquants.com/en/spec/data-spec ）。上場銘柄一覧の日付指定と規模区分の有無。
2. Kitagawa & Shuto、Kato-Skinner-Kunimura、Jinushi 2023（J-STAGE は無料）。
3. Okada ほか（PeerJ、無料）、Gao ほか（arXiv）、He ほか（arXiv）。
4. Hitotsubashi の自社株買い WP（無料の PDF）。
5. JPX の板寄せの規則と SBI の S 株のページ。
