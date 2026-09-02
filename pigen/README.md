# pigen

出荷 SD イメージの pi-gen カスタムステージの実体。
**ビルド手順は [../README.md](../README.md)**（`make_image.py` 一発）。
このファイルはステージ実装のノートのみ。

## 手動でビルドを追う場合

`make_image.py` が自動化している素の手順（デバッグ用）。この repo の
チェックアウトが `../palmimo-image` にある想定:

```bash
git clone https://github.com/RPi-Distro/pi-gen.git
cd pi-gen
git checkout arm64   # 64-bit ビルド用ブランチ。RELEASE は trixie がデフォルト

cp -R ../palmimo-image/pigen/stage-palmimo stage-palmimo
cp ../palmimo-image/pigen/config config
touch stage2/SKIP_IMAGES   # palmimo イメージのみ export

export PIGEN_DOCKER_OPTS="--volume $(cd ../palmimo-image && pwd):/palmimo-image:ro"
# 任意: -e PALMIMO_PORTAL_TAG=v0.1.0-rc1 を PIGEN_DOCKER_OPTS に追加
./build-docker.sh
# 成果物: deploy/image_<date>-palmimo.img.xz
```

前回のビルドコンテナ `pigen_work` が残っていると pi-gen が停止する —
`docker rm -f pigen_work` してから実行する（`make_image.py` はこの検査と
削除を自動でやる。動いている最中のコンテナだけは無言で kill せず中断する）。

## stage-palmimo は cp で入れ、共有物は bind mount で読む理由

pi-gen の Docker ビルドは `docker build` 時点のチェックアウトを
`COPY . /pi-gen` でイメージに焼き込む — それ以外のファイルは
`PIGEN_DOCKER_OPTS` の bind mount 経由でしかコンテナから見えない。
`stage-palmimo` 自身のスクリプトは小さくステージの形をしているので、
他のステージと同様に pi-gen ツリーへ cp する（symlink は Docker ビルド内で
dangling になる）。一方、このリポジトリが所有する大きな共有入力
（`../packages.txt`・`../lib/patch_comitup_nm.py`・`../files/`）はこの
ディレクトリへ複製しない — `stage-palmimo/prerun.sh` と
`01-palmimo-core/00-run.sh` がビルド時に `$PALMIMO_IMAGE_DIR`
（`/palmimo-image`、bind mount で解決）から読む。
apply-pi.sh が自分のチェックアウトから読むのと同じファイルであり、
「単一ソース」がここで成立する: apt リストも WPA2/PMF パッチも
`files/` ツリーも 1 つ、消費者が 2 つ。

## あえて使わない pi-gen ネイティブフラグ

pi-gen には `PUBKEY_ONLY_SSH=1` と `PASSWORDLESS_SUDO=1` があり、
`02-account-ssh/` の sshd / sudoers drop-in と重複するが、使わない:
`PUBKEY_ONLY_SSH` は既存の `sshd_config` 行を sed するだけで、行の形が
違うと**黙って何もしない**。`sshd_config.d` の drop-in は無条件に適用され、
契約テストでピン留めもされている。sudoers も同じ理由で、レビュー済みの
1 ファイルに両ポリシーをまとめている。

なお `DISABLE_FIRST_BOOT_USER_RENAME=1` に必要な `FIRST_USER_PASS` は
使い捨て値で、`02-account-ssh/` が直後にアカウントをロックする —
locked + 鍵認証のみの構成に正規の道がない upstream 公認ギャップ
（[pi-gen#670](https://github.com/RPi-Distro/pi-gen/issues/670)）への
既知の回避（詳細は `config` のコメント）。

## ステージ構成

`stage-palmimo/` は pi-gen 標準の `stage2`（Raspberry Pi OS Lite）の後に
走る。サブステージ 5 つ:

- `00-packages/` — `00-packages` はチェックインしない。`prerun.sh` が
  ビルド時に `packages.txt` から生成する。ドリフトするものをここに
  置かない。
- `01-palmimo-core/` — nm.py の WPA2/PMF パッチ・`files/` ツリーの配置・
  dnsmasq の disable・comitup-web の unmask・unit の enable（comitup /
  avahi-daemon / palmimo-portal / palmimo-firstboot — comitup-web は
  決して enable しない）。
- `02-account-ssh/` — パスワードロックされた `user` アカウント・
  `sudoers.d` の NOPASSWD drop-in・鍵認証のみの sshd drop-in・初回起動
  ウィザード（userconfig.service）の非武装化。apply-pi.sh とは共有しない:
  apply-pi.sh の対象は Raspberry Pi Imager で設定済みの Pi、pi-gen は
  ゼロから作るため。
- `03-portal/` — uv 導入・`palmimo-portal` のタグ clone・
  `uv sync --frozen --no-dev`・`fetch_static`（Updater と同一コードパス）。
- `04-oss-compliance/` — `03-portal` の後（venv が出来てから）に走る。
  一時的に deb-src を有効化 (`files/palmimo-src.sources`) して
  `apt-get update` → `lib/collect_oss_compliance.py` をチェックインコピー
  を作らず `$PALMIMO_IMAGE_DIR` からチロートの `/tmp` へコピーして
  `on_chroot` で実行 → 削除 → deb-src を無効化して再度
  `apt-get update`（出荷イメージは deb-src を持たない）。chroot に実際に
  入っているパッケージの版と同じ対応ソースを
  `/usr/share/palmimo/sources/` に集め、apt の copyright と Portal の
  Python 依存ライセンスを `/boot/firmware/licenses/{pi,portal}/` に写す
  （GPLv2 §3(a) / GPLv3 §6(a)。詳細は `../doc/design.md` の
  「対応ソースとライセンス全文の同梱」）。
  `PALMIMO_SKIP_CORRESPONDING_SOURCE=1`（`config` で export 済み、
  `tools/make_image.py --skip-corresponding-source` からも渡せる）は
  対応ソース取得だけを飛ばす開発用エスケープハッチ — ライセンスのコピー
  自体は常に行われ、`MANIFEST.txt` の先頭に `STATUS: INCOMPLETE` が
  刻まれるので、この設定で作ったイメージが出荷可能と誤認されることはない。

`EXPORT_IMAGE` がこのステージを最終 `.img` の export 対象として指す。
`.workspace/` は `make_image.py` の作業場（gitignored・消して良い）。
