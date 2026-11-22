# adbx

透過 ADB 與 `su` root，在電腦與 Android 之間拉取、推送檔案及整個資料夾，包含應用程式私有目錄。傳輸使用 CRC-32 或 MD5 校驗，驗證成功後才將暫存內容移至目的路徑，並保留來源資料。

## 環境需求

- **電腦**：Windows、macOS 或 Linux。從原始碼執行需要 Python 3.10 以上，無第三方 Python 執行期依賴；獨立執行檔不需另裝 Python。
- **ADB**：安裝 [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)，開啟 Android 的 USB 偵錯並授權電腦連線。
- **Root 與校驗工具**：裝置可透過 `su -c` 取得 root，並提供 `stat`、`cat`，以及相容的 `crc32` 或 `md5sum`；也會偵測 `toybox crc32`、`busybox crc32`。首次使用時需允許 ADB shell 的 root 請求。
- **拉取資料夾**：裝置另需 `find`（支援 `-exec … {} +`）、`tar`、`tee`、`mkfifo` 與 `mktemp`。
- **推送資料**：裝置另需 `head`（支援 `-c`）、`mkdir`、`mv`（支援 `-T` 與 `-n`）、`rm` 與 `sleep`。
- **選用加速工具**：`dd` 用於單檔分段傳輸，缺少時改用單路；`gzip` 用於串流壓縮，自動模式在缺少必要工具時不壓縮。

## 安裝

### 從原始碼安裝

在專案根目錄執行：

```powershell
python -m pip install -e .
```

安裝後可使用 `adbx` 指令。以下操作範例統一使用等效的 `python -m adbx`；在專案根目錄也可直接使用此方式執行。

### 使用獨立執行檔

