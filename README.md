# modalpoc — 瑞光路 186 號街區 3D 地景（Blender + Modal + three.js）

以台北市內湖區瑞光路 186 號（台達電子總部）為中心、約 300 m × 300 m 的小範圍，
在 Modal 雲端用 **portable Blender 4.2 LTS** 建模、烘焙光照到貼圖、降面，輸出單一 `scene.glb`，
再用 three.js 做網頁 360° 多視角檢視。

```
pipeline/
  fetch_opendata.py   OSM 建物輪廓 + 高度、NLSC 正射影像（授權乾淨、免 Google key）
  fetch_google3d.py   Google Photorealistic 3D Tiles 抓取（需開通 Map Tiles API，見 docs/）
  blender_build.py    bpy：建模 → Cycles bake（COMBINED / DIFFUSE）→ decimate → glTF 匯出 → 預覽圖
  modal_app.py        Modal App：Blender portable image、L4/A10G/L40S GPU、Volume 輸出
  geo.py              WGS84 / ECEF / ENU / Web Mercator tile 數學
web/
  index.html          three.js 檢視器：6 個 Blender 預設視角、自動 360° 環繞、線框
  live3dtiles.html    Google 3D Tiles 串流檢視（符合 Google 條款的高擬真路線）
  assets/scene.glb    產出（由 modal run 下載）
docs/google-api-handbook.md   必須開通的 Google API、Key 限制、費用試算、條款
.claude/skills/               three.js / Blender→web skills（見該目錄 README）
```

![SW 視角](docs/img/preview_View_SW.png)
![台達總部（NLSC 幾何 + 照片修正材質）](docs/img/preview_View_HeroN.png)

`web/assets/scene.glb` 現在是 **NLSC 分棟版建物** 的成品（`--mode nlsc`），台達總部的弧形帷幕貼的是
Wikimedia Commons 照片（Solomon203，CC BY-SA 3.0）經 `pipeline/photo_facade.py` 圓柱面校正後的貼圖；OSM/LOD1 版保留為
`web/assets/scene_opendata.glb`，在 viewer 用 `index.html?glb=assets/scene_opendata.glb&stats=assets/stats_opendata.json` 切換。

## 快速開始

```bash
pip install modal requests pillow
export MODAL_TOKEN_ID=ak-...  MODAL_TOKEN_SECRET=as-...

# 1) 雲端建模 + bake（L4 GPU，約 3–6 分鐘；第一次多 2–3 分鐘建 image）
modal run pipeline/modal_app.py --lat 25.0740 --lon 121.5775 --half-size 150 \
    --bake-res 4096 --ground-res 4096 --samples 96 --target-tris 80000 --gpu l4

# 2) 網頁檢視（需 http 伺服器，不能直接雙擊 html）
cd web && python -m http.server 8080     # 開 http://localhost:8080/
```

參數：`--gpu l4|a10g|l40s|none`、`--samples`（bake 取樣數）、`--bake-res`（建物貼圖）、
`--ground-res`（地面貼圖）、`--target-tris`（decimate 目標三角形數）、`--mode opendata|google3d`。

本機（不用 Modal）也可跑：`pip install bpy==4.2.0` 後
`python pipeline/fetch_opendata.py --lat 25.0740 --lon 121.5775 && python pipeline/blender_build.py --site build/site/site.json --out build/out`。

## 部署到 GitHub Pages

`web/` 是純靜態站（three.js 已 vendor、`assets/scene.glb` 進 repo），`.github/workflows/pages.yml` 會在 push 到
`main` 或 `claude/**` 時把 `web/` 發佈到 Pages。只要在 repo **Settings → Pages → Source 選 “GitHub Actions”** 一次即可，
之後網址是 `https://<user>.github.io/modalpoc/`。手動觸發：Actions → “Deploy web/ to GitHub Pages” → Run workflow。

## 台達總部（hero）與其他建物

- **Hero**（`pipeline/hero.py`）：OSM 弧形輪廓（`w343901574`，9 層）→ 依 Wikimedia Commons 照片（CC BY-SA 3.0）
  重現外觀：粉棕色花崗岩磁磚＋方窗、北側弧形藍綠玻璃帷幕＋深色水平帶（7 層）、西側塔樓＋三角形 DELTA 招牌、
  女兒牆、屋頂太陽能板陣列、機房、綠屋頂、廣場玻璃圓亭；獨占一張 4096² bake 貼圖。
