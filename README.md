# 夕飯自動化

月・火・水・金の仮献立を、3人分の材料・作り方・買い物リスト付きで表示する最小版です。

## ページを更新する

このフォルダをVS Codeで開き、ターミナルで実行します。

```powershell
python main.py
```

`output/index.html` をブラウザで開いて確認します。外部ライブラリは不要です。
元のレシピデータは2人分で、生成時に1.5倍しています。
献立の選定や履歴への記録は、今後追加する予定です。

## GitHub Pagesの初回設定

1. GitHubに用意したリポジトリへ、このプロジェクトを `main` ブランチとして登録します。
2. リポジトリの **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。
3. **Actions → Publish dinner page → Run workflow** を実行します。
4. 完了後、**Settings → Pages** に表示されるURLをスマホで開きます。

以後は `python main.py` で生成した `output/index.html` をGitHubの `main` に反映すると、公開ページも更新されます。
公開処理が配信するのは `output/` の中身です。
公開リポジトリを使う場合は、ソースコードなどリポジトリ内のファイルも閲覧できます。

## 表示と買い物チェック

チェックは端末・ブラウザごとに保存されます。他の端末とは同期しません。
献立や数量が変わるとチェックは新しくなります。
同じ献立・数量で再生成した場合はチェックが残るため、必要に応じて「チェックをすべて外す」を押してください。
古い内容が表示される場合は、開いているページを閉じて開き直してください。

## 検索への掲載

HTMLに `noindex, nofollow` を設定しています。これは検索エンジンへの指示であり、閲覧を制限するものではありません。

参考: https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages
