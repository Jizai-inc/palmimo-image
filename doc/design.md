> 本書が前提にする palmimo-portal の設計ドキュメント（機能要件・横断判断・出荷イメージの構成表）は非公開。

# palmimo-image 設計 — 出荷イメージの構成物と適用

palmimo-portal 設計ドキュメント（機能要件・横断判断・出荷イメージの構成表を
持つ）を前提に、**OS に焼く構成物の実体**と
その適用手段を定義する。O3（イメージビルドパイプライン）の入力仕様を兼ねる。

- 本書の範囲: **デバイス構成の実体**（`files/`・unit・comitup 設定・
  firstboot・共有パッチ）と、それを消費する **2 経路** — 開発機への
  手適用（`apply-pi.sh`）／出荷イメージビルド + 焼き込み
  （`pigen/` + `provision_sd.py`）。後者の詳細は
  「pi-gen イメージビルドと焼き込み CLI」のとおり本書の成果物を
  そのまま入力にする
- 実機検証は **構成物・両経路が揃った後に 1 回**（検証計画の節）
- 前提となる決定: Portal は palmimo-portal リポジトリのタグ clone
  （分離、2026-08-20）/ 個体番号は識別ファイル注入で持たせる方式に
  2026-08-20 に改訂（CPU シリアル等からの導出はやめた）/ 検証用タグは
  pre-release（`v*-*`、上書き可）

## 配置

```
palmimo-image/ (このリポジトリ)     -> 出荷イメージビルドの入力
  README.md                        -> 使い方・前提・トラブルシュート
  apply-pi.sh                      -> 手適用スクリプト（開発機から SSH で実行）
  files/                           -> イメージに置くファイルの実体（適用先パスを配下に再現）
    etc/systemd/system/palmimo-portal.service
    etc/systemd/system/palmimo-firstboot.service
    etc/systemd/system/comitup-web.service  -> no-op 置き換え（mask しない）
    etc/polkit-1/rules.d/50-palmimo-portal.rules
    etc/comitup.conf
    etc/NetworkManager/dispatcher.d/50-palmimo-avahi  -> AP->STA 切替後の avahi 再登録（#683）
    usr/local/lib/palmimo/firstboot.sh
  tools/
    make_identity.py               -> テスト用ダミー識別ファイル生成（uv script）
```

- `files/` は「適用先の絶対パスをそのまま再現したツリー」。apply-pi.sh も
  pi-gen ステージも同じツリーを rsync/コピーするだけにして、
  **配置ロジックを 1 箇所に固定**する
- palmimo-devkit リポジトリの開発 push ツールとは独立。互いに参照しない

## palmimo-portal.service（確定契約の実体化）

