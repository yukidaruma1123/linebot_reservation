import os
import sqlite3
from datetime import datetime, timedelta, time as dt_time # timeをdt_timeとしてインポート
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, TemplateMessage,
    ConfirmTemplate, PostbackAction, DatetimePickerAction,
    QuickReply, QuickReplyItem, RichMenu, RichMenuArea,
    RichMenuBounds, MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from dotenv import load_dotenv # python-dotenvをインストールしてください (pip install python-dotenv)
import json

# .envファイルから環境変数を読み込む (開発時便利)
load_dotenv()

# --- 1. アプリケーション設定 ---
app = Flask(__name__)

# LINE Developersコンソールから取得した値を環境変数に設定してください
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("エラー: チャネルアクセストークンまたはチャネルシークレットが環境変数に設定されていません。")
    exit()

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# データベースファイル名
DB_NAME = 'reservations.db'

# 店舗設定 (将来的にはこれもDBや設定ファイルで管理するのが望ましい)
STORE_OPEN_TIME = dt_time(10, 0)  # 開店時間 10:00
STORE_CLOSE_TIME = dt_time(22, 0) # 閉店時間 22:00
RESERVATION_INTERVAL_MINUTES = 30 # 予約可能な時間間隔（例: 30分ごと）
MAX_RESERVATIONS_PER_SLOT = 2     # 同じ時間帯に受け付け可能な最大予約数

# --- 2. データベース関連 ---
def get_db_connection():
    """データベース接続を取得"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row # カラム名でアクセスできるようにする
    return conn

def init_db():
    """データベースの初期化（テーブル作成）"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # ユーザーステート管理テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id TEXT PRIMARY KEY,
                state TEXT,
                data TEXT  -- JSON形式で予約途中の情報を保存
            )
        ''')
        # 予約情報テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                reservation_datetime TEXT NOT NULL, -- ISO 8601形式 (YYYY-MM-DDTHH:MM:SS)
                num_people INTEGER NOT NULL,
                status TEXT NOT NULL, --例: 'confirmed', 'cancelled'
                created_at TEXT NOT NULL
            )
        ''')
        conn.commit()

# --- ユーザーステート管理関数 ---
def get_user_state(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state, data FROM user_states WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return {"state": row["state"], "data": json.loads(row["data"]) if row["data"] else {}}
        return None

def set_user_state(user_id, state, data=None):
    current_data_json = json.dumps(data if data is not None else {})
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_states (user_id, state, data) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, data = excluded.data
        ''', (user_id, state, current_data_json))
        conn.commit()

def delete_user_state(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
        conn.commit()

# --- 予約管理関数 ---
def create_reservation(user_id, reservation_datetime_obj, num_people):
    reservation_datetime_iso = reservation_datetime_obj.isoformat()
    created_at_iso = datetime.now().isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO reservations (user_id, reservation_datetime, num_people, status, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, reservation_datetime_iso, num_people, 'confirmed', created_at_iso))
            conn.commit()
            return True
        except sqlite3.Error as e:
            app.logger.error(f"DB Error (create_reservation): {e}")
            return False

def count_reservations_for_datetime(reservation_datetime_obj):
    """指定された日時の予約数をカウント"""
    reservation_datetime_iso_exact = reservation_datetime_obj.isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM reservations
            WHERE reservation_datetime LIKE ? AND status = 'confirmed'
        ''', (f"{reservation_datetime_iso_exact.split('T')[0]}%",)) # 同日の予約をカウント
        count = cursor.fetchone()[0]
        return count

def is_store_open(reservation_datetime_obj):
    """指定された日時が営業時間内か判定"""
    reservation_time = reservation_datetime_obj.time()
    return STORE_OPEN_TIME <= reservation_time < STORE_CLOSE_TIME

def is_valid_reservation_minute(reservation_datetime_obj):
    """予約時刻の分が予約間隔に合致するか"""
    return reservation_datetime_obj.minute % RESERVATION_INTERVAL_MINUTES == 0