[Releases](https://github.com/ngiokweng/adbx/releases) 的壓縮檔名稱由[發布工作流程](.github/workflows/release.yml)定義：

| 平台 | 壓縮檔 | 解壓後的執行檔 |
| --- | --- | --- |
| Windows x64 | `adbx-windows-x86_64.zip` | `adbx.exe` |
| Linux x64 | `adbx-linux-x86_64.tar.gz` | `adbx` |
| macOS Intel | `adbx-macos-x86_64.tar.gz` | `adbx` |
| macOS Apple Silicon | `adbx-macos-arm64.tar.gz` | `adbx` |

解壓後，Windows PowerShell 使用 `.\adbx.exe`，macOS／Linux 使用 `./adbx`，取代下方範例中的 `python -m adbx`。執行檔已內含 Python，仍需另外準備 ADB 與已授權的 root 裝置。

Linux 版本在 Ubuntu 22.04 上建置。發布流程另產生 `SHA256SUMS`，供核對下載的壓縮檔。

## 裝置設定

預設使用 `PATH` 中的 `adb`。若不在 `PATH`，以 `--adb` 指定路徑：

```powershell
python -m adbx --adb "C:\Android\platform-tools\adb.exe" devices
```

多台裝置連線時，使用 `-s DEVICE_SERIAL` 選擇序號。`--adb` 與 `-s` 都必須放在 `devices`、`pull`、`push` 子指令之前；手機來源與目的路徑均須為絕對路徑。

## 快速開始

範例中的 `com.example.app` 與來源檔名須換成裝置上的實際路徑。先列出裝置，再拉取單一檔案：

```powershell
python -m adbx devices
python -m adbx pull /data/user/0/com.example.app/files/large.bin ./large.bin
```

拉取整個資料夾不需遞迴旗標；省略本機目的路徑時，以下指令會建立 `./files/`：

```powershell
python -m adbx pull /data/user/0/com.example.app/files
```

推送時會自動建立缺少的父目錄。以下將剛下載的檔案另存為手機上的 `renamed.bin`：

```powershell
python -m adbx push ./large.bin /data/local/tmp/new/renamed.bin
```

如本機已有 `./backup` 資料夾，可將整個資料夾推送至 `/data/local/tmp/backup/`：

```powershell
python -m adbx push ./backup /data/local/tmp/
```

預設拒絕覆寫目的檔案或資料夾。下列指令會在驗證成功後取代既有檔案；對資料夾使用 `--overwrite` 時，則在整批驗證後合併內容、取代同名檔案並保留其他檔案：

```powershell
python -m adbx pull /data/user/0/com.example.app/files/large.bin ./large.bin --overwrite
```

單檔拉取預設為 `--jobs 4 --compression auto`；可依來源內容調整，例如對 log 強制使用 gzip：

```powershell
python -m adbx pull /data/user/0/com.example.app/files/large.log ./large.log --jobs 4 --compression gzip
```

完整參數請查閱指令說明：

```powershell
python -m adbx --help
python -m adbx pull --help
python -m adbx push --help
```

## 傳輸方式

| 操作 | 傳輸與驗證方式 |
| --- | --- |
| 單檔 `pull` | 大檔案可用 1–8 路分段傳輸，小檔案自動減少路數；核對位元組數、CRC-32／MD5 與傳輸前後的來源資訊。 |
| 資料夾 `pull` | 使用一條 tar 串流，包含子目錄、隱藏檔與空目錄；核對整條打包串流的校驗值及最初的目錄清單。 |
| 檔案／資料夾 `push` | 逐檔、單路上傳，核對每個手機暫存檔的大小與校驗值，並檢查本機來源是否變動；不提供 `--jobs` 或 `--compression`。 |

優先使用通過相容性檢查的 CRC-32，沒有可用工具時改用 MD5。這些校驗用於偵測傳輸損壞，並非密碼學簽章。

單檔的自動壓縮會取樣開頭最多 256 KiB，預估可節省超過 10% 時使用 `gzip -1`。資料夾則在裝置有 gzip 時預設壓縮；`--compression none` 可關閉壓縮，`--compression gzip` 要求裝置具有 gzip。`--jobs` 不影響資料夾拉取使用的單條串流。

拉取資料夾時，手機僅在 `/data/local/tmp` 建立 FIFO、校驗值與狀態資訊，不儲存完整壓縮檔。推送則在手機目的地的父目錄建立暫存目錄，需容納整批來源資料。

## 完整性與限制

- **先停止來源寫入**：工具不會自動停止應用程式。資料夾拉取驗證的是打包串流，不會逐檔比較來源傳輸前後的雜湊；資料庫與 WAL 等多檔案的一致性，仍需由應用程式備份機制或停止寫入確保。
- **磁碟空間與檔案系統**：目的磁碟須有足夠空間，且能容納單一來源檔案。覆寫資料夾時，原有資料與整批暫存內容會同時佔用空間。
- **macOS／Linux 單檔拉取**：預設的不覆寫發布方式需要本機檔案系統支援硬連結；不支援時可使用 `--overwrite`，但這會允許取代既有目的檔案。資料夾拉取不需要本機硬連結支援。
- **連結與檔名限制**：資料夾來源中的符號連結與特殊檔案會被拒絕；推送也拒絕本機來源的目錄連接點。拉取資料夾時，無法安全儲存於本機或彼此衝突的檔名會回報錯誤。
- **整批驗證不等於整批原子替換**：新目的資料夾在驗證後整體重新命名；合併既有資料夾時，若發布途中發生 I/O 錯誤，可能已有部分通過驗證的檔案被更新。
- **推送後的權限**：檔案以 root 身分建立，不保留本機擁有者或權限，也不自動設定應用程式所需的擁有者、權限與 SELinux 標籤；目的檔案系統與掛載狀態仍須允許寫入。

目前不支援續傳。驗證失敗或發布前取消時會清理暫存內容；斷電、強制終止或手機斷線使清理失敗時，可能留下 `.part` 暫存檔或目錄，推送暫存目錄名稱為 `.adbx-push-*.part`。

## 測試

預設不連接手機，涵蓋二進位串流、分段傳輸、壓縮損毀、來源變動、校驗失敗、覆寫與取消清理：

```powershell
python -m unittest discover -s tests -v
```

實機測試需已授權的 root 裝置，會在 `/data/local/tmp` 建立唯一命名的測試資料並於完成後清除。以下為 PowerShell 的啟用方式，包含拉取與推送測試：

```powershell
$env:ADBX_DEVICE_TESTS = "1"
python -m unittest discover -s tests -v
```

超過 4 GiB 的測試需另外啟用；本機暫存磁碟需約 4 GiB 可用空間，裝置需支援 `truncate` 與稀疏檔案：

```powershell
$env:ADBX_DEVICE_TESTS = "1"
$env:ADBX_LARGE_TEST = "1"
python -m unittest tests.test_device.DeviceTests.test_over_four_gib -v
```

實機測試另可透過 `ADB` 指定 ADB 執行檔、`ANDROID_SERIAL` 指定裝置。測試實作位於 [tests/](tests/)。

## 建置與發布

[Release 工作流程](.github/workflows/release.yml)使用 Python 3.12，在四個目標平台執行不連接手機的測試，再以 PyInstaller 打包，並於專案目錄外測試解壓後執行檔的 `--help`、`pull --help` 與 `push --help`。

本機打包使用 [requirements-build.txt](requirements-build.txt) 中的依賴：

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --python-option "X utf8" --name adbx --paths . --specpath build adbx/__main__.py
```

產物位於 `dist/`，對應目前電腦的平台與架構。打包時啟用 UTF-8 模式，供繁體中文輸出使用。

推送任意 tag 會觸發四平台建置；全部成功後，工作流程上傳壓縮檔與 `SHA256SUMS`，再發布對應的 GitHub Release。手動執行工作流程只會測試、打包並保留建置產物，不會發布 Release。
