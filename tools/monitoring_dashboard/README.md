# ClassnoteX Monitoring Dashboard

このツールは Classnote API のセッションデータをローカルで可視化・分析するためのダッシュボードです。
Streamlit を使用して構築されています。

## 前提条件

- Python 3.8 以上
- `pip`

## セットアップ

依存ライブラリをインストールしてください。

```bash
pip install -r tools/monitoring_dashboard/requirements.txt
```

## 起動方法

以下のコマンドを実行してダッシュボードを起動します。

```bash
streamlit run tools/monitoring_dashboard/app.py
```

ブラウザが自動的に開き、`http://localhost:8501` でアクセスできます。

## 機能

- **環境切り替え**: Production (Cloud Run) と Local (localhost:8000) をサイドバーで切り替え可能です。
- **メトリクス**: 総セッション数、UU数、録音時間の統計を表示します。
- **分布グラフ**: ステータス別、モード別（講義/会議）の割合を可視化します。
- **ユーザーランキング**: アクティブなユーザーの上位を表示します。
- **データテーブル**: 生データをテーブル形式で確認・ソートできます。
