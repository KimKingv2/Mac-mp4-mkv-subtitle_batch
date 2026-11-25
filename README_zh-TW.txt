FRAME Cinema MKV Batch v2.4.14-beta
================================
macOS 批次 MKV 無損封裝／影片壓制工具

重要：本版本為 beta。v2.4.0 新增主要字幕語言設定檔 English／Japanese／Korean；所有正式封裝模式只保留可可靠識別的 English／Japanese／Korean 音訊軌，語言未知或衝突時安全停止。v2.4.2～v2.4.11 逐步修正單軌 Default／Forced、跨容器 CodecID、顯示尺寸、DefaultDuration 及 AAC BitDepth 的狹義正規化，所有其他軌道屬性、封包數及完整解碼仍嚴格驗證。**v2.4.12 修正 FFprobe 封包摘要漏取 stream index 的錯誤**：來源音訊依 FFprobe 全域 stream index 篩選時，先前摘要未要求 index 欄位，會把 index 缺失的來源音訊誤篩成 0 軌；現在明確讀取 index，確保來源與輸出音訊封包摘要正確逐軌比較。此修正不放寬軌道數量、codec 或封包數檢查。v2.4.12 通過 114 項單元測試及 Python 語法檢查。建置環境沒有 MKVToolNix，真實 mkvmerge／mkvextract macOS 端到端測試仍待執行。第一次請先用 1 集測試，確認音軌、字幕、旁掛檔及播放正常後再批次處理。 **v2.4.13 修正 FFmpeg 字幕解析驗證的 /dev/null 假成功問題**：原先 ASS muxer 輸出到已存在的 /dev/null，FFmpeg 9.0.2 會顯示檔案已存在／無法開啟，但仍可能回傳 0，舊程式因此誤判字幕解析成功。現在改用 pipe:1，stdout 直接丟棄，不寫入磁碟，並額外攔截已知致命診斷訊息。SubRip 與 ASS 均加入實際 FFmpeg pipe smoke test。

v2.4.14-beta 新增通用中文字幕旁掛匯出：每個 MKV 完整驗證及正式發布後，除原本主要 English／Japanese／Korean `.en`／`.ja`／`.ko` 外，所有封裝模式都會嘗試從最終 MKV 匯出唯一可識別的中文字幕或中英雙語字幕為同名 `.zh`，例如 `.zh.srt`／`.zh.ass`。支援 zh／zho／chi 語言標籤，以及明確標示 Chinese／中文／中英的雙語軌名稱（包括 `Chinese + English`、Language=und）。若有多條中文候選，只在其中存在唯一 Default 時選取；語言標籤矛盾、無唯一候選、格式不支援或匯出失敗時記錄原因，不猜測、不覆蓋既有檔案。批次預檢也會檢查同名 `.zh` 是否已存在。此變更不改動 MKV 內軌道、封裝、音訊篩選、Default／Forced 設定或原有驗證條件。完整測試 132/132 通過；建置環境未安裝 MKVToolNix，真實 macOS 端到端仍須在使用者環境抽查。

一、工具功能
----------------
同一套工具提供兩種正式影片處理方式及一種獨立品質測試。正式封裝先選主要字幕語言（English／Japanese／Korean），再選字幕模式。所有正式封裝的音訊保留清單固定為 English／Japanese／Korean；其他語言不進入最終 MKV。

A. 無損封裝（Remux）
- 不重新編碼影片或保留的音訊。
- 只將來源影片與字幕重新封裝成 MKV，適合只加入／整理字幕的情況。
- 影片與保留音訊資料不因封裝而重新壓縮；非 English／Japanese／Korean 音訊軌會在封裝時排除。

B. 重新編碼影片後封裝（Transcode + Mux）
- 只重新編碼影片軌，預設推薦 HEVC Main10（10-bit）。
- 只保留語言可辨識的 English／Japanese／Korean 音訊軌，由 MKVToolNix 直接複製，不重新編碼；Russian、Chinese、French 等其他語言會排除。若音軌語言標籤與名稱均無法識別，或 Language／LanguageIETF 互相矛盾，該集停止，避免誤刪英／日／韓音訊。
- 原始字幕、章節、附件及可攜帶的 Matroska 資訊，交由 MKVToolNix 從原始來源複製。
- 可以把 4K／1440p 縮小至所選最高解析度，例如 1080p；保持畫面比例，不升頻、不裁切。
- 重新編碼是有損操作，畫質可能有細微損失；來源檔永遠不會被覆蓋。

正式封裝在最終 MKV 完整驗證並正式發布後，會按所選主要字幕語言抽取旁掛字幕：English 為 `.en`、Japanese 為 `.ja`、Korean 為 `.ko`；同時嘗試從同一個最終 MKV 抽取唯一可識別的中文字幕／中英雙語字幕為 `.zh`。此規則適用所有封裝模式，包括追加字幕、RARBG 普通英文＋SDH 等模式。

    節目名稱 S01E01.mkv
    節目名稱 S01E01.en.srt   （主要字幕語言選 English）
    節目名稱 S01E01.ja.srt   （主要字幕語言選 Japanese）
    節目名稱 S01E01.ko.srt   （主要字幕語言選 Korean）
    節目名稱 S01E01.zh.srt   （唯一可識別中文字幕／中英雙語字幕）

實際副檔名依字幕格式可為 `.srt`／`.ass`／`.ssa` 等。所有旁掛字幕都從最終 MKV 的實際字幕軌提取及驗證，不是任意複製輸入資料夾裡的字幕。若沒有可識別中文軌、中文候選多於一條且沒有唯一 Default、語言標籤矛盾或格式不支援，程式會記錄未匯出原因，不猜測軌道、不影響已通過驗證的 MKV，也不覆蓋既有 `.zh` 檔案。

二、安裝需求
------------
需要：
- macOS 與 Python 3。
- FFmpeg 與 FFprobe（所有模式都需要）。
- MKVToolNix 命令列工具：mkvmerge、mkvextract（正式批次封裝／壓制模式需要；單集品質測試模式不需要）。
- 若要壓制 HEVC，需要 FFmpeg 編入 libx265。
- 若要壓制 AV1，需要 FFmpeg 編入 libsvtav1 或 libaom-av1。
- 若要 HDR→SDR，需要 FFmpeg 編入 zscale 與 tonemap 濾鏡。

若未安裝，可在終端機執行：

    brew install mkvtoolnix ffmpeg

檢查可用編碼器：

    ffmpeg -hide_banner -encoders | grep -E 'libx265|libsvtav1|libaom-av1'

檢查 HDR→SDR 濾鏡：

    ffmpeg -hide_banner -filters | grep -E 'zscale|tonemap'

