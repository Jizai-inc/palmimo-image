# 顔ディスプレイ v2 のアセットと FW の切り替え

顔ディスプレイの FW v2 は表情や絵文字を持たず、platform が書き込んだ**アセット**（画像・GIF・Lottie から変換したベクタ）を再生する。
この文書は platform 側の約束を定める: アセットの形式と変換器、標準アセットの置き場、Flash への同期、旧 FW（v1）と新 FW（v2）の切り替え。
FW とホストの通信は palmimo-devkit の `doc/reference/display-protocol.md`、アプリのアセットの宣言は同じく `doc/reference/app-manifest.md`
（schema 2）が正本で、ここでは繰り返さない。

## 置き場

| パス | 中身 | 所有 |
|---|---|---|
| `/usr/local/lib/palmimo/display-firmware/firmware.json` | 同梱する 2 つの FW の宣言（下記） | root、bundle の `owns.files` |
| `/usr/local/lib/palmimo/display-firmware/face_serial_v1.bin.gz`・`face_serial_v2.bin.gz` | FW バイナリ | 同上 |
| `/var/lib/palmimo/display-firmware/desired.json` | いま望む FW（下記） | root。書くのは `desire` サブコマンドだけ |
| `/var/lib/palmimo/display-firmware/state.json` | 既存の書き込みの記録。連続失敗は**望む FW ごと**に数える | root |
| `/usr/local/share/palmimo/display-assets/catalog.json` | SDK・エージェント向けの語彙の目録。標準集合の部分集合（形はアプリの `/run/palmimo/apps/<id>/assets.json` と同じ。id・説明・タグ・パラメータの説明・翻訳。種類と次元は書かない＝実機の `ASSET INFO` が正本） | root、読み取り専用。アプリから読める |
| `/usr/local/share/palmimo/display-assets/<name>.bin` | 標準集合の既定の中身（変換済み。system.* を含む） | 同上 |
| `/usr/local/share/palmimo/display-assets/source/<name>.json` | プレビュー用の元の Lottie。標準の `.bin` と名前が 1 対 1。変換や同期には使わない | root、bundle の `owns.files`。誰でも読める |
| `/var/lib/palmimo/display-assets/overrides/<name>.bin` | 利用者が Portal で差し替えた中身（変換済み） | root。書くのは `assets override` だけ。アプリには見せない |
| `/var/lib/palmimo/display-assets/apps/<app ID>/<name>.bin` | アプリのアセット（インストール時に変換） | root。Portal が書くのは `assets replace`。手動の `assets install` も可 |
| `/var/lib/palmimo/display-assets/launch.json` | アプリの起動の通し番号（古い順に外すため。時計は使わない） | root。`assets sync --app` が増やす |

**標準集合**は `/usr/local/share/palmimo/display-assets/` 直下の有効な `<name>.bin` の通常ファイル全体。
名前は `^[a-z0-9][a-z0-9._-]{0,39}$`（`include` は不可）、id は `palmimo/<name>`。
symlink を辿らず、規則外の名前や通常ファイルでない `.bin` が混ざっていれば検査を失敗させる。
`catalog.json` はこの集合のうち SDK・エージェントに提供する語彙・説明・翻訳の一覧で、全体の在庫には使わない。
現在は標準集合が表情 14 種と `system.idle`・`system.sleep`・`system.wake` の **17 個**、目録は表情の **14 個**。
system.* は標準集合に含め、目録には入れない。

Portal は差し替え画面の名前をこの公開ディレクトリの `.bin` 一覧から直接列挙する（symlink は辿らず上と同じ規則で検査。名前の事前照合もこの一覧を使う。差し替えの有無・大きさ・容量の情報は root の `assets status` を使う）。
system.* の表示名は Portal 側で持つ。SDK の語彙は引き続き catalog.json を読む。

## 変換器

`/usr/local/libexec/palmimo/palmimo-display-assets`（Python、root でなくても動く）。