```ini
[Unit]
Description=Palmimo Portal
# AP モード中も配信するため network-online は待たない。
After=basic.target
# 識別ファイルのマウント前に起動して「識別不能」状態を作らない。
RequiresMountsFor=/boot/firmware

[Service]
User=user
StateDirectory=palmimo
Environment=PALMIMO_ADAPTERS=real
Environment=PALMIMO_PORT=80
AmbientCapabilities=CAP_NET_BIND_SERVICE
# unit が指す venv = Updater の uv 同期が更新する venv（palmimo-portal
# リポジトリルートの .venv）。この一致が update 反映の契約。
ExecStart=/home/user/palmimo-portal/.venv/bin/python -m palmimo_portal
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

決定事項の補足:

- `PALMIMO_PORTAL_DIR` は既定（settings が自リポジトリルートを導出）で
  正しいため設定しない。`PALMIMO_UPDATE_REPO` も既定のまま
- uv は Pi 側 `~/.local/bin/uv`（settings のフォールバック解決に一致）
- ログは journald に集約（`palmimo-portal` unit 名で引ける）。
  ファイルログは持たない

## polkit ルール（sudo なし・最小許可）

`user` に対して次だけを許可する 1 ファイル:

1. comitup の D-Bus メソッド呼び出し（`org.freedesktop.DBus` 経由の
   comitup サービス。実装時に comitup が polkit action を持つか
   bus policy かを実機で確認し、必要な側に書く — T9 で D-Bus 呼び出し
   自体は user 権限で通ることを確認済みのため、追加許可が不要なら
   このルールは logind/systemd 分のみになる）
2. `org.freedesktop.login1.reboot` / `power-off`（電源操作）
3. `org.freedesktop.systemd1.manage-units` のうち unit 名が
   `palmimo-portal.service` または `palmimo-set-wifi-country@*.service`
   （将来分。P2 で unit 追加時に配列へ足す）に一致する start/restart のみ

polkit の manage-units 許可は `action.lookup("unit")` で unit 名を検査する
JavaScript ルールで書く（Debian trixie の polkitd は JS ルール対応）。

## comitup 設定

`/etc/comitup.conf`:

```
ap_name: <hostname>
enable_nuke: true
# ap_password は firstboot が識別ファイルから設定する（個体別のため
# このファイルには書かない。DIY 機 = 識別ファイル不在では未設定のまま
# = comitup 既定のオープン AP）
```

加えて apply/イメージ側で:

- `comitup-web.service` は **no-op unit で置き換える**（`systemctl mask` は
  しない）。comitup の `webmgr.py` は HOTSPOT 状態への遷移で無条件に
  `sd_start_unit("comitup-web.service")` を呼ぶため、mask されていると
  その呼び出しが `DBusException`（unit masked）を投げ、state コールバック
  内で例外が伝播して **残りの HOTSPOT セットアップ（dnsmasq の起動を含む）
  ごと中断する**（実機で確認済み: dnsmasq が上がらずクライアントが
  DHCP リースを取れない状態と journal 上の `DBusException` が同時に
  再現した）。no-op unit（`ExecStart=/bin/true` の oneshot）を
  `/etc/systemd/system/comitup-web.service` に置くと、`sd_start_unit` の
  呼び出し自体は成功して HOTSPOT セットアップが最後まで進む一方、
  同じパスを static unit より優先して読むため **本物の comitup-web が
  port 80 を取ることは構造的に起こり得ない**（Portal を一本化する目的の
  副産物として port 80 保護が手に入る）
- `dnsmasq` パッケージの導入（comitup は自分の dnsmasq プロセスを
  hotspot の DHCP/DNS 用に spawn する（`cdns.py`）が、apt の hard
  dependency ではない。バイナリが無いとクライアントは AP に associate
  はするが DHCP リースを一切もらえない — 実機で確認済み）。ただしシステム
  の `dnsmasq.service` は port 53 を握るため `disable --now` して止める。
  comitup は自分でプロセスを spawn するのでサービス化は不要
- comitup 1.43 の `make_hotspot()`（`/usr/share/comitup/comitup/nm.py`）は
  hotspot の `802-11-wireless-security` に `key-mgmt`/`psk` しか設定して
  おらず、NetworkManager が WPA1/TKIP を選び PMF を交渉できてしまう
  ため、最近の Apple 端末がハンドシェイクに失敗する（実機で確認済み）。
  `psk` の設定直後に `proto=["rsn"]` / `pairwise=["ccmp"]` /
  `group=["ccmp"]` / `pmf=dbus.Int32(1)`（brcmfmac が AP モードで PMF に
  対応しないため無効化）を追加するパッチを apply 時に当てる。
  comitup へのアップストリーム提案が本筋だが、それまでは配布物の
  `nm.py` を直接パッチして毎回冪等に再適用する。**アンカー行
  （key-mgmt/psk の設定行）が見つからない場合は fail loud**（非ゼロ終了 +
  想定している comitup バージョンを明示するメッセージ）にする —
  comitup がアップグレードでこの構造を変えたとき、パッチが無言で
  当たらずに壊れた AP を出荷することを防ぐため。この nm.py は GPL-2.0 な
  ので、配布物に含める改変版には GPLv2 2(a) 項が求める「変更した旨と
  日付の明示」が必要 — パッチは 4 行の追加と同時に
  `Modified by Jizai Inc. on <日付>` というコメント（変更通知）も挿入する。
  冪等性の判定キーはこの通知マーカーそのもの（`pmf` の有無ではない）にして
  あり、「パッチ済み」であることが構造的に「通知済み」であることを含意する
  ようにしている。旧版（4 行のみでこの通知を持たない）が当たっている
  デバイスは、次回適用時に通知だけを追記する「upgrade」パスで治癒する。
- avahi-daemon が enable であることの確認
- `/etc/network/interfaces` に Wi-Fi 定義が無いことの確認（あれば fail、
  自動では消さない — 手焼き環境の意図的設定を壊さない）
- Wi-Fi 国コード `JP`（`raspi-config nonint do_wifi_country JP` 相当）。
  出荷後の変更手段は P2 の Portal 機能（テンプレート unit）で提供予定

## palmimo-firstboot.service + firstboot.sh

ワンショット（`Type=oneshot` + `ConditionPathExists`）。**個体化の情報源は
識別ファイルのみ**（上記の改訂どおり。CPU シリアル導出はしない）。

```
[Unit]
Description=Palmimo first-boot personalization
Before=comitup.service avahi-daemon.service palmimo-portal.service
RequiresMountsFor=/boot/firmware
ConditionPathExists=/boot/firmware/palmimo-identity.json
ConditionPathExists=!/var/lib/palmimo/firstboot-done

