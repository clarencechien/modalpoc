# Google Maps Platform 開通與費用手冊（瑞光路 186 號地景專案）

更新日期：2026-09-15。價格取自 Google Maps Platform 定價頁（2025-03-01 起的新制：**每個 SKU 各自有每月免費額度，舊的每月 US$200 抵免已於 2025-02-28 結束**）。請以 https://developers.google.com/maps/billing-and-pricing/pricing 為準。

## 0. 目前這把 key 的狀態（實測 2026-09-15）

用 `google_map` 這把 key 直接打各 API 的結果：

| API | 結果 | 意義 |
|---|---|---|
| Map Tiles API `GET /v1/3dtiles/root.json` | 403 `API_KEY_SERVICE_BLOCKED` / `Map Tiles API has not been used in project 1000444854860 … or it is disabled` | **未啟用** |
| Map Tiles API `POST /v1/createSession`（2D 衛星圖） | 403 同上 | 未啟用 |
| Geocoding API | `REQUEST_DENIED: This API is not activated on your API project` | 未啟用 |
| Elevation API | 同上 | 未啟用 |
| Static Maps API | 403 | 未啟用 |
| Places API（legacy） | `legacy API … not enabled` | 未啟用 |

GCP 專案編號：`1000444854860`。也就是說這把 key 目前什麼 Maps 服務都不能用，需要到 Cloud Console 開通。

### 0.1 開通 Map Tiles API 之後的實測（2026-09-15，同日稍晚）

你開通後 `root.json` 回 200。用 `pipeline/fetch_google3d.py` 以台達總部為中心、半徑 70 m 追到最細一層：

| 項目 | 實測 |
|---|---|
| 最細 tile 的 geometricError | **2.01 m**（再往下沒有子節點） |
| 每片 tile | 約 38 m 見方、**75 個頂點 / 132 個三角形**、一張 15 KB JPEG |
| 涵蓋範圍 92 片 tile 合計 | 1.4 MB、4,820 個頂點 |
| 內容 | **只有衛星影像貼在地形上的 2.5D 面**，沒有建物體塊（見 `build/out_g3d/preview_*.png`） |
| 計費 | 每次追蹤 1 個 root 請求；本次診斷共用掉 5 次（免費額度 1,000） |

**結論：Google 在內湖瑞光路這一帶沒有提供攝影測量的 3D 建物網格**，Photorealistic 3D Tiles 在此只是貼圖地形。
所以「台達總部高精度」無法靠 Google 3D Tiles 達成；本專案改用 **程序化 hero building**（`pipeline/hero.py`：
OSM 弧形輪廓 + 9 層樓板/帷幕玻璃/女兒牆/屋頂太陽能板 266 片/機房/綠屋頂/廣場圓亭），其餘建物走 LOD1 遊戲風格。
若要更真實的台達幾何，可行來源是國土測繪中心的 LOD2 三維建物（第 3 節）或自行無人機攝影測量。

## 1. 必須開通的 API（只有一個是必要的）

| API | 用途 | 必要性 |
|---|---|---|
| **Map Tiles API**（含 Photorealistic 3D Tiles） | 真實攝影測量的建物/地形（`web/live3dtiles.html`、`--mode google3d`） | **必要**，這是唯一能達到「高仿」的資料來源 |
| Geocoding API | 地址 → 座標 | 選用。本專案已用 OSM 資料定出台達總部座標 (25.0741, 121.5777)，不需要 |
| Static Maps API / 2D Map Tiles | 衛星底圖 | 不需要。地面貼圖改用 **內政部國土測繪中心 NLSC 2024 正射影像**（政府資料開放授權，免費、解析度約 0.13 m/px，比 Google 靜態圖更適合當貼圖） |
| Elevation API | 地形高程 | 不需要（內湖此區塊近乎平地；3D Tiles 本身含地形） |

### 開通步驟（Cloud Console）

1. https://console.cloud.google.com/apis/library?project=1000444854860 → 搜尋 **Map Tiles API** → **啟用**。
   直接連結：https://console.developers.google.com/apis/api/tile.googleapis.com/overview?project=1000444854860
2. 專案必須**綁定計費帳戶**（Maps Platform 所有 API 都要求，即使用量在免費額度內）。
   Billing → Link a billing account。沒有綁定時所有請求都會 403。