AV1 若未顯示可用編碼器，程式會停用 AV1 選項，不會假裝支援。

三、如何執行
------------
1. 解壓縮整個工具資料夾。
2. 雙擊 Start-FRAME-MKV-Batch.command。
3. 選擇工作模式：
   1 = 批次無損封裝，不重新編碼影片或音訊。
   2 = 批次壓制影片再封裝，白名單音訊／字幕／章節／附件仍直接複製。
   3 = 單集編碼品質測試，只產生三段短樣片，不執行正式封裝。
4. 若選擇模式 3，依照提示選擇一集 MKV、CRF、每段片長、三個片段起點及輸出位置；不需要選字幕模式。
5. 若選擇模式 1 或 2，先選主要字幕語言：English（.en）、Japanese（.ja）或 Korean（.ko）。這是每一批次的主要字幕設定，不會自動猜測劇集國別。再選字幕封裝模式及來源資料夾。工作模式 1 的組合包含原有雙語字幕、主要語言＋中文，以及兩種僅限 English 的 RARBG 模式。
6. 檢查完整配對預覽與「跳過／需人工檢查」明細。
7. 只有輸入大寫 YES 才會開始批次封裝。

四、主要字幕語言與音訊保留規則（v2.4.0-beta）
----------------------------------------------
正式批次封裝（工作模式 1／2）開始前，選擇本批次的主要字幕語言：
1 = English（英語作品／美劇；主要字幕 Default Yes；旁掛 `.en.*`）
2 = Japanese（日語作品／日劇；主要字幕 Default Yes；旁掛 `.ja.*`）
3 = Korean（韓語作品／韓劇；主要字幕 Default Yes；旁掛 `.ko.*`）

語言設定是「每批次一次」的選擇，不會單靠資料夾名稱自動判斷作品國別。建議一批處理同一主要語言的作品；若同一批混合美劇、日劇、韓劇，請分批執行，避免套用錯誤的預設字幕語言。

所有正式封裝模式統一只保留 English／Japanese／Korean 音訊軌（ISO 639-2：eng／jpn／kor）；其他明確識別的語言軌（例如 Russian／rus）會排除。保留的音訊直接 Copy，不重新編碼。原有 Default 音訊旗標在保留軌中維持；若原本設為 Default 的音軌全部被排除，程式會優先把本批次主要語言音軌設為 Default；該語言音軌不存在時，才選第一條保留音軌作為備援 Default。這個備援只在原 Default 全被排除時發生。

若 Language／LanguageIETF 具有不同寫法但代表同一語言（例如 `rus` 與 `ru-RU`），程式會先正規化再比較。若語言欄位缺失，僅在音軌名稱明確標示語言時才推斷（例如 Russian、English、Japanese、Korean、中文）；若仍無法識別，或 MKVToolNix 與 FFprobe 對同一軌的明確語言標籤衝突，該集會停止，不會冒險刪除可能屬於英／日／韓的音軌。請先用 MediaInfo／MKVToolNix 修正來源語言標籤。

字幕模式 1 的 1C／1D（RARBG 英文 Subs）只適用 English profile，因 RARBG 這兩種流程的來源字幕固定是英文；Japanese／Korean 作品請選一般主要語言字幕資料夾模式。字幕模式 2／3／4 的英文專用舊名稱已改為依所選主要語言運作；模式 4 仍保留來源 Default 旗標，不保證所選語言會被設為 Default。

五、單集編碼品質測試（新增）
----------------------------
此模式是為正式批次壓制前先確認畫質及估算容量而設計：
- 選擇一集 MKV，固定使用 HEVC Main10（x265），預設 CRF 21、Very Slow，最高 1080p。
- 預設產生三段各 60 秒樣片：一般／對話、動作／快速變化、暗場／複雜細節。可以為每段輸入自訂起點；空白則採用按片長分布的參考時間。
- 每段可調整為 30～120 秒。三段不可重疊。
- 測試模式會強制重新編碼，絕不套用正式批次的「相同格式自動跳過」規則。來源即使已經是 HEVC 10-bit，也會實際試壓。
- 若來源是 10-bit／廣色域但 HDR／SDR 標籤不完整，測試前會要求明確確認；確認後只保留可讀到的標籤、不做 tone mapping、不自行補寫 BT.709。正式批次模式也提供相同的明確例外選項；預設仍是停止。
- 樣片只有影片軌，不含音訊或字幕，方便集中比較畫質。樣片會輸出為三個獨立 MKV，並逐一執行完整影片解碼驗證。
- 完成後會以三段樣片的平均位元率推算整集影片資料量，再加回來源檔中音訊、字幕及其他非影片資料量，給出整集大小估算及約 ±20% 的參考區間。這不是精確承諾，場景差異可能令實際結果超出區間。
- 為估算原片非影片資料量，程式會以 FFmpeg 串流複製方式完整讀取來源影片軌並統計輸出位元數；資料經管線計數，不會建立另一份完整影片檔，也不會修改來源。
- 請把測試輸出位置選在外接硬碟，避免樣片寫入 Mac 內部 SSD。

重要：若品質測試結果滿意，之後使用批次模式 2 對同一部已是 HEVC Main10 的來源正式壓制時，請在「相同格式自動跳過」選項輸入 N。若保持預設 Y，程式會保護來源並跳過二次壓制，CRF 不會執行。若來源色彩標籤不完整，還必須明確啟用 10-bit 色彩標籤例外，否則正式批次仍會停止該集。

批次模式的輸出資料夾必須是獨立資料夾，不可位於來源影片或字幕資料夾內。建議每一輪使用新的空資料夾。若輸出 MKV 或同名主要語言旁掛字幕已存在，該集會在開始前跳過，不會覆蓋。

五、壓制選項說明
----------------
1. 影片編碼

HEVC Main10（10-bit）（推薦）
- 使用 x265，輸出 HEVC Main10 / 10-bit。
- 適合建立長期片庫，在壓縮效率、畫質和裝置支援之間較均衡。
- 8-bit 來源轉成 10-bit 不會憑空恢復原片不存在的色彩細節，但較高的編碼精度有助於控制後續壓縮的色階損失。

HEVC 8-bit
- 使用 x265，輸出 HEVC Main / 8-bit。
- 主要供不支援 HEVC Main10 的較舊設備使用。
- 若來源是 HDR 且選擇保留 HDR，程式會拒絕 HEVC 8-bit，以免輸出不適合的 HDR 位深。

AV1 10-bit
- 若 FFmpeg 有 libsvtav1，優先用 SVT-AV1；否則用 libaom-av1。
- 壓縮效率有機會比 HEVC 更高，但編碼時間可能更長，而且必須確認播放設備支援 AV1 10-bit。
- AV1 的品質值與速度預設和 x265 不同，不能拿相同數字直接比較。