[Service]
Type=oneshot
ExecStart=/usr/local/lib/palmimo/firstboot.sh
RemainAfterExit=yes
```

firstboot.sh の仕様:

1. `/boot/firmware/palmimo-identity.json` から `device_id` を読む
   （JSON 破損・`device_id` 欠落は **エラー終了 + journal に ERROR**。
   hostname を変えずに終わる — 中途半端な個体化をしない）
2. `device_id` を検証（`[a-z0-9-]{1,32}` 程度の許可正規表現。hostname に
   使えない値を弾く）し、hostname を `palmimo-<device_id>` に設定
   （`hostnamectl set-hostname` + `/etc/hosts` の 127.0.1.1 行更新）
3. comitup の `ap_password` を識別ファイルの `initial_password` に設定する
   （「識別ファイル仕様 v2」参照）
4. 成功時のみ `/var/lib/palmimo/firstboot-done` を作成（冪等マーカー。
   出荷リセット（nuke + state 初期化）では `/var/lib/palmimo/` ごと
   消えるので、リセット後の再個体化も同じ経路で走る）

DIY 機（識別ファイル不在）: `ConditionPathExists` で unit ごとスキップ。
hostname は `palmimo` のまま、Portal はオープン初回セットアップ経路。

## apply-pi.sh（手適用スクリプト）

素の Raspberry Pi OS Lite (64-bit, trixie) に SSH で流す。**開発ループ用**
であり、pi-gen カスタムステージと論理を共有する（`files/` ツリーが共通実体）。

```
PI_HOST=user@<addr> PORTAL_TAG=v0.1.0-rc1 apply-pi.sh
  [--identity <path>]   # テスト識別ファイルを /boot/firmware に配置
  [--no-apt]            # apt 済みの再実行を速くする
