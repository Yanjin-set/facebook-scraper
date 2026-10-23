# Facebook 貼文蒐集工具

擷取專頁貼文文字及圖片，可依關鍵字篩選，圖片按貼文分開存到本機。程式不會下載影片。

## 使用方式

需要 Python與 Google Chrome。在此資料夾開啟終端機，先安裝套件：

```powershell
python -m pip install -r requirements.txt
```

第一次使用，先執行登入程式：

```powershell
python fb_login.py
```

在開啟的 Chrome 手動登入 Facebook，再回終端機按 Enter，儲存登入 cookie (!程式不會截取帳號密碼!)。未來抓取皆將自動載入登入狀態，登入失效時，再執行一次即可。

接著啟動蒐集程式，不需填任何參數：

```powershell
python fb_scraper.py
```

依照終端機提示輸入：

1. 要蒐集的 Facebook 專頁網址。
2. 模式：`1` 只抓文字、`2` 只抓圖片、`3` 文字與圖片都抓。
3. 要蒐集幾篇貼文，例如 `5`。
4. 是否篩選關鍵字：輸入 `y` 後填關鍵字；輸入 `n` 或直接 Enter 則不篩選。

關鍵字依貼文內文比對，英文區分大小寫；圖片模式也能先篩選再下載。

## 各檔案的用途

使用者只需執行登入程式與蒐集程式，其餘功能模組由程式自動使用，不用逐一執行。

| 檔案 | 用途 |
| --- | --- |
| `fb_login.py` | 第一次使用或登入失效時，手動執行以儲存登入狀態 |
| `fb_scraper.py` | 平常執行的主程式，詢問條件並蒐集貼文 |
| `fb_runtime.py` | 自動處理 Cookie、共用路徑與資料存檔 |
| `fb_download.py` | 圖片／全部模式時，自動下載圖片、重試並記錄結果 |
| `fb_validation.py` | 自動整理完整性狀態及產生摘要，不是驗收腳本 |
| `requirements.txt` | 安裝所需套件的清單 |

## 執行效果

結果會存到 `fb_output/session_日期時間/`：

- `fb_posts.json`：貼文文字、來源網址與圖片清單。
- `images/post_000/` 等資料夾：各篇圖片、文字檔與下載紀錄（圖片／全部模式）。
- `summary.json`：蒐集結果摘要；未確認完整時會標為 `partial`。