```
palmimo-display-assets convert <入力> -o <出力.bin> [--json]
palmimo-display-assets inspect <.bin> [--json]
```

- 入力の種類は拡張子と中身で決める: Lottie（`.json`）→ SPAF、GIF → PIMG、PNG・JPEG → PIMG（1 フレーム）。1 フレームの GIF も静止画（kind `image`、遅延0）とし、複数フレームだけ kind `gif` とする。
- 失敗は終了 2 と、`--json` なら `{"ok": false, "errors": [{"code", "path", "message"}]}`。Lottie の非対応機能は、レイヤー名・機能・JSON のパスを
  **すべて**並べる（最初の 1 つで止めない）。ただし資源の上限超過と共有塗りの幾何判定の失敗は即時停止する。JSON は読み込み中に 16 MiB・100 万トークン（コンテナ・文字列・スカラー）・深さ 64 を検査し、デコード前に拒否する。キー数（累計 2048）・Track 数（256）・成分数（144）は組み立て中に検査する。
- 複数の形が1つの塗りを共有するとき、同じグループ座標系で全キーフレームの始点・終点とBezier制御点を包む外接矩形が互いに離れていれば、別々のDrawを維持する。形のSlotや異なるグループ座標系は、この分離を保証できないため拒否する。
  外接矩形が重なる場合は、静的な閉じた直線パスと角丸なしの四角だけを検査する。自己交差・接触・輪郭間の交差を拒否し、nonzero（`r: 1`、既定）で塗りの有無が変わる境界、またはevenodd（`r: 2`）の全境界を往復する直線ブリッジで1パスにする。24頂点を超える結果、曲線、開いたパス、アニメーション、対象Draw間に別の塗り・線が挟まる場合の統合は拒否する。
- グラデーション塗り `gf` は線形（`t: 1`）・円形（`t: 2`）に対応する。共有塗りと塗り規則 `r` は単色と同じ。
  `g.p = n` は色の段の数（1〜16）。`g.k` は `[位置,r,g,b] × n` の後に `[位置,透明度] × m`（m ≥ 0）を並べ、全体を144成分以下にする。
  値はすべて0〜1、色と透明度それぞれの位置は各キーで非減少（同じ位置も可）。全キーの成分数は同じにする。
  `s`・`e`・`g.k` はキーフレーム可。`s`・`e` は2成分として保存し、位置と同じく3成分の入力も受け付ける（zは保存しない）。
  座標は `gf` を宣言したグループのNodeを基準とし、内側の形への継承でも基準を保持する。円形は `s` が中心、`s` から `e` までの距離が半径。
  段の位置の外側は端の値で延ばす（pad）。色はsRGB値で線形補間し、透明度は独立に補間する。塗りの `o` とNodeの不透明度を全体に掛ける。
  `gs`、`h` が静的0以外のハイライト、`g`・`g.k`・`s`・`e` の `sid`、段の数・長さ・値・順序の不正は集約エラーで拒否する。
- 成功は `{"ok": true, "kind": "vector|image|gif", "bytes": <int>, "crc32": "<8 桁の小文字 16 進>", "params": {"<sid>": {"dim": <int>}}, "warnings": [{"code", "message"}]}`。
  `inspect` は同じ形で既存の `.bin` を検査する（`convert` の結果を root 側で検査し直すのに使う）。
- 警告: GIF のコマ間隔が約 30fps より遅い（高コントラストの動きがちらついて見える）、画像を標準アセットの差し替えに使う
  （パラメータが効かなくなる）など。Portal が利用者に見せる。
- 複数フレームの GIF の各フレームの遅延は、パネルの書き換え（61.5Hz、1 回 16.24ms）の回数に直して PIMG に書く: `max(1, round(遅延 ms / 16.24))`。
  GIF の遅延が 0 または指定なしのフレームは 10ms とみなす（一般のブラウザと同じ扱い）。

## 形式