```

手順（各ステップ冪等）:

1. apt: `comitup` `avahi-daemon` `git` `dnsmasq` の導入（`--no-apt` で
   スキップ）→ システムの `dnsmasq.service` を `disable --now`
   （comitup が自分の dnsmasq を spawn するため、システム側が port 53 を
   握っていてはいけない。バイナリだけ入っていれば comitup から呼べる）
2. comitup の `nm.py` に WPA2/PMF パッチを当てる（`lib/patch_comitup_nm.py`
   を `sudo python3 -` にパイプ。GPLv2 2(a) の変更通知マーカー
   （`Modified by Jizai Inc.`）が既に含まれていれば「already patched」で
   exit 0、アンカー行が無ければ fail loud）
3. uv: 無ければ導入（`~/.local/bin/uv`）
4. palmimo-portal: `/home/user/palmimo-portal` に **タグの clone**
   （既存なら `fetch` + そのタグへ `checkout --detach`）→ uv の frozen 同期
   → venv の python で `-m palmimo_portal.fetch_static --tag $PORTAL_TAG` を実行
   （Updater と同一コードで static asset を取得・検証）
5. `systemctl unmask comitup-web`（旧版で mask 済みのデバイスを治癒する
   冪等操作。mask のシンボリックリンクは no-op unit を置きたいパスと
   同じなので、次の rsync より前に実行する）
6. `files/` ツリーを rsync で配置（unit・polkit・comitup.conf・firstboot・
   comitup-web の no-op unit）
7. `systemctl daemon-reload` → `enable comitup avahi-daemon
   palmimo-portal palmimo-firstboot`（comitup-web は enable/mask いずれも
   しない。files/ が置いた no-op unit が static unit を上書きするだけで
   十分）
8. 検査（apply 自身のセルフチェック）: `/etc/network/interfaces` に Wi-Fi
   定義が無い / 国コード設定済み / `systemctl is-enabled` が期待どおり /
   `curl -fsS localhost:80/api/v1/system/status` が 200 / `dnsmasq`
   バイナリが存在する / `systemctl cat comitup-web` に
   `ExecStart=/bin/true` がある（no-op unit に置き換わっている） /
   `nm.py` に `pmf` の行がある
9. `--identity` 指定時は識別ファイルを配置して firstboot を今すぐ実行
   （`systemctl start palmimo-firstboot`）

失敗マトリクス（設計時に埋める表。実装 PR に対応テスト/挙動を明記）:

| ステップ × 失敗 | 途中 SSH 断 | apt/ネットワーク失敗 | 再実行 |
|---|---|---|---|
| 各ステップ | 部分状態が残るが、**再実行で収束**（clone は fetch+checkout、rsync は上書き、unmask/enable/nm.py パッチはいずれも冪等） | 該当ステップで非ゼロ終了・以降に進まない | 先頭から全ステップをやり直して安全 |

## tools/make_identity.py

テスト用の識別ファイル生成: `tools/make_identity.py --device-id 405
--password <plain>`（uv script として実行）→ `palmimo-identity.json`
（識別ファイル仕様 v2: `device_id` + `initial_password` 平文）。出荷工程
（O10）でもこの仕様が原器になる。

## 検証計画（構成物・両経路が揃った後に実機 1 回）

事前に palmimo-portal へ **`v0.1.0-rc1`**（pre-release、上書き可）を発行
（リポジトリは public 化済みの前提。未 public なら check が 404 になり
update 検証だけできない）。

| # | 項目 | 対応する既存 T |
|---|---|---|
| V1 | 素の Pi に apply-pi.sh 一発 → 電源 OFF/ON → AP `palmimo-<nnn>` が立つ | T2, T11 |
| V2 | firstboot: 識別ファイルあり → hostname/SSID/シール ID が一致。なし → `palmimo` のまま | T11（新仕様） |
| V3 | **comitup-web を mask した構成**で captive portal 自動ポップアップ（iOS/Android） | T3/T10 再検証 |
| V4 | port 80 + CAP_NET_BIND_SERVICE で Portal 応答（`http://palmimo-<nnn>.local/`） | 新規 |
| V5 | シールログイン → PW 変更 → Wi-Fi 設定 → 自動切替 → ダッシュボード | 一気通貫（8080 では確認済み） |
| V6 | polkit 越し: UI から reboot / shutdown が通り、他の systemd 操作は拒否される | 新規 |
| V7 | update 一連: check → apply(rc2) → restart → done。rollback も 1 回 | 新規（systemd 常駐で初） |
| V8 | 誤パスワード → HOTSPOT 復帰 → 再入力成功（Portal 画面での失敗表示含む） | T4 + 未確認だった UI 表示 |
| V9 | nuke（出荷リセット相当）→ 初期ログイン情報モード復帰 → firstboot 再実行 | T7（新仕様） |

証拠は journal（`palmimo-portal` / `palmimo-firstboot`）とスクリーンショットで
回収し、結果を本書に追記する。

### 受け入れ検証結果（2026-08-25、pi-gen イメージ + provision_sd.py）

- `make_image.py` によるイメージビルド（オペレータ実行、1 コマンド）→
  焼き込み前の静的検分 12 項目 green → `provision_sd.py` で実 SD へ
  焼き込み + 識別ファイル注入（device_id 406）→ 実機起動から動作確認まで
  一気通貫で **pass**（オペレータ実施）
- この実走で CLI の考慮漏れ 2 件を発見・修正: ①Mac 内蔵 SDXC リーダーは
  `Internal` 報告のため候補から漏れる（Secure Digital バス + リムーバブルの
  carve-out で解決）②raw デバイス書き込みに root が必要（プロンプト前の
  preflight 化 + PermissionError 時の「カード無傷」明示で解決）

