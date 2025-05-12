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
    QuickReply, QuickReplyItem
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from dotenv import load_dotenv # python-dotenvをインストールしてください (pip install python-dotenv)

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
            import json
            return {"state": row["state"], "data": json.loads(row["data"]) if row["data"] else {}}
        return None

def set_user_state(user_id, state, data=None):
    import json
    with get_db_connection() as conn:
        cursor = conn.cursor()
        current_data_json = json.dumps(data if data is not None else {})
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
    # 予約時間帯の開始と終了を定義 (例: 予約日時の前後30分など、店舗の運用に合わせる)
    # ここでは簡単のため、同一時刻のみをチェック
    reservation_datetime_iso_exact = reservation_datetime_obj.isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM reservations
            WHERE reservation_datetime = ? AND status = 'confirmed'
        ''', (reservation_datetime_iso_exact,))
        count = cursor.fetchone()[0]
        return count

def is_store_open(reservation_datetime_obj):
    """指定された日時が営業時間内か判定"""
    reservation_time = reservation_datetime_obj.time()
    # 注意: 日付をまたぐ営業時間は別途考慮が必要
    return STORE_OPEN_TIME <= reservation_time < STORE_CLOSE_TIME

def is_valid_reservation_minute(reservation_datetime_obj):
    """予約時刻の分が予約間隔に合致するか"""
    return reservation_datetime_obj.minute % RESERVATION_INTERVAL_MINUTES == 0


# --- 3. LINE メッセージテンプレート作成ヘルパー ---
def create_confirm_template(text, yes_label, yes_data, no_label, no_data):
    return TemplateMessage(
        alt_text=text.split('\n')[0], # 最初の行を代替テキストに
        template=ConfirmTemplate(
            text=text,
            actions=[
                PostbackAction(label=yes_label, data=yes_data, display_text=yes_label),
                PostbackAction(label=no_label, data=no_data, display_text=no_label)
            ]
        )
    )

def create_datetime_picker(action_label="日時を選択", postback_data="select_datetime"):
    return QuickReply(
        items=[
            QuickReplyItem(action=DatetimePickerAction(label=action_label, data=postback_data, mode="datetime"))
        ]
    )

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
        set_user_state(user_id, "ASKING_DATETIME", {})
        messages_to_reply.append(TextMessage(text="ご予約ですね。ご希望の日時を選択してください。", quick_reply=create_datetime_picker()))
    elif current_state == "ASKING_PEOPLE":
        try:
            num_people = int(text)
            if not (1 <= num_people <= 10): # 例: 1名から10名まで
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
            delete_user_state(user_id) # エラー時は状態をリセット
    # ... 他の状態やコマンドに対する処理 ...
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

    if postback_data == "select_datetime":
        if current_state != "ASKING_DATETIME":
            # 予期せぬタイミングでの日時選択
            messages_to_reply.append(TextMessage(text="予期せぬ操作です。最初から「予約」と入力してください。"))
            delete_user_state(user_id)
        else:
            selected_datetime_str = event.postback.params.get('datetime')
            if selected_datetime_str:
                try:
                    selected_dt = datetime.strptime(selected_datetime_str, '%Y-%m-%dT%H:%M')

                    if selected_dt < datetime.now() + timedelta(minutes=30): # 30分後以降の予約のみ
                        messages_to_reply.append(TextMessage(text="過去の日時、または直近すぎる時間は指定できません。30分後以降でお願いします。"))
                    elif not is_store_open(selected_dt):
                        messages_to_reply.append(TextMessage(text=f"申し訳ありません。その時間は営業時間外です。\n(営業時間: {STORE_OPEN_TIME.strftime('%H:%M')}～{STORE_CLOSE_TIME.strftime('%H:%M')})"))
                    elif not is_valid_reservation_minute(selected_dt):
                        messages_to_reply.append(TextMessage(text=f"申し訳ありません。ご予約は{RESERVATION_INTERVAL_MINUTES}分単位で承っております。(例: 10:00, 10:30)"))
                    else:
                        # 空き状況チェック
                        num_existing_reservations = count_reservations_for_datetime(selected_dt)
                        if num_existing_reservations >= MAX_RESERVATIONS_PER_SLOT:
                            messages_to_reply.append(TextMessage(text="申し訳ありません。その時間帯は既に満席です。別の日時をお試しください。"))
                        else:
                            user_data["datetime_obj_iso"] = selected_dt.isoformat() # ISO形式で保存
                            set_user_state(user_id, "ASKING_PEOPLE", user_data)
                            dt_display_str = selected_dt.strftime('%Y年%m月%d日 %H時%M分')
                            messages_to_reply.append(TextMessage(text=f"{dt_display_str}ですね。次に、ご希望の人数を半角数字で入力してください。(例: 2)"))

                except ValueError:
                    messages_to_reply.append(TextMessage(text="日時の形式が正しくありません。もう一度選択してください。"))
                except Exception as e:
                    app.logger.error(f"Error processing datetime selection: {e}")
                    messages_to_reply.append(TextMessage(text="日時の処理中にエラーが発生しました。"))
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