2. HDR 處理

依原片保留 HDR／SDR（推薦）
- HDR 來源嘗試保留原始 HDR 色彩標籤及可確認的靜態 HDR metadata。
- SDR 來源仍輸出 SDR。
- 不會將 SDR 假裝轉成 HDR。
- 若 HDR 靜態 metadata 在編碼後無法通過檢查，程式會停止該集，不建立最終 MKV。

HDR 轉換成 SDR（色調映射）
- 對可識別的靜態 HDR10／HLG 來源使用 zscale + tonemap 映射至 BT.709 SDR。
- 這會改變原始 HDR 呈現，不可還原成原始 HDR。
- 若來源是 Dolby Vision 或 HDR10+ 動態 HDR，本版會停止重新編碼，避免未經驗證而丟失動態 metadata。這些片源請選無損封裝並保留原檔。

3. 最高輸出解析度

最高 1080p（推薦）
- 4K／1440p 自動縮小至不超過 1920×1080。
- 低於 1080p 的來源不升頻。

最高 1440p
- 4K 自動縮小至不超過 2560×1440；低於上限不升頻。

保持原始解析度
- 不做解析度縮小，只按所選編碼器及品質重新編碼。

縮放採用 Lanczos，保持原始長寬比並避免裁切。像素尺寸不一定剛好等於 1920×1080，例如超寬畫面會保持原始比例。

4. CRF／品質值

x265（HEVC）的建議起始範圍：
- CRF 18～19：畫質優先，檔案通常較大。
- CRF 20～21：畫質與容量平衡（推薦先從 21 測試）。
- CRF 22～23：容量優先，暗場、顆粒、頭髮及細節損失風險較高。
- CRF 24～27：較激進壓縮，不建議作為高品質收藏預設。

AV1 品質值：
- 程式接受 20～40，建議先測 28～35。
- 數字越小通常品質越高、檔案越大；實際碼率依編碼器及內容而異。
- AV1 CRF 不能與 x265 CRF 直接比較；請用相同片段實測畫質和大小。

CRF 是恆定品質目標，不是目標檔案大小。不同集數、畫面複雜度、顆粒及來源碼率會造成輸出大小不同。

5. 編碼速度 Preset

x265：
- Medium：較快，壓縮效率通常低於 Slow。
- Slow（推薦）：速度與壓縮效率較均衡。
- Slower：分析更多，可能再節省一些容量，但更耗時。
- Very Slow：非常耗時，額外壓縮收益通常逐漸遞減。
- Placebo：極端耗時，通常不值得日常使用。

SVT-AV1：
- 程式提供 Preset 8、6（推薦）、4、2、0。
- 數字越小通常越慢；與 x265 preset 不可直接等同。

libaom-av1：
- 程式提供 cpu-used 6、4（推薦）、3、2、1。
- 數字越大通常越快，但壓縮效率可能較低；與 x265 preset 不可直接等同。

6. 自動跳過相同格式
- 預設啟用：若來源已符合所選編碼及位深，而且不需要縮小解析度，程式會跳過二次有損壓制，避免無意義的再壓縮。
- 若輸出編碼和來源不同（例如 HEVC→AV1），程式仍會按選擇重新編碼。
- 若你明確想把已符合所選格式的來源再壓一次，可選 N 關閉自動跳過。

六、字幕封裝模式（原四種功能保留；工作模式 1 現有四種雙字幕組合）
------------------------------------------------------------
工作模式 1 進入後會先詢問雙字幕組合：

快速分辨：
- 中文／雙語字幕是獨立的外掛字幕檔：純中文選 1B；中英雙語選 1A，並另外提供主要語言字幕檔。
- 中文／雙語字幕已經內嵌在原始 MKV，只需要再新增英文：不要選 1A／1B，改用工作模式 2／3／4，原字幕才會保留。

1A：已有外掛中英雙語字幕檔，再加入主要語言字幕（需要兩個外掛字幕資料夾）
- 按 SxxExx 配對影片、所選主要語言字幕 ASS／SSA／SRT，以及已準備好的中英雙語字幕 ASS／SSA／SRT。
- 「已有中英雙語字幕」是指另外提供的外掛字幕檔；本選項不會保留原始 MKV 內嵌字幕，封裝時會排除來源影片內嵌字幕。
- 主要語言字幕依 profile 標記為 eng／jpn／kor 並設 Default Yes；Chinese + English（und）設 Default No。
- 若原始 MKV 已內嵌純中文或中英雙語字幕，而你只想再新增英文，請勿選 1A；請改用工作模式 2／3／4 的追加字幕流程，以保留原有內嵌字幕。

1B：無內嵌字幕影片 + 主要語言字幕 + 外掛純中文字幕（新增；需要兩個外掛字幕資料夾）
- 按 SxxExx 配對影片、主要語言 ASS／SSA／SRT、中文字幕 ASS／SSA／SRT。
- 選擇四個資料夾：影片、主要語言字幕、純中文字幕、獨立輸出資料夾。
- 主要語言字幕依 profile 標記 eng／jpn／kor 並設 Default Yes；Chinese（zh）設 Default No。中文使用通用 zh 語言標籤，不自行猜測 zh-TW 或 zh-CN，字幕文字本身維持原樣。
- 輸出 MKV 保留來源影片、通過 English／Japanese／Korean 白名單的音訊、章節及可攜帶附件；影片與保留音訊均不重新編碼。
- 安全限制：來源影片必須沒有任何內嵌字幕軌。若發現原片已有 PGS、SRT 或其他內嵌字幕，該集會停止，不會為了套用兩條新字幕而靜默丟棄原字幕。這類影片請改用其他封裝模式。
- 最終 MKV 必須恰好有兩條新增字幕；驗證器確認主要語言 Default Yes、中文 Default No，逐條解析字幕，並完整解碼所有輸出影片與保留的音訊軌。
- 完整驗證成功後，從最終 MKV 實際提取主要語言 `.en`／`.ja`／`.ko`，並嘗試同時輸出唯一可識別的中文字幕／中英雙語 `.zh`；所有旁掛檔都依最終 MKV 軌道格式匯出。