### 実機スモーク結果（2026-08-21、apply-pi.sh + テスト機）

- **V1 ✓**: apply-pi.sh 一発 → 電源 OFF/ON → AP `palmimo-<nnn>` が立ち、
  WPA2 でのハンドシェイクが（TKIP/PMF パッチ後）成功して join できることを確認
- **V3 ✗→✓**: captive portal の自動ポップアップは初回スモークでは未達
  （OS の疎通確認プローブに Portal 側が応答していなかった）。
  **palmimo-portal#15**（unprovisioned 時のみプローブに 302 応答）で解消し、
  2026-08-21 の再検証で iPhone の Safari 起動を契機にポータルが表示される
  ことを確認（参加した瞬間のシート自動表示は iOS 側挙動に依存し、
  サーバ応答は正しいため受容）
- **AP→STA 切替で `.local` の IPv4 レコードが失われることがある（#683）**:
  再検証で発見。avahi が新アドレス登録時に `Local name collision` で失敗し
  `<hostname>.local` が IPv6 リンクローカルのみになる — セットアップ完了
  直後の「.local で開き直す」導線がちょうど壊れる。対策として
  `files/etc/NetworkManager/dispatcher.d/50-palmimo-avahi` を追加。
  **素の restart は禁止**: `comitup.service` は
  `Requires=avahi-daemon.service` を持つため avahi の restart は comitup の
  kill→再起動に伝播し、hotspot では comitup 起動が再び wlan0 up を発火させて
  **restart ループで comitup が start-limit 死する**（2026-08-21 実機で発生・
  復旧済み）。また「壊れた時だけ restart」の検出はローカルでは不成立
  （Pi 上の getent/avahi は loopback レコードを返し、クライアントが見る
  wlan0 レコードを観測できない — これも実機で確認）。フックは
  (1) AP サブネット 10.41.* では何もしない
  (2) STA 側の up で `--job-mode=ignore-dependencies` の restart を無条件に
  1 回走らせる — 依存ジョブを作らないため comitup には一切触れず、
  ループは構造的に不可能（実機で comitup PID 不変を確認）
- **V4 ✓**: port 80 + CAP_NET_BIND_SERVICE で Portal が応答、
  `http://palmimo-<nnn>.local/` も到達
- **V5 ✓**: シールログイン → PW 変更 → Wi-Fi 設定 → 自動切替 →
  ダッシュボードの一気通貫を確認
- **V6 backend 半分 ✓**: polkit の allow/deny マトリクス（許可アクションは
  通り、それ以外は拒否）、logind 経由の reboot、boot からポータル応答まで
  **27 秒**を確認。UI 側の確認は別途
- **V8 ✓**: 誤パスワード → HOTSPOT 復帰 → 再入力成功（Portal 画面の失敗
  表示を含む）
- **firstboot ✓**（`/etc/hostname` 直接書き込み後、PR #680）。この実機での
  root cause は `/` の所有権が壊れていたことだったが、`hostnamectl` 経由
  ではなく `/etc/hostname` を直接書く方式は「壊れた環境でも通る」頑健な
  選択として維持する

**発見して修正した障害の連鎖**（すべて実機で相互に独立して setup-AP への
join を壊していた）: TKIP/PMF ハンドシェイク失敗 →（修正後に露見）
comitup-web の mask が `DBusException` で HOTSPOT セットアップ全体を
中断 →（修正後に露見）dnsmasq 未導入で DHCP リースが出ない。3 件とも
本 PR（`fix/image-hotspot-chain`）で apply-pi.sh / `files/` へ反映済み

UX 上の発見（本設計書の範囲外、palmimo-portal 側で追跡）:
**palmimo-portal#13 palmimo-portal#14 palmimo-portal#15**

## cloud-init との境界（2026-08-21 決定）

