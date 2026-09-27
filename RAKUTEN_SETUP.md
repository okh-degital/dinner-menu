# 楽天レシピ取得の準備

## 1. 楽天ウェブサービスでアプリを登録

https://webservice.rakuten.co.jp/guide

楽天アカウントでログインし、アプリを登録します。
アプリ名は「夕飯メニュー」、アプリURLは以下を使えます。

https://okh-degital.github.io/dinner-menu/

利用目的は「家庭用の夕飯献立候補として、楽天レシピのカテゴリ別ランキングを取得し、元レシピへのリンクを表示する」です。
APIの利用許可が必要な場合は楽天レシピを選択してください。
発行・承認されたアプリIDとアクセスキーを使います。

## 2. このPCにキーを保存

`rakuten.example.json` を同じフォルダに `rakuten.local.json` という名前でコピーし、空欄にアプリIDとアクセスキーを記入します。

`rakuten.local.json` はGitへの登録対象外です。キーは `settings.json` やHTMLに書かず、チャットにも貼らないでください。
環境変数 `RAKUTEN_APPLICATION_ID` と `RAKUTEN_ACCESS_KEY` を設定する方法にも対応しています。

## 3. まず1回取得

プロジェクトのターミナルで実行します。

```powershell
python fetch_rakuten.py
```

カテゴリを指定する場合の例:

```powershell
python fetch_rakuten.py --category-id 10
```

結果は `data/rakuten_recipes.json` に保存します。このフォルダもGitへの登録対象外です。
短時間に何度も実行しないでください。失敗時に自動リトライは行いません。

## 現段階の範囲

ランキングは最大4件です。取得できるのはレシピ名・URL・材料名・調理時間・費用目安などです。
人数、材料の分量、調理手順はAPIに含まれないため、結果には未取得として保存します。
3人分への換算は、元の人数と分量を確認してから追加します。費用目安も3人分の費用とは限りません。
この取得処理は `main.py`、献立履歴、公開中のHTMLを変更しません。
公開ページにAPIのデータを組み込む段階で、楽天の指定クレジットを追加します。

公式仕様: https://webservice.rakuten.co.jp/documentation/recipe-category-ranking