1C：RARBG Subs 每集資料夾（新增）
- 適用 RARBG 常見結構：影片檔直接放在影片根目錄；`Subs/` 內以每集影片完整檔名建立子資料夾，子資料夾裡放 `3_English.srt`、`4_English.srt` 等英文字幕。也支援字幕檔再放在每集子資料夾的下一層。
- 依序選擇三個資料夾：影片根目錄、RARBG `Subs` 資料夾、獨立輸出資料夾。
- 優先以影片完整檔名（不含副檔名）精確匹配 `Subs` 子資料夾；找不到時，僅在 `SxxEyy` 對應資料夾唯一時才使用集數回退匹配。多個候選會停止，不會把字幕配到錯集。
- 在每集子資料夾中遞迴尋找檔名含 English 的 ASS／SSA／SRT。檔名數字（例如 3_、4_）只作檔案名稱，不作字幕類型依據。
- 讀取字幕內容，優先識別 SDH 明確標記（例如 `[English - us - sdh]`、SDH／Hearing Impaired），並輔以聲音描述行密度；檔名標明 Forced 的檔案，以及事件數極少（20 條以下）的疑似 Forced／局部字幕會排除。必須唯一找到一份普通英文及一份 SDH，否則該集停止並列出判定依據。
- 封裝為兩條英文字幕：普通 English（eng，Default Yes）；English SDH（eng，Default No）。SDH 保留在 MKV 內作為可切換的輔助字幕。
- 完整驗證後，從最終 MKV 匯出普通英文 `.en.srt`／`.en.ass`，SDH `.en.sdh.srt`／`.en.sdh.ass`；另依通用規則嘗試匯出可識別的中文字幕／中英雙語 `.zh`。所有檔案都從最終 MKV 提取，不會直接複製原始字幕。
- 安全限制：此流程與 1B 一樣，要求來源影片沒有任何內嵌字幕軌；若來源已有內嵌字幕，該集停止，避免為了加入兩條外掛字幕而靜默刪除原字幕。
- 你提供的 Shameless 樣本中，`3_English.srt`（682 個字幕事件、沒有獨立聲音描述）被識別為普通英文；`4_English.srt`（722 個字幕事件、39 行聲音描述，且尾端有 `[English - us - sdh]`）被識別為 SDH。此判定來自內容，不是把 3、4 編號寫死。

1D：RARBG 普通英文 + 外部中文字幕（新增 v2.3.1）
- 適用英文字幕位於 RARBG `Subs/每集資料夾/`、中文字幕另存於獨立資料夾的情況。
- 依序選擇四個資料夾：影片根目錄、RARBG `Subs` 根目錄、外部中文字幕根目錄、獨立輸出資料夾。
- 英文字幕依影片完整檔名精確匹配 RARBG 每集資料夾；找不到時只允許唯一 SxxEyy 回退。中文字幕從獨立資料夾遞迴掃描，按唯一 SxxEyy 與影片配對。
- 在 RARBG 每集資料夾內，依字幕內容辨識唯一普通英文；若另有 SDH，記錄為未加入本模式的輔助候選。多份普通英文或判定異常時停止該集。
- 封裝兩條字幕：RARBG 普通 English（eng，Default Yes）及外部 Chinese（zh，Default No）。RARBG SDH 不加入此模式；若要同時加入 SDH，請使用 1C。
- 來源影片必須沒有任何內嵌字幕軌，避免加入外掛字幕時靜默丟棄來源字幕。
- 最終 MKV 完整驗證後，從最終 MKV 實際提取普通英文 `.en.srt`／`.en.ass`，並依通用規則另外提取中文字幕／中英雙語 `.zh`。
- 中文字幕必須依檔名含唯一 SxxEyy 匹配；缺失、重複或無法識別時停止該集並記錄原因。

1A 詳細流程：影片 + 純英文字幕 + 原始雙語字幕
- 按 SxxExx 配對 MP4／MKV／M4V、英文 ASS／SSA／SRT、雙語 ASS／SSA／SRT。
- 保留來源影片、通過語言白名單的音訊、章節及可攜帶附件。
- 排除來源影片內嵌字幕，避免重複。
- 加入所選主要語言字幕（eng／jpn／kor，Default Yes）及 Chinese + English（und，Default No）。

模式 2：MKV 新增一條所選主要語言字幕，強制新字幕為唯一預設
- 保留 MKV 原有字幕內容，但將原有字幕 Default 設為 No。
- 新增的所選主要語言字幕設為 Default Yes。

模式 3：MKV 新增一條字幕，自動選擇所選主要語言作為唯一預設
- 目標語言字幕可來自原 MKV 內嵌軌，也可來自本次新增外掛軌。
- 其餘字幕保留但設為 Default No。
- 若有多條目標語言字幕且無法唯一判斷，停止該集，不猜選。

模式 4：MKV 新增一條字幕，完全保留原有字幕預設設定
- 逐條讀取原有 Default 旗標並明確沿用。
- 新增字幕設為 Default No。

模式 2～4 只接受 MKV 作為來源，每集需要唯一配對一個 MKV 和一份外掛 ASS／SSA／SRT。

1A、1B 及原有模式 2～4 會在最終 MKV 驗證成功後，嘗試輸出所選主要語言的旁掛字幕（`.en.*`／`.ja.*`／`.ko.*`）。1C 只適用 English profile，會分別輸出普通英文 `.en.*` 及 SDH `.en.sdh.*`；1D 也只適用 English profile，輸出普通英文 `.en.*`。若沒有可識別的目標語言字幕、多條字幕無法唯一判斷或格式不支援，會在日誌說明；MKV 封裝結果與旁掛匯出結果分開統計。1B／1C／1D 只會在確認來源沒有內嵌字幕時執行；它們不會移除任何來源字幕軌。

七、壓制安全限制
----------------
為避免自動轉換損害畫面或音訊，本版壓制流程只接受已確認可處理的簡單片源。遇到以下情況會停止該集壓制，請改用無損封裝或另外人工處理：
- 多條影片軌、attached-picture 封面影像軌。
- 隔行影片。
- 非一般 4:2:0 色度取樣／像素格式。
- 影片旋轉／Display Matrix 資訊。
- Dolby Vision 或 HDR10+ 動態 HDR。
- 10-bit／BT.2020 來源卻沒有足夠色彩資訊可判定 HDR／SDR，且本集確實需要重新編碼、縮放或色調映射。預設停止；若你明確確認要保留可讀標籤、不做色調映射，僅限 10-bit 輸出時可啟用例外。HDR→SDR 及 8-bit 輸出仍不允許此例外。
- 來源沒有音訊軌。

這些限制是保護措施，不代表來源檔案損壞。遇到限制時，可先選擇「只做無損封裝」，保留來源內容。