trixie の Raspberry Pi OS は cloud-init を同梱し、初回起動時に
default_user（`pi`）の作成と hostname 管理を行おうとする。本イメージでは
アカウントはビルド時に確定（`user` 1 本・パスワードロック・bash シェル）、
hostname は palmimo-firstboot の専権のため、
`files/etc/cloud/cloud.cfg.d/99-palmimo.cfg`（`users: []` +
`preserve_hostname: true`）で cloud-init をこの 2 領域から締め出す。
files/ ツリー経由なので apply-pi.sh の開発機にも同じ境界が適用される。
なお pi-gen は `FIRST_USER_PASS` 未設定だとシェルを nologin のまま残すため、
02-account-ssh が `usermod -s /bin/bash` を明示する（処女ビルドで発覚）。

## pi-gen イメージビルドと焼き込み CLI（実装済み、2026-08-21）

pi-gen（[RPi-Distro/pi-gen](https://github.com/RPi-Distro/pi-gen)、`arm64`
ブランチ、Docker ビルド経路）のカスタムステージが、apply-pi.sh と論理を
共有しながら出荷 .img を直接組み立てる。

### 共有入力（単一の正）

このリポジトリの直下に、apply-pi.sh と pi-gen ステージの両方が参照する
実体を切り出した:

- `packages.txt` — apt パッケージリスト（1 行 1 パッケージ）
- `lib/patch_comitup_nm.py` — WPA2/PMF nm.py パッチ（旧: apply-pi.sh に
  埋め込みだった heredoc をファイル化。冪等・fail-loud の挙動は不変）
- `files/`（既存） — 適用先パスをそのまま再現したツリー

apply-pi.sh は `packages.txt` を読んで apt install し、
`lib/patch_comitup_nm.py` を SSH 越しに `sudo python3 -` へパイプする
（埋め込みをやめただけで、SSH 越しに当てる動作自体は不変）。

pi-gen 側は Docker ビルドの構造上、pi-gen チェックアウトの外にあるファイルは
デフォルトでは見えない（`Dockerfile` が `COPY . /pi-gen` でビルド時に
焼き込むため）。そこで `PIGEN_DOCKER_OPTS` の `--volume` でこのリポジトリを
コンテナに `/palmimo-image` として bind mount し、`config` の
`PALMIMO_IMAGE_DIR=/palmimo-image` 経由でステージの
スクリプトから読む。`files/` と `lib/patch_comitup_nm.py` はチェックイン
コピーを作らずこの経路で直接読む。`00-packages`（pi-gen がステージ内に
要求する固定ファイル名）だけは `stage-palmimo/prerun.sh` がビルド時に
`packages.txt` からコピー生成する（チェックインしない — ドリフトしようが
ない）。詳細と bind mount の張り方は `pigen/README.md`。

### ステージ構成 `pigen/stage-palmimo/`

stage2（Raspberry Pi OS Lite）の後に続く 4 サブステージ:

1. `00-packages/` — 上記のとおり `packages.txt` から生成
2. `01-palmimo-core/` — nm.py パッチ・`files/` ツリーのコピー・
   dnsmasq の disable・comitup-web の unmask・4 unit の enable
   （comitup-web は enable/mask のいずれもしない — apply-pi.sh と同じ理由）
3. `02-account-ssh/` — アカウント/SSH ポリシー（次項）
4. `03-portal/` — uv インストール・palmimo-portal のタグ clone・
   frozen 同期（`--no-dev`）・`fetch_static`（apply-pi.sh と同一コード
   パス）

### アカウント/SSH ポリシーとその理由

pi-gen はゼロから組み立てるため、Raspberry Pi Imager が事前にやっている
前提（apply-pi.sh はそれに乗っている）を自分で作る必要がある:

- **パスワードログインは無効化**（`passwd -l user`。`FIRST_USER_PASS` を
  設定しない pi-gen の既定でも同じ状態になるが、テスト可能にするため
  明示する）
- **SSH は鍵認証のみ**（`ENABLE_SSH=1` で有効化した上で、
  `sshd_config.d/50-palmimo-key-only.conf` に
  `PasswordAuthentication no` + `KbdInteractiveAuthentication no`）。
  理由: **Portal が鍵登録の唯一の経路**であり、既存の「最後の鍵を消すと
  SSH から締め出される」という警告（palmimo-portal 側の既存 UX）は
  鍵認証オンリーであることを前提にしている。パスワード認証が生きていると
  この前提が崩れる
- **sudo は NOPASSWD**（`sudoers.d/010-palmimo-user`、モード `0440`、
  `visudo -c` で検証）。パスワードログインが無いのでパスワード付き sudo は
  そもそも使えない。devkit の開発ループ（apply-pi.sh の前提と同じ）を
  壊さないための割り切り

いずれも `files/` には置かない（apply-pi.sh 側では不要なため）。

### ビルド手順（要約。詳細は `pigen/README.md`）

arm64 の Docker ホストが必要（`build-docker.sh` が QEMU 経由の
アーキテクチャエミュレーションを内部で処理する）。pi-gen を clone → `arm64`
ブランチへ → `stage-palmimo` を symlink → `config` を配置 →
`PIGEN_DOCKER_OPTS` でこのリポジトリを bind mount → `./build-docker.sh`。
`PALMIMO_PORTAL_TAG`（既定 `v0.1.0`）は `PIGEN_DOCKER_OPTS` に `-e` で渡す
（`config` はコンテナ内で source されるだけで、ホストの環境変数は
自動では渡らないため）。

Wi-Fi 国コード JP は pi-gen 側では `config` の `WPA_COUNTRY=JP`
（pi-gen 組み込みのビルド時設定）で焼き込む。apply-pi.sh の
`raspi-config nonint do_wifi_country JP` はライブコマンド（実行中の Pi に
対する自己検査つき適用）なので、pi-gen 側は素直にビルド時設定に置き換わる
だけで論理の共有対象ではない。

### 出荷イメージに含めないもの

識別ファイル（`palmimo-identity.json`）は焼き込まない。焼き込み/注入 CLI
（PR-B）が「公式手順で .img を焼く → boot（FAT）パーティションに
`palmimo-identity.json` を書く」だけを担う。FAT なので macOS からも書ける。
個体化はすべて firstboot が担うため、CLI は識別ファイル以外に触らない。

## 識別ファイル仕様 v2（2026-08-21 決定）

```json
{ "device_id": "405", "initial_password": "<シール記載の平文>" }
```

- **初期パスワードは平文 1 フィールド**（ユーザー決定）。旧
  `initial_password_hash` は廃止 — 平文があればハッシュは冗長で、二重に
  持つと印字値との不一致事故の元。シールに書いてある値なので boot
  パーティション平文は脅威モデル上許容（SD 窃取は守備範囲外、の既存判断と
  同じ線）
- firstboot は同じ値を comitup `ap_password` に設定（シール 1 値の原則が
  ファイル上も 1 フィールドで成立）。なお `/etc/comitup.conf` を焼き込み時に
  直接書く案は不採用: rootfs は ext4 で macOS/Windows から書けず、共通
  ファイルに個体値を混ぜると「共通イメージ + FAT 注入」の分離も崩れる。
  firstboot が FAT 上の識別ファイルを /etc 側へ反映するのが「外から
  書き込む」の実装形
- **palmimo-portal 側の追随（小 PR）**: 識別ファイル読取りを新仕様に変更し、
  初期ログイン照合を argon2 検証から定数時間比較
  （`secrets.compare_digest`、または読み取り時ハッシュ化）へ。既存のテスト
  identity ファイルも新仕様へ更新
- 製造工程（O10）への効用: 印字する値をそのまま JSON に書くだけになり、
  argon2 生成を焼き込みラインに持ち込まなくてよい
- **`initial_password` の許可文字集合は `[A-Za-z0-9]{8,63}`**
  （製造シールの原器仕様。8 文字下限は WPA2-PSK の下限、英数字限定は
  シールの可読性に加え、`comitup.conf` への書き込みや sed など下流の
  どこに渡ってもエスケープ不要で安全という理由による）。
  `tools/make_identity.py` と `firstboot.sh` の両方でこの正規表現を検査する
  （実装は sed 置換への直接埋め込みも避け、リテラルとして扱う——両対策とも
  同じレビュー指摘への対応）

## 未決（実装前に確定するもの）

- polkit の comitup 許可が実際に必要か（T9 では user 権限で D-Bus 呼び出しが
  通っている — バスポリシー次第）。実機で確認し、不要なら書かない