- **街景投影實驗**（`pipeline/streetview.py`、`hero_photo.py`、`build_photo_hero.py`）：把 Google Street View 全景投影到
  hero 上烘焙。方向對位正確，但行道樹遮蔽與幾何落差讓貼圖糊掉，且受 Google 條款限制，只留在本機 `build/`，
  詳見 docs 第 5 節。
- **其他建物**：LOD1 擠出 + 女兒牆 + 屋頂水箱，依 OSM `building` 標籤選玻璃辦公/一般辦公/公寓三種程序化立面，
  合併成一個 mesh、共用一張 4096² 貼圖（遊戲風：有質感、不細節）。
- **地面**：NLSC 2024 正射影像 + 建物投影（太陽方位對齊正射影像原本的陰影）。

Google Photorealistic 3D Tiles 在此區只有 2.5D 貼圖地形（實測見 docs），所以 hero 不走 Google。

## 國土測繪中心三維建物（`--mode nlsc`，目前建議的主路線）

內政部國土測繪中心「多維度國家空間資訊服務平臺」免費提供全國三維建物的 OGC 3D Tiles / I3S 服務：

| 服務 | 網址 |
|---|---|
| 服務清單（3D Tiles） | `https://3dtiles.nlsc.gov.tw/tiles3d/service` → `LAYERS.BUILDING[]` / `LAYERS.ROAD[]` |
| 臺北市分棟版建物模型 | `https://3dtiles.nlsc.gov.tw/building/tiles3d/30/tileset.json`（3D Tiles 1.1，`contents[]`，GLB） |
| 臺北市建物模型（合併版） | `https://3dtiles.nlsc.gov.tw/building/tiles3d/0/tileset.json` |
| I3S 版 | `https://i3s.nlsc.gov.tw/building/i3s/SceneServer/layers/0` |

`pipeline/fetch_tiles3d.py` 走訪 tileset（region/box/sphere 包圍盒、外部 tileset、transform 串接、b3dm→GLB），
只抓與街區相交的 tile；`--mode nlsc` 匯入後裁切、把台達那棟的牆面換成照片修正版材質（屋頂保留 NLSC 的真實正射影像）、
Cycles 光照 bake、匯出。瑞光路街區實測：63 片 tile、165 MB 原始貼圖 → 裁切後約 1 萬個三角形、3 張 4096² 貼圖。

注意：`*.nlsc.gov.tw` 伺服器沒有送出 TWCA 中繼憑證，`pipeline/tls_tw.py` 會經由憑證的 AIA 補齊鏈（驗證不關閉）。
Modal 容器可直連；本沙箱的 proxy 連不到 nlsc，故 `tools/nlsc_fetch_modal.py` 用 Modal 代抓到 `build/tiles_nlsc/`。

```bash
modal run pipeline/modal_app.py --mode nlsc --gpu l4 --samples 128     # 全流程（抓 tile + Blender）在 Modal
modal run tools/nlsc_fetch_modal.py --half 150                           # 只抓 tile 到本機
python pipeline/blender_build.py --site build/site/site.json --out build/out --mode nlsc --tiles build/tiles_nlsc
```

## 兩條「高擬真」路線

| 路線 | 幾何來源 | 擬真度 | 條件 |
|---|---|---|---|
| A. `web/live3dtiles.html` | Google Photorealistic 3D Tiles 串流 | 視 Google 涵蓋而定（**內湖此區實測只有 2.5D 貼圖地形**） | 開通 Map Tiles API；每次載入 = 1 個 root 請求（每月 1,000 次免費） |
| B. `--mode google3d` | 同上，下載 → Blender 合併/裁切/decimate/重新 bake 成單一 GLB | 同上 | 同上；**但違反 Google Map Tiles 政策的「不得儲存/離線使用/擷取」條款**，僅供內部實驗 |
| C. `--mode nlsc`（**建議**） | 國土測繪中心分棟版三維建物 + NLSC 正射影像 + 台達照片修正材質 | 高（真實幾何、真實屋頂） | 免 key、政府開放資料，可公開發佈 |
| D. `--mode opendata` | OSM 輪廓 + 高度、NLSC 2024 正射影像、程序化立面 | 中（LOD1 + 真實地面） | 免 key、授權乾淨，可公開發佈 |

細節與費用見 [docs/google-api-handbook.md](docs/google-api-handbook.md)。