v2.0.1-beta 修正：若來源已符合所選編碼／位深，且不需要縮小解析度或轉換 HDR，會先執行自動跳過二次壓制，再檢查需要轉碼時才會用到的色彩 metadata。這種情況會直接保留原影片位元流及原色彩 metadata，繼續無損封裝；不會因缺少 transfer 標籤而錯誤阻止單純加字幕。
v2.1-beta 補充：若確實需要重新編碼，但來源 10-bit／廣色域色彩標籤不完整，預設仍停止；只有使用者明確啟用例外、選擇保留色彩且輸出仍為 10-bit 時，才允許不做 tone mapping、只保留可讀標籤後繼續。HDR→SDR 及 8-bit 輸出不允許例外。若不確定來源動態範圍，請維持預設停止。
v2.1.1-beta 修正：壓制後以 MKVToolNix 複製原音訊／字幕時，若來源原本沒有明確的 LanguageIETF 標籤，MKVToolNix 可能依照未變動的舊版 Language 自動補出 BCP 47 標籤（例如 eng → en）。舊版驗證將「欄位缺席」與「欄位補齊」誤判為音訊設定變更，導致整集停止。新版允許這種有明確來源缺席且舊版 Language 完全一致的自動補齊；若來源原本有明確 LanguageIETF，內容變更或欄位消失仍會停止。音訊／字幕軌數量、編碼、舊版語言、名稱、Default／Forced 旗標仍嚴格驗證。錯誤訊息亦會列出來源與暫存值，方便診斷。

八、P0 安全封裝與嚴格保留驗證（v2.2.0-beta）
--------------------------------------------
- 每集最終 MKV 先建立在輸出磁碟同一資料夾層級的隱藏暫存資料夾 `.frame-v2-staging-*`，正式輸出名稱不會在驗證前出現。
- 只有 MKVToolNix 軌道／metadata、FFprobe 串流格式、字幕解析、完整影片與音訊解碼，以及章節／附件檢查全部通過，程式才會發布正式 MKV。
- 正式發布使用不覆蓋既有檔案的原子操作；若相同名稱在執行期間被其他程序建立，程式停止發布，不覆蓋該檔案。
- 驗證失敗或 Ctrl+C 時，已生成的未驗證 MKV 會移至輸出旁的 `Failed/` 資料夾，以時間戳命名；它不會佔用正式 `.mkv` 名稱，修正問題後可重新執行該集。Failed 內檔案可能很大，確認不需診斷後可由使用者自行刪除。
- 若遇到突然斷電、系統崩潰或強制結束 Python，程式可能來不及整理隱藏 `.frame-v2-staging-*` 暫存目錄；本版不會在下次啟動時自動刪除這類目錄。確認沒有處理程序仍在執行前，請勿直接清理；啟動時掃描／恢復屬於下一階段 P1。
- 音訊逐軌比較 codec、codec ID、語言／LanguageIETF、軌道名稱、Default／Forced／Enabled、聲道數、取樣率及位深等可取得欄位；FFprobe 另檢查 codec profile、sample rate、channel layout 等格式資料。
- 字幕逐軌比較 codec、codec ID、語言／LanguageIETF、名稱、Forced／Enabled 及模式規定的 Default；所有輸出字幕仍需通過解析或 bitmap 封包檢查。
- 章節會比較完整章節樹，而不只是章節數量；包含起訖時間、UID、Edition flags、顯示文字及語言。
- 附件比較數量、名稱、MIME、描述及大小。來源是 MKV 時，還會用 mkvextract 提取來源與輸出附件並比對 SHA-256。來源是 MP4／M4V 時，mkvextract 無法直接讀取原容器，只能驗證封裝後附件的 metadata／大小，日誌會明確說明這個限制。
- MKVToolNix 依照來源既有舊版 Language 自動補出 LanguageIETF（例如 eng → en）仍允許；來源原本已有明確 LanguageIETF 時，變更或消失仍會停止。
- 重新編碼模式會先比較「原始來源 → 壓制準備檔」的音訊、字幕、章節及附件；再比較「壓制準備檔 → 最終 MKV」的影片／音訊等封裝保留資訊。

八、驗證與日誌
--------------
每集完成後執行：
- FFprobe／MKVToolNix：檢查影片、音訊、字幕軌數量、編碼及軌道標籤。
- 音訊：確認白名單音訊軌數量、編碼及屬性保留；不重新編碼保留的音訊。
- 字幕：逐條解析文字字幕；PGS／VobSub 等 bitmap 字幕以 FFprobe 讀取封包數檢查，不會強制轉成文字字幕。
- FFmpeg：對最終影片及全部保留音訊完整解碼，輸出丟棄至 null，不產生另一份影音檔。
- 主要語言字幕：從最終 MKV 以 mkvextract 抽取；依所選 profile 輸出 `.en`／`.ja`／`.ko` 旁掛檔，ASS／SSA／SRT 會再檢查旁掛檔。
- 保留 mkvmerge 完整輸出、退出代碼、警告原文、FFmpeg 診斷、壓制時間、跳過原因及逐集結果。

完整解碼會讀取整部輸出影片及音訊，因此需要時間和磁碟讀取；這一階段不寫出新影音檔、不重新編碼。壓制本身會寫出影片暫存檔及新的最終 MKV。

完整解碼不能保證字幕翻譯正確、字幕與音訊逐句同步，也不能代替播放器實際播放抽查。

九、磁碟空間與資料安全
----------------------
- 來源影片永遠不會被修改或覆蓋。
- 最終 MKV 另存至你選擇的輸出資料夾。
- 壓制暫存檔會放在所選輸出資料夾所在的磁碟，不使用系統內部磁碟的 /tmp 作為影片工作目錄。
- 若要降低 Mac 內部 SSD 寫入，建議將工具資料夾及輸出資料夾放在外接硬碟，並讓暫存工作也留在該外接磁碟。
- 壓制需要額外空間；程式會在開始前及最終封裝前檢查可用容量，但 CRF 輸出大小不可精確預測。請預留充足空間，避免在磁碟接近滿載時批次處理。
- 若外接硬碟連線不穩、休眠或容量不足，請先排除問題再批次執行。
- 每輪建立獨立日誌：FRAME_MKV_Batch_YYYYMMDD_HHMMSS_microseconds.log。

十、建議第一次使用方式
--------------------
1. 先用模式 3 對一集有代表性的片源測試三段樣片，預設 HEVC Main10、CRF 21、Very Slow、最高 1080p。
2. 使用 IINA 等播放器檢查普通場景、動作場景和暗場；樣片沒有音訊或字幕。
3. 若壓縮和畫質可接受，再用模式 2 對少量集數正式壓制，先選正確的主要字幕語言 profile，並確認最終只保留英／日／韓音訊及對應 `.en`／`.ja`／`.ko` 旁掛檔。
4. 若來源本來已是 HEVC Main10，並確定要重新壓制，請關閉相同格式自動跳過（輸入 N）。
5. HDR 片源優先保留 HDR；不要直接對 Dolby Vision／HDR10+ 使用壓制模式。
6. 確認日誌及成品均符合預期後，再批量處理。

