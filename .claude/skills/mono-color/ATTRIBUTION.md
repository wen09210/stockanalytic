# mono-color skill — 來源與授權

本目錄是外部 skill 的原樣複製，用來定義本專案報告的視覺語言。

- **來源**：https://github.com/yanliudesign/mono-color-skill
- **版本**：commit `de607fe`
- **作者**：Yan Liu
- **授權**：MIT（見 `LICENSE`）

## 收錄範圍

只收錄 MIT 授權的部分：`SKILL.md` 與 `design-system/` 的機器可讀型錄。

**刻意未收錄** 上游的 `examples/` 目錄：依上游 `ASSET-LICENSE.md`，那些視覺範例為
「保留所有權利」，未經作者書面同意不得重製或散布。需要看範例請直接前往原 repo。

## 本專案怎麼用它

原 skill 的預設交付物是「生成一張海報點陣圖」，需要影像生成能力。本專案沒有要產生
海報，而是把它的設計系統（基材、雙墨限制、版面家族、字級關係、留白比例）轉譯成
報告頁面的 CSS。實際套用的配方寫在 `ptt_stock_wordcloud.py` 的樣式區塊註解裡，
摘要如下：

| 欄位 | 解析結果 | 依據 |
|---|---|---|
| substrate | Cool Gray `#E9E9E5` | `colors.json` → 適用 architecture / technology / charcoal-led systems |
| mode | chromatic + black | 長文與精密標籤交給碳墨，符合 long-form 主題 |
| palette | Charcoal `#30343A` + Signal Red `#C83232` | `palette_charcoal_signal_red`，型錄明列適用 **reports** |
| plate roles | 碳墨＝所有內文／表格／格線；紅墨＝重點數字與強調 | 兩塊印版各有職責，紅墨不作純裝飾 |
| layout | ruled information poster | `compositions.json`，本報告以事實與數據為主 |
| type | Programmatic | `typography.json`，數字與日期可作為錨點、表格數字對齊 |
| empty paper | 35% | skill 預設值 |
| manual gesture | 1（僅細格線構成的 metadata band） | `manual_gesture_limit: 1` |

### 一個必須處理的衝突

報告原本用台股慣例「紅漲綠跌」，但這個設計系統限制最多兩塊印版，綠色會變成第三個墨。
解法是：**漲＝Signal Red（保留紅漲慣例）、跌＝Charcoal**。

這除了符合墨數限制，兩種狀態的可辨識度也變好：漲跌兩色之間的對比從原本紅綠的
1.47:1 提升到 2.36:1。原本的紅綠對比不僅偏低，兩色又主要靠色相區分，對紅綠色盲
最不友善；改成紅 vs 碳黑後主要靠明度區分，這是色覺缺陷者仍保留的辨識管道。
（漲跌另有 ▲▼ 箭頭與文字標示作為次要編碼，不單靠顏色傳達。）