3. **憑證 → 該 API 金鑰 → 編輯**：
   - **API 限制**：選「限制金鑰」，勾選 *Map Tiles API*（若之後要用 Geocoding 再加）。
     這正是目前 `API_KEY_SERVICE_BLOCKED` 的來源之一：key 的 API 限制清單裡沒有 Map Tiles API。
   - **應用程式限制**：
     - 給網頁串流（`web/live3dtiles.html`）用：**HTTP 參照網址**，填 `http://localhost:*/*`、你的網域 `https://example.com/*`。
     - 給 Modal / 伺服器端抓取（`--mode google3d`）用：**建議另建一把 key**，用「IP 位址」限制或不限制但只放在 Modal Secret 裡，不要放進網頁。
4. 等 1–5 分鐘讓設定生效，驗證：
   ```bash
   curl -sS "https://tile.googleapis.com/v1/3dtiles/root.json?key=$google_map" | head -c 300
   # 成功會回 {"asset":{"version":"1.1"...},"geometricError":...,"root":{...}}
   ```
5. 給 Modal 用：
   ```bash
   modal secret create google-maps-key GOOGLE_MAPS_API_KEY=AIza...
   ```
   （`pipeline/modal_app.py` 在 `--mode google3d` 時讀取 Modal Secret `google-maps-key`。）

### 配額（Quotas）建議

在 *APIs & Services → Map Tiles API → Quotas* 把 **root tileset requests per day** 設個上限（例如 100/天），
就算 key 外洩也不會被刷爆帳單。

## 2. 費用試算

### SKU：Map Tiles API — Photorealistic 3D Tiles（Enterprise 級）

| 每月用量（root tileset 請求數） | 每 1,000 次價格 |
|---|---|
| 0 – 1,000 | **US$0（免費額度）** |
| 1,001 – 100,000 | US$6.00 |
| 100,001 – 500,000 | US$5.10 |
| 500,001 – 1,000,000 | US$4.20 |
| 1,000,001 – 5,000,000 | US$3.30 |
| 5,000,000 以上 | US$2.40 |

**計費事件 = 一次 `root.json` 請求。** 之後 renderer 追下去抓的子 tileset、`.glb` 磁磚**都不另外計費**，
同一個 root session 可以連續抓至少 3 小時，超過要重新請求一次 root。

#### 本專案的用量

| 情境 | 每次動作的計費事件 | 每月次數估計 | 費用 |
|---|---|---|---|
| `modal run … --mode google3d`（把街區磁磚抓下來給 Blender） | 1（`fetch_google3d.py` 只請求 1 次 root，其餘走 session） | 建模迭代 10–50 次 | **US$0**（< 1,000） |
| `web/live3dtiles.html` 串流檢視 | 1 / 每次開頁（每個瀏覽器分頁一次；超過 3 小時再 +1） | 自用/內部展示 < 1,000 次 | **US$0** |
| 對外公開展示頁 | 同上 | 5,000 次開頁 | (5,000 − 1,000) × 6/1000 = **US$24** |
| | | 20,000 次開頁 | 19,000 × 6/1000 = **US$114** |

結論：**只要每月 root 請求 < 1,000 次，這個專案完全不會產生 Google 費用。**
Blender bake 路線每次建模只花 1 個事件；真正會累積的是公開網頁的每次載入。
若要公開，把 `live3dtiles.html` 改成「按鈕才載入」（已是預設）並在 Console 設每日配額。

### 其他 SKU（僅供對照，本專案不會用）

| SKU | 級別 | 每月免費 | 超過後 |
|---|---|---|---|
| Map Tiles API — 2D Map Tiles | Essentials | 100,000 | US$0.60 / 1,000 |
| Static Maps | Essentials | 10,000 | US$2.00 / 1,000 |
| Geocoding | Essentials | 10,000 | US$5.00 / 1,000 |
| Elevation | Pro | 5,000 | US$5.00 / 1,000 |

### Modal 算力費用（參考 https://modal.com/pricing）

| 資源 | 約略單價 | 本專案一次建模（含 bake 4096² × 2、96 spp） |
|---|---|---|
| L4（預設） | ≈ US$0.80 / GPU-hr | **實測 135 秒**（hero/建物/地面各一張 4096²、128 spp、OptiX）≈ **US$0.03** |
| A10G | ≈ US$1.10 / GPU-hr | 約 3–6 分鐘 |
| L40S | ≈ US$1.95 / GPU-hr | 約 2–4 分鐘 |
| CPU 4 核 | ≈ US$0.135 / core-hr | 首次建 image（下載 Blender 4.2 tarball）約 2–3 分鐘，之後快取 |

Modal 新帳號每月有免費額度（目前 US$30/月），這個專案的用量遠低於此。