版本：2.4.9-beta

v2.1.2-beta 新增：工作模式 1 增加 1B「無內嵌字幕影片＋純英文字幕＋純中文字幕」組合。依 SxxExx 配對三種來源，英文標記為 eng／English／Default Yes，中文字幕標記為 zh／Chinese／Default No。來源如含任何內嵌字幕會安全停止，不會靜默刪除原字幕。輸出恰好兩條外掛字幕後，逐條驗證字幕解析、完整解碼全部影音，再從已驗證的最終 MKV 提取同名 .en.srt／.en.ass。


v2.2.0-beta P0 更新：所有正式 MKV 先輸出到隱藏 staging，完整驗證通過才不覆蓋地原子發布；失敗檔隔離至 Failed。新增逐軌 Matroska／FFprobe metadata、完整章節樹、附件 metadata 檢查；MKV 來源附件另做 SHA-256 對照。詳情見本說明的「P0 安全封裝與嚴格保留驗證」及 CHANGELOG.txt。

十一、RARBG Subs 模式回歸測試（v2.3.0-beta）
--------------------------------------
- 測試樣本直接取自使用者提供的 `字幕處理.zip`，已放入 `tests/fixtures/`，供單元測試重現。
- 樣本 `3_English.srt`：682 個字幕事件，未檢出獨立聲音描述行，分類為普通英文。
- 樣本 `4_English.srt`：722 個字幕事件，檢出 39 行獨立聲音描述，並包含 `[English - us - sdh]` 明確標記，分類為 English SDH。
- 測試涵蓋完整發布資料夾名稱匹配、巢狀字幕檔掃描、唯一集數回退匹配、重複資料夾拒絕、普通英文重複時拒絕猜測、Forced 排除、普通／SDH 匯出選軌及來源已有內嵌字幕時停止。
- 本次 19 項單元測試全部通過；真實 MKVToolNix 封裝仍需在使用者 Mac 上以少量影片完成端到端測試。


十二、RARBG 英文＋外部中文字幕模式（v2.3.1-beta）
------------------------------------------------
- 新增工作模式 1 的 1D：從 RARBG `Subs` 每集子資料夾選取普通英文，再從獨立中文字幕資料夾按 SxxEyy 配對中文。
- 封裝 English（eng，Default Yes）及 Chinese（zh，Default No），不加入 RARBG English SDH；若偵測到 SDH，會記錄並忽略。
- 來源需無內嵌字幕；完整驗證通過後從最終 MKV 提取 `.en.srt`／`.en.ass` 旁掛字幕。
- 增加 RARBG 普通英文選取與外部中文字幕集數配對回歸測試。


v2.4.1-beta：澄清工作模式 1 的字幕組合 1／2。組合 1 是兩份外掛字幕（主要語言＋中英雙語），不保留來源 MKV 內嵌字幕；組合 2 是兩份外掛字幕（主要語言＋純中文），要求來源無內嵌字幕。若要保留原片已有的內嵌中文／雙語字幕並只新增英文，請使用工作模式 2／3／4，並依需求選擇 Default 規則。


十三、v2.4.2-beta 單一影片軌 Default 旗標修正
------------------------------------------
- 修正 MP4 來源只有一條影片軌時，來源 mkvmerge -J 未回報 default_track（None），而輸出 MKVToolNix 明確回報 default_track=True 所造成的誤判。
- 僅允許「來源恰好一條影片軌、來源旗標缺失、輸出唯一影片軌為 Default Yes」這個狹窄情況；記錄為單軌預設資訊正規化。
- 多影片軌、來源明確 Default No、輸出 Default No、音訊與字幕 Default 旗標仍嚴格比對，不放寬其他軌道。
- FFprobe disposition 只有在 MKVToolNix metadata 同時證實上述單影片軌正規化時，才允許 video default 的表示差異；audio／subtitle 仍嚴格檢查。
- 針對本次 Shameless S01E01／S01E02 日誌中的 `default_track` 差異新增回歸測試。通過 50 項單元測試與 Python 語法檢查。
- 建置環境沒有 MKVToolNix；仍需在使用者 Mac 使用原本失敗的 1～2 集實測。


十四、v2.4.3-beta 單一影片軌 Forced 旗標修正

v2.4.3-beta 修正 MP4 來源只有一條影片軌時，mkvmerge -J 可能沒有回報 Matroska Forced 旗標（None），而重新封裝後 MKVToolNix 會明確回報 Forced No（False）。這是來源容器未提供此欄位與 MKV 輸出明確寫入預設 False 的表示差異。

只有在來源與輸出各恰好一條影片軌、來源 forced_track 為 None、輸出 forced_track 為 False 時才允許；輸出 Forced Yes、來源明確 Forced No 變成 Yes、多影片軌、音訊或字幕 Forced 旗標變化仍會驗證失敗。此修正不放寬字幕 Forced／Default 或音訊設定保護。

本次新增 7 項回歸測試；完整測試套件 57 項通過。真實 MKVToolNix 端到端測試仍需在 macOS 上執行。


十五、v2.4.4-beta 來源容器 CodecID 正規化
-----------------------------------------
v2.4.4-beta 修正 MP4 等非 Matroska 來源在 mkvmerge -J 中可能沒有 Matroska CodecID（例如來源 properties.codec_id=None），但重新封裝為 MKV 後，MKVToolNix 會依實際影片編碼指定 V_MPEGH/ISO/HEVC 或 V_MPEG4/ISO/AVC 等 Matroska CodecID，造成舊版逐字比較誤判。

僅在以下條件全部成立時接受這種差異：來源 CodecID 缺失、輸出 CodecID 已存在、來源與輸出的 mkvmerge 人類可讀 codec 描述完全相同，而且輸出 CodecID 屬於已知且與描述相符的影片編碼配對（本版涵蓋 HEVC、AVC、AV1、VP9、MPEG-2、Theora）。來源若有明確 CodecID，任何變更仍會拒絕；未知 CodecID、描述不符、音訊／字幕 CodecID 缺失變化也不放寬。

此正規化不代表略過編碼驗證：後續 FFprobe 仍會嚴格比較來源與輸出的 codec_name、Profile、解析度、pixel format、色彩資訊及其他可用屬性；同時保留封包數比較與完整 FFmpeg 影片／音訊解碼驗證。


十六、v2.4.5-beta 來源影片預設顯示尺寸正規化
-----------------------------------------------
v2.4.5-beta 修正 MP4 來源在 mkvmerge -J 中沒有提供 `display_dimensions`（None），而 MKV 輸出將預設顯示尺寸明確回報為 `1920x1080` 時，舊版逐字比較造成的誤判。