# --- 3. LINE メッセージテンプレート作成ヘルパー ---
def create_confirm_template(text, yes_label, yes_data, no_label, no_data):
    return TemplateMessage(
        alt_text=text.split('\n')[0],
        template=ConfirmTemplate(
            text=text,
            actions=[
                PostbackAction(label=yes_label, data=yes_data, display_text=yes_label),
                PostbackAction(label=no_label, data=no_data, display_text=no_label)
            ]
        )
    )

def create_date_picker(action_label="日付を選択", postback_data="select_date"):
    now = datetime.now()
    min_date = now.strftime('%Y-%m-%d')
    max_date = (now + timedelta(days=7)).strftime('%Y-%m-%d') # 例: 1週間後まで

    return QuickReply(
        items=[
            QuickReplyItem(action=DatetimePickerAction(
                label=action_label,
                data=postback_data,
                mode="date",
                initial=min_date,
                min=min_date,
                max=max_date
            ))
        ]
    )

def create_time_selection_quick_reply(base_datetime=None):
    """30分単位の時間選択肢をQuickReplyで返す（当日分 or 指定日）"""
    items = []
    if not base_datetime:
        base_datetime = datetime.now()

    # 当日 or 翌日の日付（時間が遅ければ翌日に切り替えも可）
    date = base_datetime.date()
    now = datetime.now()

    # 時間範囲
    current = datetime.combine(date, STORE_OPEN_TIME)
    end = datetime.combine(date, STORE_CLOSE_TIME)

    while current < end:
        # 今より30分以上先の時間のみ表示（当日の場合）
        if current > now + timedelta(minutes=30):
            label = current.strftime("%H:%M")
            iso_str = current.isoformat()
            items.append(
                QuickReplyItem(
                    action=PostbackAction(
                        label=label,
                        data=f"select_time|{iso_str}",
                        display_text=label
                    )
                )
            )
        current += timedelta(minutes=RESERVATION_INTERVAL_MINUTES)

    return QuickReply(items=items)