全形式とも little-endian。先頭 4 バイトの識別子で種類を見分ける。FW は受け取ったときと起動時に全体を検査し、通らないものは使わない。

### PIMG v2（画像・GIF）

v1（旧 FW の画像スロットの形式）との違いはフレームごとの表示時間だけ。GIF はフレームごとに遅延が違ってよいので、v2 はそれをフレーム表に持つ。

| 位置 | 中身 |
|---|---|
| ヘッダ 32B | `"PIMG"`、u16 版 = 2、u16 画像数（= 1）、u32 全長、u32 CRC-32（[32, 全長)）、予約 16B |
| エントリ 32B | 名前 20B（NUL 埋め、`[A-Z0-9_]`）、u16 フレーム数（≤ 64）、u16 予約（0）、u32 フレーム表の位置、予約 4B |
| フレーム表 12B × フレーム数 | u32 RLE の位置（2B 境界）、u32 語数、u32 このフレームを出す**パネルの書き換えの回数**（1 以上。静止画は 0）。ms ではなく回数で持つので、書き換えの速さが変わっても丸めの誤差が出ない |
| RLE | 480×480 RGB565 の語単位 RLE。制御語 `c` が `c & 0x8000` なら続く 1 語を `(c & 0x7fff) + 1` 回、それ以外は続く `c + 1` 語をそのまま |

### SPAF v2（Lottie から変換したベクタ）

固定長のレコードとオフセットで並べ、FW は Flash 上のまま（RAM に写さずに）読む。

| 位置 | 中身 |
|---|---|
| ヘッダ 80B | `"SPAF"`、u32 版 = 2、u32 種類 = 1、u32 全長（4 の倍数、≤ 1MiB）、u32 CRC-32（16〜19 を 0 とみなした全体）、f32 fps・ip・op、Node・Draw・Track・Key・Slot・payload の位置と件数（各 u32 2 つ） |
| Node 40B | i32 親（-1 = 根）、i32 Track×5（位置・基準点・拡大・回転・不透明度）、f32 ip・op・開始・伸縮 |
| Draw 68B | 先頭44B: u32 Node・種類（0 楕円・1 角丸四角・2 パス）・頂点数・閉じているか・塗りか、i32 形の Track×3・色・不透明度・線幅。残り24Bは下表 |
| Track 16B | u32 次元・Key 数・先頭の Key、i32 Slot（-1 = 無し） |
| Key 32B | f32 フレーム、u32 hold、u32 ベクトルの位置×6（値・終わり・ease の out x/y・in x/y） |
| Slot 12B | u32 名前の位置・名前の長さ（NUL 込み）・次元 |
| payload | float32 のベクトルと slot 名。同じベクトルは共有してよい |

Drawの追加フィールド（Draw先頭からの位置）:

| 位置 | 型 | 中身 |
|---|---|---|
| 44 | u32 | 塗りの種類: 0 単色、1 線形、2 円形 |
| 48 | u32 | 色の段の数 n（単色は0、グラデーションは1〜16） |
| 52 | i32 | 始点Track（dim 2。単色は-1） |
| 56 | i32 | 終点Track（dim 2。単色は-1） |
| 60 | i32 | 段Track（dim D = 4n + 2m ≤ 144。単色は-1） |
| 64 | i32 | グラデーション座標系のNode（単色は-1） |

FWと `inspect` はv1を版のエラーで拒否する。単色は追加フィールドが上表の初期値であることを要求する。
グラデーションは塗りが1、有効な座標系Node、始点・終点・段の有効なTrackとslot = -1を要求する。
始点・終点は±1024、段は全キーの始点・終点で0〜1を検査する。段の位置の非減少は変換器が入力の各キーで検査し、
FWと `inspect` は要求しない（成分別のイージングで補間中に順序が交差しうるため）。
グラデーションも有効な色Track（変換器は定数 `[1,1,1,1]` を出力）を持ち、色のalphaを全体の不透明度に掛ける。