只有在來源與輸出各恰好一條影片軌、來源沒有 `display_dimensions`、來源與輸出的 `pixel_dimensions` 完全相同，而且輸出的 `display_dimensions` 也恰好等於該 `pixel_dimensions` 時，才把輸出尺寸視為 MKVToolNix 補上的預設像素顯示尺寸。來源與輸出像素尺寸不同、輸出顯示尺寸不是像素尺寸（例如非方形像素／非預設顯示比例）、來源原本有明確顯示尺寸，或存在多條影片軌時，仍嚴格拒絕。若相同條件下來源未提供 `display_unit`、輸出明確寫入 Matroska 預設值 0（pixels），才允許該配套正規化；其他顯示單位仍嚴格比較。

FFprobe 另外新增 `sample_aspect_ratio` 與 `display_aspect_ratio` 的逐串流嚴格比較，並繼續比對 codec、profile、寬高、像素格式、色彩及其他既有欄位。此修正不放寬影片解析度、畫面比例或實際編碼檢查。


十七、v2.4.6-beta 影片 DefaultDuration 影格時序正規化
--------------------------------------------------
v2.4.6-beta 修正 MP4 等非 Matroska 來源未提供 `default_duration`（None），而重新封裝後 MKVToolNix 在影片軌寫入 `41708333` ns 等 Matroska 預設影格時長時，舊版逐欄位比較造成的誤判。這個欄位不能直接忽略，因為錯誤的 DefaultDuration 可能影響影格時間解讀。

只有在來源與輸出各恰好一條影片軌、來源 `default_duration` 缺失、輸出為正整數時才啟動例外驗證。程式會重新讀取來源及輸出的 FFprobe 影片封包時序，並要求：
- 來源與輸出 `avg_frame_rate`、`r_frame_rate` 四者完全相同且非零，並且 avg／r 相同，排除可辨識的 VFR／不明確影格率。
- 輸出 `default_duration` 必須等於該固定影格率換算的奈秒值，最多容許 1 ns 的整數四捨五入差異。
- 來源與輸出影片封包數一致；每一個封包都必須有可讀的 PTS 與 duration。
- 逐封包 duration 必須符合固定影格時長，來源與輸出相互一致；每一個封包相對於首個封包的 PTS 也必須一致。考慮 Matroska 常見 1 ms 時間戳刻度，最多容許較粗時間基底的一個 tick；若時間戳精度粗於 1 ms，則拒絕正規化。

任何 VFR／影格率不一致、封包數不同、封包時長不同、PTS 偏移超過容許值、時間戳缺失或時序資料無法讀取，都維持驗證失敗。其他軌道、音訊／字幕封包、色彩、解析度、畫面比例、章節、附件與完整 FFmpeg A/V 解碼檢查不放寬。

本版新增 10 項 DefaultDuration 回歸測試；完整測試套件 81/81 通過。另以合成 24000/1001 CFR MP4 經 FFmpeg stream-copy 產生 MKV，使用 FFprobe 實際讀取 72 個封包並通過時序比較。這是 FFmpeg／FFprobe smoke test，並非真實 MKVToolNix 測試；仍需在使用者 Mac 以原始 Shameless S01E01 完成 mkvmerge 端到端驗證。


十八、v2.4.7-beta H.264／HEVC 前導封包差異驗證
------------------------------------------------
v2.4.7-beta 延伸 v2.4.6 的 DefaultDuration 驗證，處理 MKVToolNix 可能不保留第一個 keyframe 之前的 H.264／HEVC 前導封包。這些封包可能存在於來源 MP4 的 packet index，但不一定能產生可解碼畫面；MKVToolNix 的 H.264／HEVC 解析器有丟棄第一個 keyframe 之前影格的既有行為。

程式不會因為「輸出封包比較少」就自動放行。必須同時符合以下條件：
- 來源與輸出影片 codec 相同，且為 H.264 或 HEVC。
- 來源少掉的封包數，必須剛好等於來源第一個 keyframe 前的完整前綴數量。
- 輸出第一個影片封包必須是該 keyframe；若差額不是完整前綴，仍然拒絕。
- 使用 FFmpeg 對來源與輸出完整解碼並產生 framemd5；可解碼影格數、每一幀 MD5、順序、相對 PTS 與 duration 必須全部一致。
- 若來源／輸出 framemd5 使用相同 CFR 時基，影格時間必須精確一致；若時基不同，只有雙方量化精度均不粗於 1 ms 時，才容許最多 1 ms 誤差。
- 之後仍逐封包比較保留部分的 duration 與相對 PTS，並要求 DefaultDuration 精確符合 CFR 影格率。

這項例外只取代已證明可解碼影格完全一致時的影片封包數相等檢查。音訊封包數仍嚴格逐軌比對；影片影格 MD5 不一致、影格數不同、PTS／duration 改變、VFR、缺少時序資料、非前導封包差異、非 H.264／HEVC 或其他條件不符合時，均會停止該集並移入 Failed。

逐影格 framemd5 輸出直接保留在記憶體，不會建立影格圖片或額外影片檔；但會額外完整解碼來源與輸出影片，增加讀取與 CPU 時間。

本版通過 90/90 單元測試，並以合成 HEVC 24000/1001 MP4→MKV 實際比對 72 幀。建置環境未安裝 MKVToolNix，因此仍需在 macOS 以原始 Shameless S01E01 測試實際 v103.0 行為；第一次只處理一集，確認最終影片、音訊、字幕旗標及 `.en` 旁掛檔後再批次處理。


十九、v2.4.8-beta 修正前導封包對應的 framemd5 比較
------------------------------------------------
v2.4.7 在 Shameless S01E01 的實際測試中，FFprobe 顯示來源／輸出影片封包為 82681／82616，差額 65；但 FFmpeg 完整解碼清單也分別回報 82681／82616 個影格。v2.4.7 直接要求兩份完整清單數量相等，因此在進入逐影格 MD5 比較前就會拒絕，沒有驗證封包前綴之後的畫面。

v2.4.8 修正此驗證順序：
- 先獨立判定來源與輸出是否符合相同 H.264／HEVC、來源封包差額正好等於來源第一個 keyframe 前的封包數，以及輸出第一個封包為 keyframe。其他封包差異不會啟用例外。
- 候選前綴通過後，FFmpeg 仍完整解碼來源及輸出，但比較時只略過來源解碼清單最前面的相同數量記錄；來源剩餘影格數必須剛好等於輸出影格數。
- 所有剩餘影格均必須逐幀 MD5 相同，順序、相對 PTS 及 duration 也必須符合原本時間精度限制。若略過數量不符、剩餘影格數不同、任一 MD5 不同或時間不一致，仍安全停止。
- `compare_implicit_default_duration_timing` 仍會再次執行完整 CFR、第一個 keyframe 位置、封包 duration、相對 PTS 與 DefaultDuration 檢查；音訊封包數及其他 metadata 檢查沒有放寬。

