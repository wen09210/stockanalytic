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
| substrate | Pale Beige `#F5F1E8` | `colors.json` → 適用 tactile / archive 類主題 |
| mode | complementary duotone | 型錄對此 palette 的定義 |
| palette | Botanical Green `#008A4B` + Oxblood `#8F3434` | `palette_botanical_oxblood` |
| plate roles | Oxblood 為主版（內文／表格／格線，內文用 `#4A1F1F` 高濃度）；綠墨為輔版（跌、情緒悲觀段），佔比 15%–30% | 兩塊印版各有職責，輔版不作純裝飾 |
| layout | ruled information poster | `compositions.json`，本報告以事實與數據為主 |
| type | Programmatic | `typography.json`，數字與日期可作為錨點、表格數字對齊 |
| empty paper | 35% | skill 預設值 |
| manual gesture | 1（僅細格線構成的 metadata band） | `manual_gesture_limit: 1` |

### 墨色與台股慣例

這組墨是**唯一能同時容納台股「紅漲綠跌」的雙墨組合**：漲＝Oxblood、跌＝Botanical Green。

綠墨實際用 `#00753F`（比型錄的 `#008A4B` 略濃）。原因是型錄值在米色紙上只有 3.93:1，
小字不過 AA；提濃到 5.15:1 才夠。**不再更濃**是因為綠墨一旦壓深，明度就會與 Oxblood
重疊——實測 `#006B3A` 會讓漲跌對比從 1.75:1 掉到 1.17:1，兩種狀態幾乎分不出來。

紅與綠本來就難靠明度分辨（這也是先前採 Charcoal + Signal Red 時對比較好的原因），
所以漲跌另有 ▲▼ 箭頭與正負號作為次要編碼，不單靠顏色傳達。

實測對比（皆通過 AA 4.5:1）：

| 元素 | 對比 |
|---|---|
| 內文 Oxblood 高濃度 `#4A1F1F` on 紙 | 12.36:1 |
| 漲 Oxblood `#8F3434` on 紙 | 6.88:1 |
| 跌 綠墨 `#00753F` on 紙 | 5.15:1 |
| 情緒條紙色字 on 漲／跌 | 6.88:1／5.15:1 |

文字雲因為是大字，綠墨可直接用型錄原值 `#008A4B`（大字不受小字門檻限制）。