上限: 8 Draw、32 Node、深さ 8、1 パス 24 頂点、256 Track、2048 Key、32 Slot、1 Track 144 成分。値の範囲（拡大 ±200%、位置 ±1024、線幅 0〜128、
色 0〜1、不透明度 0〜100、ease の制御点 0〜1）を超えるものは変換器が断る。光は Lottie のグラデーションの塗り（`gf`）で書く。レイヤー名の `#glow` は使えず、変換エラーになる。
重ねた stroke は 1 本ずつ Draw にし、合計 8 Draw を超えると変換エラーになる。
Draw は Lottie と同じく塗り・線ごとにまとめ、その塗り・線が掛かるすべての形を描いてから次へ進む。
単色の塗り・グラデーションの塗り・線はいずれも、各リストを末尾から先頭へ、子グループを深さ優先でたどった登録順になる。
各塗り・線の中でも形はこの走査順に並ぶ。親の塗り・線は子グループの形にも掛かり、子固有の塗り・線より先に描く。
レイヤーは末尾から先頭の順。共有の塗りは対応可能な輪郭を結合し、順序や上限を保てない入力は変換エラーになる。
`convert` と `inspect` の JSON 出力では、`draws.fill` と `draws.stroke` にそれぞれの Draw 件数を返す。

## FW の切り替え

`firmware.json`（schema 2。`schema` キーが無い平らな `{"version", "sha256"}` は schema 1 ＝今の道具の形で、新しい道具は schema 1 を受け付けない。
新しい道具と schema 2 の宣言は同じ bundle で入れ替わるので、混在は起きない）:

```json
{"schema": 2, "firmware": {"1": {"version": "0.1.3+…", "sha256": "…", "file": "face_serial_v1.bin.gz"},
                           "2": {"version": "2.0.0+…", "sha256": "…", "file": "face_serial_v2.bin.gz"}}}
```

`desired.json`: `{"schema": 1, "protocol": 1 | 2, "by": "app-start" | "initial"}`（時刻は持たない。RTC が無いため）。

- 起動時の unit・platform 更新の段・手動の書き直しは、どれも `desired.json` の `protocol` の FW へ収束させる（`ensure`）。
- **`desired.json` が無いとき、`ensure` は何も書かない**（`{"result": "undesired"}`）。起動時の unit は Portal より前に動き、autostart の情報を持たないため。
- `desired.json` を書くのは `palmimo-display-firmware desire <1|2> --by <app-start|initial>`（root、Portal が sudo で呼ぶ）だけ。
  Portal は起動時と platform 更新の段で、`desired.json` が無ければ初期値（preempt で止めたアプリの要求 ＞ 動いている motor_display アプリの要求 ＞ autostart の先頭の要求 ＞ 2。要求の無いアプリは飛ばす）を `--by initial` で書いてから `ensure` する。
  アプリの起動の判定では `--by app-start` で書く。`desire` の出力は `{"protocol": <int>, "changed": <bool>}`。
- アプリの要求は、アプリの環境にインストールされた palmimo-sdk の `display_firmware.json` をファイルとして読む（devkit の
  `display-protocol.md`「Firmware selection」）。

`ensure` は機器が無い場合、初めて確認する機体でも過去に顔を確認した機体でも absent・終了 0 を返す。
前者の reason は null、後者には USB 接続や復旧を案内する reason を付ける。before/after は null。
顔が無いことを起動の失敗とせず、起動時の書き込み失敗回数も増やさない。

## Flash の領域

| 範囲 | v2 の用途 |
|---|---|
| 0〜2MiB（`0x000000`〜`0x200000` 未満） | FW。バイナリは 2MiB 未満 |
| 2MiB〜16MiB−8KiB（`0x200000`〜`0xffe000` 未満） | アセット本体。末尾から先頭へ詰める連続領域 |
| 16MiB−8KiB〜16MiB（`0xffe000`〜`0x1000000` 未満） | 目録 2 面、各 4KiB |