這次修正針對 v2.4.7 的比較順序缺陷，不代表已確認使用者原檔的 65 個前導封包全部不可解碼。真正的 Mac／MKVToolNix v103.0 測試仍以剩餘影格逐幀一致及全流程驗證通過為必要條件。


二十、v2.4.9-beta 單一音訊軌 Forced 預設值正規化
-----------------------------------------------
v2.4.9-beta 修正 MP4 來源只有一條音訊軌、來源 `mkvmerge -J` 未提供 Matroska `forced_track`（None），而重新封裝為 MKV 後 MKVToolNix 明確回報 `forced_track=False` 時的跨容器表示差異。

僅在來源及輸出各恰好一條音訊軌、來源 Forced 旗標缺失、輸出明確為 Forced No（False）時接受。這是單音訊軌下 Matroska 預設 Forced No 的狹義正規化。輸出 Forced Yes、來源原本明確有 Forced 值、多音訊軌、字幕 Forced 旗標變化仍然拒絕。

本修正不放寬音訊語言／LanguageIETF、軌道名稱、Default、Enabled、CodecID、codec 描述、聲道數、取樣率、位深、FFprobe stream metadata、音訊封包數或完整影音解碼。

新增單音訊軌 None -> False 接受、None -> True 拒絕、明確 False -> True 拒絕、多音訊軌拒絕及字幕 Forced 仍嚴格等回歸測試。真實 MKVToolNix v104.0 與使用者 Shameless S01E01 的完整端到端結果仍需在使用者 Mac 上確認。


二十一、v2.4.10-beta 單一 AAC 音訊軌 CodecID 正規化
----------------------------------------------------
v2.4.10-beta 修正 MP4 來源只有一條 AAC 音訊軌時，來源 `mkvmerge -J` 未提供 Matroska `codec_id`（None），但重新封裝後輸出 MKV 明確回報 `A_AAC` 的跨容器表示差異。

僅在來源與輸出各恰好一條音訊軌、來源 CodecID 缺失、輸出恰為 `A_AAC`、兩邊 mkvmerge codec 描述完全相同，且該描述明確包含 AAC 時接受。其他音訊 CodecID、非 AAC 描述、描述改變、來源原本有明確 CodecID、多音訊軌均拒絕。

本修正不放寬 FFprobe 的 `codec_name`、Profile、取樣率、聲道數、channel layout、位深比對，也不放寬音訊封包數、完整影音解碼及其他音訊／字幕 Metadata。

新增回歸測試：單 AAC 音訊軌 None -> A_AAC 接受；非 AAC CodecID、錯誤描述、來源明確 CodecID 變更、多音訊軌及聲道改變均拒絕。真實 MKVToolNix v104.0 與使用者 Shameless S01E01 完整端到端結果仍需在使用者 Mac 上確認。


二十二、v2.4.11-beta 單一 AAC 音訊軌 Matroska BitDepth 正規化
-----------------------------------------------------------
v2.4.11-beta 修正 MP4 來源 AAC 音訊軌由 mkvmerge -J 回報 `audio_bits_per_sample=16`，但輸出 MKV 不提供 `audio_bits_per_sample`（None）時的跨容器表示差異。Matroska RFC 9559 將 Audio BitDepth 定義為每樣本位元數，並註明主要用於 PCM；AAC 是壓縮音訊，Matroska 軌道不一定會寫入該欄位。

僅在來源與輸出各恰好一條音訊軌、來源 MP4 欄位恰為 16、輸出欄位缺失、來源 Matroska CodecID 缺失、輸出 CodecID 恰為 `A_AAC`，且來源／輸出 mkvmerge codec 描述相同並明確為 AAC 時，才接受此容器欄位差異。其他數值、來源有明確 CodecID、非 AAC、多音訊軌，或輸出明確回報其他 BitDepth 均維持拒絕。

這不代表忽略實際音訊位深：FFprobe 的 `codec_name`、Profile、`sample_rate`、`channels`、`channel_layout`、`bits_per_sample` 及 `bits_per_raw_sample` 仍逐欄位嚴格比較；音訊封包數仍須完全相同，輸出仍須通過完整 FFmpeg 音訊／影片解碼。

二十四、v2.4.13-beta FFmpeg 字幕解析 pipe 輸出修正

v2.4.13-beta 修正文字字幕驗證將 ASS muxer 寫入 /dev/null 時的假成功。FFmpeg 9.0.2 在 macOS 上可能因 /dev/null 已存在而顯示「File already exists」及「Error opening output file」，但仍回傳退出碼 0，舊程式因此誤判字幕解析成功。現在改用 `pipe:1`，並將 stdout 丟棄於程序管線，不建立臨時字幕檔；另對明確致命 stderr 訊息作第二層拒絕。SubRip 與 ASS 文字字幕均加入實際 FFmpeg pipe smoke test。此修正不放寬字幕軌數、字幕事件抽取、影音完整解碼或音訊／字幕封包數驗證。

二十三、v2.4.12-beta FFprobe 音訊封包摘要 Stream Index 修正
---------------------------------------------------------
v2.4.12-beta 修正來源音訊封包摘要被錯誤篩成 0 軌的問題。原本 `ffprobe_packet_summary()` 使用 `-show_entries stream=codec_type,codec_name,nb_read_packets`，沒有要求 FFprobe 回傳 `stream.index`；但後續音訊語言白名單篩選會以來源 FFprobe 全域 stream index 選出保留音訊。因摘要內 index 為 None，單一音訊軌（例如 MP4 中 video index=0、audio index=1）會被全部排除，最後錯誤顯示「來源 0 軌、輸出 1 軌」。

本版將查詢欄位修正為 `stream=index,codec_type,codec_name,nb_read_packets`，並保留全域 index。正式封裝與追加字幕驗證都使用同一摘要函式，因此兩處來源音訊篩選同步修正。修正只補齊正確的 stream index，沒有跳過或放寬任何封包數比對；來源選中的音訊軌仍必須與輸出逐軌 codec 及封包數一致。

新增 3 項回歸測試：確認 FFprobe 查詢要求 index、確認單音訊來源的 index=1 不會被錯誤排除，以及確認多音訊來源在語言篩選後可正確選中原始全域 index 對應的保留軌。完整單元測試 114/114 通過，Python 語法檢查通過。真實 MKVToolNix v104.0 與使用者 Shameless S01E01 完整端到端結果仍需在使用者 Mac 上確認。

