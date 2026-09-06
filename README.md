# headline-collector（Xを読む係）

5アカウント（@DeItaone @FirstSquawk @financialjuice @Yuto_Headline @SBILM）の新着ヘッドラインを
10分ごとに自動で拾い、`data/` に溜め続けます。判断は一切しません。溜めるだけです。

---

## 毎朝やること（これだけ）

1. このリポジトリの `data` フォルダ → `latest_12h.md` を開く
2. 右上の **「Copy raw file」**（四角が2つ重なったアイコン）を押す
3. ChatGPT か Claude に貼り付けて送信 → 8通貨別のまとめが出ます

- 6時間分なら `latest_6h.md`、24時間分なら `latest_24h.md`
- ファイルの先頭に「AIへの指示」が入っているので、貼るだけで形式どおりに出ます

---

## 初回だけやること

### 1. ファイルを置く
- リポジトリの画面で **「Add file」→「Upload files」**
- 解凍したフォルダの中身（`collect.py` `requirements.txt` `prompt.md` `README.md` `data` `.github`）を
  まとめてドラッグ＆ドロップ → 下の **「Commit changes」**

> `.github` フォルダがうまく入らなかったときは：
> **「Add file」→「Create new file」** → ファイル名の欄に `.github/workflows/collect.yml` と入力 →
> `collect.yml` の中身を貼り付け → **「Commit changes」**

### 2. 書き込み許可をつける（念のため）
- **Settings** → 左メニュー **Actions** → **General** → 一番下の **Workflow permissions**
- **「Read and write permissions」** を選んで **Save**

### 3. 一度手動で動かす
- 上の **Actions** タブ → 左の **「collect」** → 右の **「Run workflow」** → 緑のボタン
- 1〜2分待って、`data/latest_12h.md` ができていれば成功
- 以後は10分ごとに勝手に動きます（PCの電源が切れていても動きます）

---

## 止まったとき

- Actions タブに **「Scheduled workflows are disabled」** と出たら **Enable** を押す
  （2ヶ月間リポジトリを触らないと止まる仕様です。毎日 `latest_12h.md` を見ていれば止まりません）
- 赤い ✕ が続くときは、その実行を開いてログの内容を Claude に見せてください

---

## 仕組み

| 取得元 | 対象 | 方式 |
|---|---|---|
| X | 5アカウント | サイト埋め込み用の公開エンドポイント（ログイン不要）。1回につき直近20件 |
| Telegram | DeItaone（=WalterBloomberg）、financialjuice | 公開ミラー `t.me/s/`（ログイン不要）。取得が遅れた分は自動で遡って穴埋め |
| CME | COMEX金先物の建玉 | 取引日・速報／確報・建玉残高・前日比・出来高・中心限月。価格方向はYahooの日足から |

- 72時間分を `data/headlines.jsonl` に保持。同じ文はX・Telegramの両方にあっても1行に統合
- `data/latest_*.md` の「取得状況」に、アカウント別の件数・最古・最新・最大空白が出ます。
  ⚠ は45分超の空白＝欠落の可能性（週末・米国夜間は自然に空きます）
- CMEは日本時間の朝には前営業日分が未掲載のことがあります。その場合は掲載済みの最新分を取引日付きで出します

## 制限

- Xは1回の取得が直近20件までなので、指標発表直後などの高頻度時は FirstSquawk が取りこぼす可能性があります
  （DeItaone と financialjuice は Telegram 側で補完されます）
- Telegramミラーは非公式の自動転送なので、止まることがあります。その場合はX側だけで続きます
- 建玉の「価格方向」はベストエフォートです。取れない日は「判定: 保留」と出ます

## 設定を変えたいとき

- アカウントの追加・削除：`collect.py` の先頭 `X_ACCOUNTS`
- Telegramミラーの追加：`collect.py` の先頭 `TG_CHANNELS`（チャンネル名: Xアカウント名）
- 実行間隔：`.github/workflows/collect.yml` の `cron` の行
- ダイジェストの窓：`collect.py` の `DIGEST_HOURS`

---

このリポジトリは **Public（公開）** で作ってください。公開ならタイマー実行が時間無制限で無料です。
中身は公開ヘッドラインだけで、パスワードや個人情報は一切入りません。