v1 の FW バイナリの上限は 10MiB 未満。v2 の領域内には旧 FW 用目録の穴を設けない。
アセット本体の合計占有量が 6MiB−8KiB 以下なら 10MiB より後ろに収まり、v1 への切り替えでも残る。
それを超えて手前に置いた分は v1 の書き込みで失われることがあり、v2 の起動時 CRC 検査で除外し、SD から同期し直す。
v1 が旧目録の位置へ画像を書いた場合も同様に検査・再同期する。
既存データの移行は行わない。本体の相対位置の基準が変わるため、起動時の検査で除外して同期し直す。
変換器の 1 アセットの上限（`MAX_FILE`）は 6,275,072 バイトのまま。

## アセットの同期

`palmimo-display-firmware assets sync [--app <app ID>] --json`（root）。

- `display.lock` を**待たずに**取る。取れなければ `{"result": "busy"}`。FW が v2 でなければ `{"result": "skipped"}`。
- Flash に置くべき集合 = 上記の標準集合全体（目録に無い system.* も含み、差し替えがあればその中身）＋ `--app` のアプリのアセット。`ASSET LIST` の CRC と比べて、違う・無いものを `ASSET PUT`。
- 場所が足りなければ、他のアプリのアセットを `launch.json` の古い順に `ASSET DEL`。望む集合に無い `palmimo/*` は削除する。それでも連続した空きが足りなければ、標準アセット以外を全部消して入れ直す。まだ入らなければ、最後の手段として標準を含む全アセットを消して入れ直す（その間は内蔵画面へ戻る）。再配置の開始と各 DEL・PUT の経過を標準エラーへ出す。全件の再配置後も NOSPACE なら `reason` は `insufficient_contiguous_space_after_repacking`。
- 結果は `{"result": "synced" | "busy" | "skipped" | "failed", "put": [...], "deleted": [...], "reason"}`。経過は標準エラーに 1 行ずつ。
- 容量の条件は Portal が先に検査し、`replace`・`override` も保存時に検査する。同期でも守られていなければ `failed`。保存時の不足は `{"ok": false, "reason": "...", "short_by": <不足バイト>}`・終了 1。
  - 使える量: 14,680,064（2MiB〜16MiB）− 8,192（末尾の FW の目録 2 面）＝ **14,671,872 バイト**。
  - 1 アセットの占める量: `ceil(bytes / 4096) × 4096`。
  - 条件: 標準集合全体（system.* と差し替えを反映し、目録の件数で絞らない）の占める量の和 ＋ アプリ 1 つのアセットの占める量の和 ＋ その中で一番大きいアセット 1 個の占める量 ≤ 14,671,872。
  - FW の目録にも同じ集合が収まること。`palmimo/<name>`・`<app ID>/<name>` の完全な ASCII ID ごとに `align4(13 + ID のバイト長)` を加算し、合計 ≤ 4,076 バイト（4,092 − ヘッダ16）、件数 ≤ 254。目録は同じ ID の更新で項目を置き換えるため、本体のような最大1個分の余裕は不要。
  - `replace`・`override`・同期の望む集合で本体と FW 目録の両条件を検査する。`override` は各インストール済みアプリとの組み合わせを検査する。目録不足の reason は `Assets exceed display directory capacity`、保存時の `short_by` は不足バイト（件数超過は超過1件につき最小項目長16バイト）を返す。同期では DEL・PUT の前に `failed` にする。

store の `.lock` は `status`・`replace`・`uninstall`・`override`・`install` が最大 120 秒待つ。
0.1 秒おきに非ブロック flock を試し、期限は monotonic で測る。待ち切れなければ `{"ok": false, "reason": "Asset store is busy"}`・終了 75。
`sync` は display.lock と store ロックの両方を非ブロックで取り、どちらも待たずに busy・終了 75 を返す。