## 3. 條款注意事項（Map Tiles API Policies）

摘自 https://developers.google.com/maps/documentation/tile/policies ：

- 「you must not pre-fetch, index, store, or cache any Content except under the limited conditions stated」（只允許依 HTTP `Cache-Control` 短暫快取）。
- 禁止用途包含：image analysis、object detection、**geodata extraction**、**offline uses**。
- 使用第三方 renderer 可以（CesiumJS 1.91+、three.js 的 3D Tiles renderer 等），但**必須把每片磁磚 glTF `asset.copyright` 彙整、排序後顯示在畫面上**（通常在底部），且不得被 logo 遮蓋。
- 可以在 3D Tiles 上疊加自己的 3D 物件，但那些物件「aren't extracted, traced, or otherwise derived by hand or machine from Photorealistic 3D Tiles」。

**因此：**

| 路線 | 條款判斷 |
|---|---|
| A. `web/live3dtiles.html` 即時串流 + `showCreditsOnScreen` | ✅ 符合 |
| B. `--mode google3d`：下載 → Blender 合併/降面/重 bake → 自行 host `scene.glb` | ❌ 屬於「儲存、離線使用、擷取」，違反政策；帳號可能被停用。程式保留供內部技術驗證，**不要公開發佈產物** |
| C. `--mode opendata`：OSM（ODbL）+ NLSC 正射影像（政府資料開放授權） | ✅ 可公開，需標示來源（viewer 底部已自動顯示） |

若需要「可離線、可公開、又高擬真」的合法來源，替代方案是：
1. 內政部國土測繪中心「多維度國家空間資訊服務平臺」的**全國三維建物模型（LOD1/LOD2，OGC 3D Tiles/I3S）**：https://3dmaps.nlsc.gov.tw/ （開放授權；本沙箱無法連線驗證，需在你的機器上確認 tileset 端點）。
2. 自行拍攝無人機影像做攝影測量（RealityCapture / Meshroom）再走本專案的 `google3d` 同一套 Blender 合併→bake→decimate 流程（把 GLB 放進 `--tiles` 目錄，`manifest.json` 列出檔名即可）。

## 4. 常見錯誤對照

| 訊息 | 原因 | 處理 |
|---|---|---|
| `API_KEY_SERVICE_BLOCKED` | key 的「API 限制」沒包含該 API | 憑證 → 編輯 key → API 限制加入 Map Tiles API |
| `Map Tiles API has not been used in project … or it is disabled` | 專案未啟用該 API | 步驟 1 |
| `This API project is not authorized to use this API` / 403 且無 JSON | 沒綁計費帳戶或 referrer 限制不符 | 步驟 2、3 |
| `REQUEST_DENIED: This API is not activated` | Geocoding/Elevation 未啟用 | 不需要就忽略 |
| 串流頁載入後畫面全黑 | root.json 成功但 referrer 限制擋掉子磁磚請求 | 把 referrer 規則改成 `http://localhost:*/*` 或加上你的網域 |


## 5. 照片 / 街景實驗（2026-09-15）

### 用到的資料

| 來源 | 內容 | 授權 |
|---|---|---|
| Wikimedia Commons `File:Delta Electronics headquarters 20110201.jpg` | 台達總部正面照（粉棕色花崗岩磁磚＋方窗、北側弧形藍綠玻璃帷幕、西側塔樓與三角形 DELTA 招牌） | CC BY-SA 3.0 |
| Map Tiles API — Street View Tiles | 台達周邊 13 張全景（7 張 Google 2022–2025、6 張使用者上傳），zoom 3 = 4096×2048 | Google 條款；每片 tile 計入 Street View Tiles SKU（每月 100,000 片免費，本次約 420 片） |

`pipeline/streetview.py`：createSession(streetview) → panoIds（POST）→ metadata → z/x/y tiles 拼接。
`pipeline/hero_photo.py` + `pipeline/build_photo_hero.py`：把每張全景當 Environment Texture，
依「著色點 − 全景位置」的方向取樣、以 `heading − 90°` 轉到 Blender 的等距柱狀慣例（中心 = +X、順時針），
權重 = 面向程度³ × 距離⁻³，烘焙成 hero 的 DIFFUSE 貼圖；並用等距柱狀相機從全景位置渲染做對位驗證。

### 結果與判斷