# --- 4. Webhookルートとイベントハンドラ ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. Check your channel secret.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error processing request: {e}")
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token
    messages_to_reply = []

    user_state_info = get_user_state(user_id)
    current_state = user_state_info["state"] if user_state_info else None
    user_data = user_state_info["data"] if user_state_info and user_state_info.get("data") else {}

    if text.lower() == "予約":
        set_user_state(user_id, "ASKING_TIME", {})
        messages_to_reply.append(TextMessage(
            text="ご希望の時間帯を選択してください（本日分のみ表示）",
            quick_reply=create_time_selection_quick_reply()
        ))

    elif current_state == "ASKING_PEOPLE":
        try:
            num_people = int(text)
            if not (1 <= num_people <= 10):
                raise ValueError("人数は1名から10名の間で入力してください。")

            user_data["people"] = num_people
            set_user_state(user_id, "CONFIRMING_RESERVATION", user_data)

            dt_obj_str = user_data.get("datetime_obj_iso")
            dt_display_str = "未選択"
            if dt_obj_str:
                dt_obj = datetime.fromisoformat(dt_obj_str)
                dt_display_str = dt_obj.strftime('%Y年%m月%d日 %H時%M分')

            confirm_text = (
                f"以下の内容で予約しますか？\n"
                f"日時: {dt_display_str}\n"
                f"人数: {num_people}名様"
            )
            messages_to_reply.append(create_confirm_template(confirm_text, "はい", "confirm_yes", "いいえ", "confirm_no"))
        except ValueError as e:
            messages_to_reply.append(TextMessage(text=f"人数を正しく入力してください (例: 2)。\nエラー: {e}"))
        except Exception as e:
            app.logger.error(f"Error in ASKING_PEOPLE state: {e}")
            messages_to_reply.append(TextMessage(text="エラーが発生しました。もう一度お試しください。"))
    else:
        messages_to_reply.append(TextMessage(text=f"「{text}」ですね。\n「予約」と入力すると予約を開始できます。"))

    if messages_to_reply:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            try:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=reply_token, messages=messages_to_reply)
                )
            except Exception as e:
                app.logger.error(f"Error sending reply message: {e}")


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    postback_data = event.postback.data
    
    messages_to_reply = []

    user_state_info = get_user_state(user_id)
    current_state = user_state_info["state"] if user_state_info else None
    user_data = user_state_info["data"] if user_state_info and user_state_info.get("data") else {}

    if postback_data.startswith("select_time|"):
        if current_state != "ASKING_TIME":
            messages_to_reply.append(TextMessage(text="予期せぬ操作です。最初から「予約」と入力してください。"))
            delete_user_state(user_id)
        else:
            try:
                iso_str = postback_data.split("|")[1]
                selected_dt = datetime.fromisoformat(iso_str)

                # 空きチェック
                if count_reservations_for_datetime(selected_dt) >= MAX_RESERVATIONS_PER_SLOT:
                    messages_to_reply.append(TextMessage(text="申し訳ありません。その時間帯は満席です。別の時間をお選びください。"))
                    messages_to_reply.append(TextMessage(
                        text="再度、時間帯をお選びください。",
                        quick_reply=create_time_selection_quick_reply()
                    ))
                else:
                    user_data["datetime_obj_iso"] = selected_dt.isoformat()
                    set_user_state(user_id, "ASKING_PEOPLE", user_data)
                    time_display = selected_dt.strftime('%H:%M')
                    messages_to_reply.append(TextMessage(text=f"{time_display}ですね。次に、人数（1〜10）を入力してください。"))
            except Exception as e:
                app.logger.error(f"時間選択の処理エラー: {e}")
                messages_to_reply.append(TextMessage(text="時間形式の処理中にエラーが発生しました。もう一度お試しください。"))

            else:
                messages_to_reply.append(TextMessage(text="日時が選択されませんでした。"))

    elif postback_data == "confirm_yes" and current_state == "CONFIRMING_RESERVATION":
        if not user_data.get("datetime_obj_iso") or not user_data.get("people"):
            messages_to_reply.append(TextMessage(text="予約情報が不足しています。最初からやり直してください。"))
            delete_user_state(user_id)
        else:
            dt_obj = datetime.fromisoformat(user_data["datetime_obj_iso"])
            num_people = user_data["people"]

            # 再度空き状況を確認 (確認ボタンを押すまでの間に埋まる可能性を考慮)
            num_existing_reservations = count_reservations_for_datetime(dt_obj)
            if num_existing_reservations >= MAX_RESERVATIONS_PER_SLOT:
                messages_to_reply.append(TextMessage(text="申し訳ありません。最終確認中に満席となってしまいました。お手数ですが、別の日時で再度お試しください。"))
                delete_user_state(user_id) # 状態リセット
            elif create_reservation(user_id, dt_obj, num_people):
                messages_to_reply.append(TextMessage(text="ご予約ありがとうございます！予約を確定しました。"))
                # TODO: 店舗側への通知処理などをここに追加
                delete_user_state(user_id) # 予約完了後、状態をリセット
            else:
                messages_to_reply.append(TextMessage(text="申し訳ありません、予約の処理中にエラーが発生しました。お手数ですが、少し時間をおいて再度お試しください。"))
                # delete_user_state(user_id) # 状況によっては状態を維持してリトライさせることも検討

    elif postback_data == "confirm_no" and current_state == "CONFIRMING_RESERVATION":
        messages_to_reply.append(TextMessage(text="予約をキャンセルしました。最初からやり直す場合は「予約」と入力してください。"))
        delete_user_state(user_id)
    # ... 他のPostbackデータ処理 ...

    if messages_to_reply:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            try:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=reply_token, messages=messages_to_reply)
                )
            except Exception as e:
                app.logger.error(f"Error sending postback reply message: {e}")


# --- 5. アプリケーション実行 ---
if __name__ == "__main__":
    init_db() # アプリケーション起動時にデータベースを初期化
    port = int(os.environ.get("PORT", 8080)) # HerokuなどはPORT環境変数を設定する
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "False").lower() == "true")