### 保存の入口（root、Portal が sudo で呼ぶ）

- `assets replace <app ID> <dir>`: Portal の staging ディレクトリの直下を、すべて有効な `<name>.bin` の通常ファイルとして symlink を辿らず読み、全件 inspect する。標準集合全体＋候補集合の容量条件も検査し、不合格なら現在の集合を変更しない。
  保存前に store ロックを待って取り、容量はロック内でも再確認する。`apps/.replace-<app ID>/` に全件を原子的に保存して fsync → 旧集合があれば `.old-<app ID>` に rename → 新集合を `apps/<app ID>` に rename → 親を fsync → 旧集合を削除する。空の候補はアプリの集合を削除する。
  2 回の rename の間で中断された場合、次の replace・uninstall・status の冒頭で `.old-<app ID>` を元へ戻し、旧集合に戻る。公開済みの集合があれば `.old-*` は削除し、`.replace-*` は常に捨てる。作業用の名前は status のアプリ一覧には出さない。sync は指定された `apps/<app ID>/` だけを読む。
- `assets install <app ID> <name> <変換済みの .bin>`（手動互換用。Portal は replace を使う）: `inspect` で検査し直してから `apps/<app ID>/<name>.bin` に置く。
- `assets uninstall <app ID>`: そのアプリの分を消す（Flash の分は次に場所が要るときに外れる）。
- `assets override <name> <変換済みの .bin>` / `assets override --reset <name>`: 標準集合の既定 `.bin` がある名前を差し替え／標準に戻す。目録への掲載は条件にせず、system.* も受け付ける。差し替え・reset とも、候補（reset は既定の中身）を反映した標準集合＋インストール済みの各アプリについて、本体と FW 目録の容量をロック内で検査する。アプリが無ければ標準集合だけを検査する。不足時は `ok:false` と最大の不足量を `short_by` で返し、現在の差し替えを残す。標準集合から消えた孤児の名前の reset は容量検査なしで削除できる。
- 上の保存操作の出力は `{"ok": <bool>, "reason"}`。Portal は変換器を user で動かし、結果の `.bin` をこれらに渡す。

### 保存内容の照会（root）

`assets status [--json]` は store ロックを待ち、以下の 1 行 JSON を返す（成功は終了 0、検査や読み取り失敗は ok:false・終了 1、ロック期限は終了 75）。USB には触れない。

```json
{"capacity":14671872,"standard":[{"name":"happy","bytes":12345,"kind":"vector","params":["eye_color"],"overridden":false,"default_bytes":12345,"default_kind":"vector","default_params":["eye_color"]}],"apps":{"zip.example":{"bytes":20480,"largest":16384,"names":["bye","welcome"]}},"orphan_overrides":["removed"]}
```

standard は標準集合の名前順。bytes/kind/params は有効な差し替えを反映し、default_* は既定の .bin。params は inspect で得る sid の一覧。
apps の bytes は 4KB 単位の占有量の和、largest は最大の 1 個（空集合は 0）、names は名前順。
orphan_overrides は overrides に残り、現在の標準集合には無い名前。apps の直下の `.` で始まる作業用項目は一覧から除外する。
各アプリの中は通常の `<name>.bin` だけを認め、symlink・サブディレクトリ・他の項目があれば失敗とする。

### 同梱物の受け入れ契約

- 全標準 `.bin` と catalog.json が bundle の owns.files に含まれ、すべての `.bin` が変換器の inspect を通る。
- 目録の id は重複せず、標準集合の id の部分集合。目録と全 `.bin` の完全一致は要求しない。
- 現在の同梱物は 17 個の `.bin` と 14 件の目録で、差は `palmimo/system.idle`・`palmimo/system.sleep`・`palmimo/system.wake` の 3 個だけ。
- 同期・override・容量計算は同じ標準集合全体を使う。目録に無い system.* も同期でき、差し替えと reset ができ、容量の合計に入ることを試験する。