- **對位正確**：從全景位置渲染的驗證圖，台達出現的方位與原始全景一致。
- **貼圖品質不可用**：街景裡台達前方有整排行道樹，投影後樹葉、天空、路牌全糊在立面上；
  多張全景混合會鬼影；LOD1 高度（OSM 9 層 × 3.3 m）與實際（照片估約 25 m）有落差，樓層線對不上。
  要真的用街景貼圖，需要每張全景的可見性/遮蔽判斷（或深度）與精確幾何，這已是攝影測量的範疇。
- **可用的部份**：Commons 照片讓程序化 hero 的外觀貼近真實（磁磚＋方窗、北側弧形帷幕與深色水平帶、
  塔樓＋招牌），這個版本不含任何 Google 內容，可以公開發佈。
- **條款**：Street View 影像同樣受 Map Tiles 政策約束（須顯示 copyright、不得儲存/離線使用），
  街景貼圖版本只保留在本機 `build/` 實驗，不進 repo、不上 Pages。


## 6. 國土測繪中心三維建物（2026-09-15 實測，成功）

- 入口：多維度國家空間資訊服務平臺 https://3dmaps.nlsc.gov.tw/ （藏識科技 PilotGaea 前端）。
- 服務清單 API（前端 `DataHandlingPanel.js` 使用）：
  - 3D Tiles：`https://3dtiles.nlsc.gov.tw/tiles3d/service`
  - I3S：`https://i3s.nlsc.gov.tw/i3s/service`
- 臺北市有兩個 3D Tiles 圖層：`/building/tiles3d/0/`（建物模型）與 `/building/tiles3d/30/`（**分棟版**）。
  tileset 為 3D Tiles 1.1（`asset.version 1.1`、`contents[]`、sphere 包圍盒、`refine REPLACE`），內容為 GLB，
  使用 `EXT_mesh_features` / `EXT_structural_metadata` / `EXT_texture_bound`（藏識自訂），每片 tile 帶 2 張 2048×4096 貼圖。
- 瑞光路街區（半徑 150 m）：63 片 tile、165 MB、12.4 萬頂點；Blender 匯入後 2 萬個三角形，裁切後約 1 萬。
  幾何是真實輪廓＋高度（台達總部的弧形量體、廣場圓亭都在），屋頂貼真實正射影像，立面是通用窗格貼圖。
- 憑證：伺服器沒送 TWCA 中繼憑證（`TWCA Secure SSL Certification Authority`），一般 client 會報
  `unable to get local issuer certificate`。`pipeline/tls_tw.py` 用憑證的 AIA 網址下載中繼憑證補鏈，驗證維持開啟。
- 授權：平台公告「免費供應、免申請」的網路服務；正式發佈前請再確認該平台的使用條款並標示「內政部國土測繪中心」。


## 7. Commons 照片貼上弧形帷幕（2026-09-15）

- 來源：`File:Delta Electronics headquarters 20110201.jpg`，Solomon203，CC BY-SA 3.0（`pipeline/data/delta_hq_commons_2011.json` 有完整出處）。
- 方法（`pipeline/photo_facade.py`）：把弧形帷幕當垂直圓柱面：水平方向以 sin(角度) 對應照片 x（弦投影），
  垂直方向逐欄在手選的「女兒牆上緣曲線」與「一樓上緣曲線」之間線性取樣，展開成 2048×768 的平面貼圖。
- 貼回（`pipeline/hero.py` `tile_glass_material(arc_photo=…)`）：U = 著色點繞弧心的角度（弧心由 OSM 輪廓北向頂點鏈最小平方擬合，
  圓心 (42.3, 0.4)、半徑 30.8 m、60°→159°），V = (z − 3.4) / (24 − 3.4)；範圍外回到程序化帷幕。
- 授權：衍生貼圖為 CC BY-SA 3.0，viewer 底部已標示作者與授權；整個 `scene.glb` 因含此貼圖，公開時應一併標示。

### 為什麼 Google 街景不能用同樣方法？

1. **條款**：Street View 屬 Map Tiles API 內容，政策禁止儲存、離線使用與衍生（見第 3 節）；bake 進公開的 GLB 就違反。
2. **技術**：街景是全景，要先把台達那一段投影成透視圖再校正，本身可做；但這一帶的街景全景前方有整排行道樹、
   路燈、車輛，遮蔽比例高；且 metadata 只有經緯度與 heading，沒有精確位姿與深度，投影到幾何上會把樹和天空糊上牆。
   Commons 那張是站在對街、正對帷幕、沒有遮蔽的單張照片，才能用兩條曲線就校正好。
3. 若只做內部實驗，同樣的 `photo_facade.py` 也能吃街景透視裁圖，只是不能發佈。
