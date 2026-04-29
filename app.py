import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta, timezone
from PIL import Image
from pyzbar.pyzbar import decode
import requests
from bs4 import BeautifulSoup
import uuid
import hashlib
import urllib.parse
from fastapi import FastAPI, Request
import uvicorn
from threading import Thread
import time

# --- データベース接続設定 ---
def init_connection():
    try:
        db_conf = st.secrets["database"]
        safe_password = urllib.parse.quote_plus(db_conf['password'])
        db_url = (
            f"postgresql://{db_conf['user']}:{safe_password}@"
            f"{db_conf['host']}:6543/{db_conf['database']}"
        )
        return create_engine(
            db_url, 
            connect_args={'sslmode': 'require'}, 
            pool_pre_ping=True
        )
    except Exception as e:
        st.error(f"接続設定エラー: {e}")
        st.stop()

engine = init_connection()

# ==========================================
# セキュリティ & ハッシュ化の設定
# ==========================================
SALT = "koala_secure_2026"  # 共通のソルト（秘密の合言葉）

def hash_data(raw_string):
    """
    パスワードやLINE IDなど、すべての秘匿情報を
    共通のソルトを加えてハッシュ化する
    """
    if not raw_string:
        return ""
    return hashlib.sha256((raw_string + SALT).encode()).hexdigest()

# ==========================================
# FastAPI (Webhook受信用) の設定
# ==========================================
api = FastAPI()

@api.post("/webhook")
async def line_webhook(request: Request):
    body = await request.json()
    try:
        for event in body.get("events", []):
            if event["type"] == "message":
                user_id = event["source"]["userId"]
                # 到着時刻と共にキューに保存
                st.session_state.webhook_queue.append({
                    "user_id": user_id,
                    "received_at": datetime.now()
                })
    except Exception:
        pass
    return {"status": "ok"}

def start_webhook_server():
    # Streamlitとは別のポート(8000)でWebサーバーを起動
    uvicorn.run(api, host="0.0.0.0", port=8000)


# --- 自動計算ロジック ---
def update_inventory_by_time(group_id):
    with engine.begin() as conn:
        items = conn.execute(text("SELECT id, quantity, daily_rate, last_updated FROM items WHERE group_id=:gid"), 
                             {"gid": group_id}).fetchall()
        today = datetime.now().date()
        for item in items:
            item_id, qty, rate, last_up = item
            days_passed = (today - last_up).days
            if days_passed > 0:
                new_qty = max(0.0, float(qty) - (float(rate) * days_passed))
                conn.execute(text("UPDATE items SET quantity=:q, last_updated=:d WHERE id=:id"), 
                             {"q": new_qty, "d": today, "id": item_id})

# --- JAN検索 ---
def search_product_by_jan(jan_code):
    url = f"https://www.google.com/search?q=JAN+{jan_code}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.find("h3").get_text() if soup.find("h3") else f"JAN:{jan_code}"
        return title
    except:
        return f"JAN:{jan_code}"

# --- 画面描画（ダッシュボード） ---
def show_dashboard(user_info):
    view_id = st.session_state.view_group_id
    update_inventory_by_time(view_id)
    st.header(f"📊 在庫ダッシュボード ({view_id})")
    
    with engine.connect() as conn:
        query = text("""
            SELECT category, SUM(quantity) as total_qty, MAX(capacity) as unit, 
                   SUM(daily_rate) as total_rate, MAX(threshold) as max_threshold, MAX(name) as latest_name
            FROM items WHERE group_id=:gid GROUP BY category
        """)
        df = pd.read_sql(query, conn, params={"gid": view_id})
    
    if df.empty:
        st.info("登録されている在庫はありません。")
        return

    def get_status(row):
        days_left = row['total_qty'] / row['total_rate'] if row['total_rate'] > 0 else 999
        if row['total_qty'] <= row['max_threshold']: return "🔴 もうすぐ切れる"
        elif days_left <= 3: return "🟡 そろそろ切れる"
        else: return "🔵 まだ余裕ある"
    
    df['status'] = df.apply(get_status, axis=1)
    cols = st.columns(3)
    states = ["🔴 もうすぐ切れる", "🟡 そろそろ切れる", "🔵 まだ余裕ある"]
    for i, state in enumerate(states):
        with cols[i]:
            st.subheader(state)
            sub_df = df[df['status'] == state]
            for _, row in sub_df.iterrows():
                st.write(f"**{row['category']}** ({round(row['total_qty'], 1)}{row['unit']})")
                st.caption(f"（最新登録：{row['latest_name']}）")
                st.divider()

# --- 画面描画（登録・スキャン） ---
def show_registration(user_info):
    view_id = st.session_state.view_group_id
    st.header("🛒 在庫登録・スキャン")
    reg_mode = st.radio("登録モード", ["日常の購入（在庫加算）", "新規分類・商品の登録"], horizontal=True)
    img_file = st.camera_input("バーコードスキャン")
    scanned_jan, auto_name, auto_cat = "", "", ""
    
    with engine.connect() as conn:
        existing_data = conn.execute(text("SELECT category, name, jan_code FROM items WHERE group_id=:gid"), {"gid": view_id}).fetchall()
    cat_list = sorted(list(set([r[0] for r in existing_data if r[0]])))

    if img_file:
        img = Image.open(img_file)
        decoded_objs = decode(img)
        if decoded_objs:
            scanned_jan = decoded_objs[0].data.decode('utf-8')
            match = next((r for r in existing_data if r[2] == scanned_jan), None)
            if match:
                auto_cat, auto_name = match[0], match[1]
                st.success(f"✅ 登録済み：【{auto_cat}】{auto_name}")
            else:
                with st.spinner('新規商品として検索中...'):
                    auto_name = search_product_by_jan(scanned_jan)
            
            # セッション同期
            if auto_cat in cat_list:
                st.session_state["cat_sel_add"] = auto_cat
                f_names = sorted(list(set([r[1] for r in existing_data if r[0] == auto_cat and r[1]])))
                st.session_state["name_sel_add"] = auto_name if auto_name in f_names else "（直接入力）"
            else:
                st.session_state["cat_sel_add"] = "（直接入力）"
                st.session_state["name_sel_add"] = "（直接入力）"

    st.divider()

    # モード切り替えとフォーム表示
    if reg_mode == "新規分類・商品の登録":
        with st.form("registration_form", clear_on_submit=True):
            st.subheader("🆕 新規マスタ登録")
            sel_cat = st.selectbox("既存の分類から選択", ["（直接入力する）"] + cat_list)
            f_cat = st.text_input("新規の分類名を入力", value=auto_cat) if sel_cat == "（直接入力する）" else sel_cat
            f_jan, f_name = st.text_input("JANコード", value=scanned_jan), st.text_input("具体的な商品名", value=auto_name)
            f_cap = st.text_input("単位", value="個")
            c1, c2, c3 = st.columns(3)
            f_qty, f_rate, f_alert = c1.text_input("現在数", "1.0"), c2.text_input("1日の消費", "0.1"), c3.text_input("警告しきい値", "1.0")
            if st.form_submit_button("新規マスタとして登録"):
                try:
                    with engine.begin() as conn:
                        conn.execute(text("INSERT INTO items (group_id, category, name, jan_code, capacity, quantity, daily_rate, threshold, last_updated) VALUES (:gid, :cat, :name, :jan, :cap, :qty, :rate, :alert, :today)"),
                                     {"gid": view_id, "cat": f_cat, "name": f_name, "jan": f_jan, "cap": f_cap, "qty": float(f_qty), "rate": float(f_rate), "alert": float(f_alert), "today": datetime.now().date()})
                    st.success("登録完了！")
                except: st.error("登録失敗")
    else:
        # 在庫加算ロジック
        st.subheader("➕ 在庫の加算")
        col_a, col_b = st.columns(2)
        sel_cat_add = col_a.selectbox("分類を選択", ["（直接入力）"] + cat_list, key="cat_sel_add")
        current_cat = col_a.text_input("分類を手入力", value=auto_cat, key="cat_manual_in") if sel_cat_add == "（直接入力）" else sel_cat_add
        filtered_names = sorted(list(set([r[1] for r in existing_data if r[0] == current_cat and r[1]])))
        sel_name_add = col_b.selectbox(f"「{current_cat}」内の商品を選択", ["（直接入力）"] + filtered_names, key="name_sel_add")
        current_name = col_b.text_input("商品名を手入力", value=auto_name, key="name_manual_in") if sel_name_add == "（直接入力）" else sel_name_add

        with st.form("addition_form", clear_on_submit=True):
            f_jan_add = st.text_input("JANコード", value=scanned_jan)
            f_add_qty = st.text_input("追加数", value="1.0")
            if st.form_submit_button("在庫を加算する"):
                try:
                    add_val = float(f_add_qty)
                    with engine.begin() as conn:
                        # 1. まず対象の item_id を特定
                        item = conn.execute(text("""
                            SELECT id, quantity, daily_rate FROM items 
                            WHERE group_id = :gid 
                            AND ((jan_code = :jan AND jan_code <> '') OR (category = :cat AND name = :name))
                        """), {"jan": f_jan_add, "cat": current_cat, "name": current_name, "gid": view_id}).fetchone()

                        if item:
                            target_item_id, current_qty, daily_rate = item
                            new_qty = current_qty + add_val
                
                            # 2. 在庫数を更新（上書き）
                            conn.execute(text("""
                                UPDATE items SET quantity = :q, last_updated = :today WHERE id = :id
                            """), {"q": new_qty, "today": datetime.now().date(), "id": target_item_id})
                
                            # 3. 履歴テーブルにレコードを挿入
                            conn.execute(text("""
                                INSERT INTO inventory_history (item_id, change_qty, action_type, group_id) 
                                VALUES (:item_id, :add, 'purchase', :gid)
                            """), {"item_id": target_item_id, "add": add_val, "gid": view_id})
                
                            # 4.次回購入日をとりあえず通知
                            st.success(f"在庫を加算しました！（現在庫: {new_qty}）")
                            if daily_rate > 0:
                                days_left = int(new_qty / daily_rate)

                                # Python側でも日本時間を基準にする（UTC+9）
                                from datetime import timezone
                                jst_now = datetime.now(timezone(timedelta(hours=9)))
                                next_date = jst_now + timedelta(days=days_left)
                                st.info(f"💡 次回の購入予定日は **{next_date.strftime('%Y/%m/%d')}** です（残り約{days_left}日分）")
                            else:
                                st.warning("⚠️ 1日の消費量が0に設定されているため、予測日を計算できません。")
                        else:
                            st.error("一致する商品がありません。")
                except Exception as e:
                    st.error(f"エラーが発生しました: {e}")

# --- 画面描画（編集・削除） ---
def show_edit_delete(user_info):
    view_id = st.session_state.view_group_id
    st.header("🔧 在庫の編集・削除")
    with engine.connect() as conn:
        items = conn.execute(text("SELECT id, category, name, quantity FROM items WHERE group_id=:gid"), {"gid": view_id}).fetchall()
    for item in items:
        item_id, cat, name, qty = item
        with st.expander(f"📦 【{cat}】 {name}"):
            new_qty = st.number_input("数量修正", value=float(qty), key=f"ed_{item_id}")
            col1, col2 = st.columns(2)
            if col1.button("保存", key=f"sv_{item_id}"):
                with engine.begin() as conn:
                    conn.execute(text("UPDATE items SET quantity=:q, last_updated=:t WHERE id=:id"), {"q": new_qty, "t": datetime.now().date(), "id": item_id})
                st.rerun()
            if col2.button("🗑️ 削除", key=f"dl_{item_id}"):
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM items WHERE id=:id"), {"id": item_id})
                st.rerun()

# --- 管理者専用：DBメンテナンス画面 ---
def show_admin_tool():
    # --- 1. CSVダウンロードセクション (堅牢版) ---
    st.subheader("📥 データのバックアップ (CSV出力)")
    col_csv1, col_csv2, col_csv3 = st.columns(3)
    
    with engine.connect() as conn:
        # 在庫マスタ
        df_items = pd.read_sql("SELECT * FROM items ORDER BY id ASC", conn)
        # ユーザーデータ
        df_users = pd.read_sql("SELECT id, username, group_id, role FROM users ORDER BY id ASC", conn)
        
        # 履歴データの取得 (LEFT JOINで名前を紐付け。名前が取れなくても全行出す)
        df_history = pd.read_sql("""
            SELECT 
                (h.created_at + INTERVAL '9 hours') as "更新日時",
                i.category as "分類",
                i.name as "商品名",
                h.change_qty as "加算数",
                i.capacity as "容量",
                h.group_id as "実行グループ",
                h.item_id as "アイテムID"
            FROM inventory_history h
            LEFT JOIN items i ON h.item_id = i.id
            ORDER BY h.created_at DESC
        """, conn)

    # 日本時間への変換（created_atが文字列やUTCの場合の対策）
    if not df_history.empty:
        df_history["更新日時"] = pd.to_datetime(df_history["更新日時"]).dt.strftime('%Y-%m-%d %H:%M:%S')

    with col_csv1:
        st.write("📦 在庫マスタ")
        if not df_items.empty:
            csv_items = df_items.to_csv(index=False).encode('utf-8-sig')
            st.download_button(label="在庫CSVを保存", data=csv_items, 
                               file_name=f"inventory_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
        else:
            st.warning("在庫データがありません")

    with col_csv2:
        st.write("👤 ユーザー")
        if not df_users.empty:
            csv_users = df_users.to_csv(index=False).encode('utf-8-sig')
            st.download_button(label="ユーザーCSVを保存", data=csv_users, 
                               file_name=f"users_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
        else:
            st.warning("ユーザーデータがありません")

    with col_csv3:
        st.write("📜 入庫履歴")
        if not df_history.empty:
            csv_history = df_history.to_csv(index=False).encode('utf-8-sig')
            st.download_button(label="履歴CSVを保存", data=csv_history, 
                               file_name=f"history_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
        else:
            # データが0件の場合のデバッグ表示
            st.error("履歴が0件です。更新を試してください。")
            # 念のため生テーブルに直接問い合わせてみるボタン（デバッグ用）
            if st.button("生データの存在を確認"):
                with engine.connect() as conn:
                    raw_check = pd.read_sql("SELECT count(*) FROM inventory_history", conn)
                    st.write(f"DB内の生データ件数: {raw_check.iloc[0,0]}件")

    st.divider()

    # --- 2. 直近の履歴表示 ---
    st.subheader("📜 最近の在庫更新履歴")
    if not df_history.empty:
        # 表示用にコピーを作成
        display_history = df_history.copy()
        
        # 【修正箇所】SQLで名前を変えたため、"更新日時" カラムを参照する
        # すでに上のCSVセクションで変換済みの場合は、この処理はスキップまたは以下のように書きます
        try:
            # すでにフォーマット済みならそのまま、未フォーマットなら変換
            if "更新日時" in display_history.columns:
                st.dataframe(display_history.head(20), use_container_width=True)
            else:
                # 万が一 'created_at' のままだった場合の保険
                display_history['created_at'] = pd.to_datetime(display_history['created_at']).dt.strftime('%m/%d %H:%M')
                st.dataframe(display_history.head(20), use_container_width=True)
        except Exception:
            # 最悪、エラーを出さずにデータをそのまま表示する（フェールセーフ）
            st.dataframe(display_history.head(20), use_container_width=True)
    else:
        st.info("履歴データはまだありません。")

    st.divider()

    # --- 3. データ編集セクション（既存機能） ---
    try:
        st.subheader("📊 在庫データ修正（全グループ対象）")
        st.dataframe(df_items, use_container_width=True)
        
        st.divider()
        st.subheader("✏️ データの個別修正")
        target_id = st.selectbox("修正するアイテムの ID を選択", df_items['id'].tolist() if not df_items.empty else [])
        
        if target_id:
            row = df_items[df_items['id'] == target_id].iloc[0]
            with st.form("admin_edit_form"):
                c1, c2, c3 = st.columns(3)
                new_cat = c1.text_input("分類", value=row['category'])
                new_name = c1.text_input("商品名", value=row['name'])
                new_qty = c2.number_input("現在数", value=float(row['quantity']))
                new_rate = c2.number_input("1日の消費", value=float(row['daily_rate']))
                new_gid = c3.text_input("グループID", value=row['group_id'])
                new_jan = c3.text_input("JANコード", value=row['jan_code'])
                
                if st.form_submit_button("🚀 データベースを更新"):
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE items SET category=:cat, name=:name, quantity=:q, 
                            daily_rate=:r, group_id=:gid, jan_code=:jan, last_updated=:today 
                            WHERE id=:id
                        """), {
                            "cat": new_cat, "name": new_name, "q": new_qty, "r": new_rate, 
                            "gid": new_gid, "jan": new_jan, "today": datetime.now().date(), "id": target_id
                        })
                    st.success("更新しました")
                    st.rerun()
    except Exception as e:
        st.error(f"エラーが発生しました: {e}")


# ==========================================
# LINE連携 案内画面 (UI)
# ==========================================
def show_line_linking_flow(username):
    st.title("🔗 LINE連携の設定")
    
    # セッション状態の管理
    if "link_status" not in st.session_state:
        st.session_state.link_status = "ask"

    if st.session_state.link_status == "ask":
        st.write(f"**{username}** さん、アカウント作成ありがとうございます！")
        st.write("在庫が少なくなった時にLINEで通知を受け取れるようにしますか？")
        c1, c2 = st.columns(2)
        if c1.button("はい、連携する"):
            st.session_state.link_status = "waiting"
            st.rerun()
        if c2.button("今はしない（ダッシュボードへ）"):
            st.session_state.logged_in = True
            st.rerun()

    elif st.session_state.link_status == "waiting":
        st.info("以下の手順で操作してください：")
        st.markdown("""
        1. 公式アカウントを**友だち追加**する（下のQRコードから）
        2. トーク画面で **「hello」** と送信する
        3. 送信後、下の **「送信しました」** ボタンを押す
        """)
        
        # QRコード表示（LINE Developersから取得したURLを入れる）
        st.image("QRcode_LINE.png", width=250)
        
        if st.button("✅ メッセージを送信しました"):
            # 「一時預かり所」をチェック（直近2分以内のデータを探す）
            now = datetime.now()
            found_id = None
            
            # キューを新しい順に確認
            for entry in reversed(st.session_state.webhook_queue):
                if (now - entry["received_at"]).seconds < 120:
                    found_id = entry["user_id"]
                    break
            
            if found_id:
                hashed_id = hash_data(found_id)
                # DBに保存
                try:
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE users SET line_user_id = :lid WHERE username = :un"),
                                     {"lid": hashed_id, "un": username})
                    st.success("🎉 LINE連携が完了しました！")
                    time.sleep(2)
                    st.session_state.logged_in = True
                    st.rerun()
                except Exception as e:
                    st.error(f"DB保存エラー: {e}")
            else:
                st.error("メッセージが確認できませんでした。公式アカウントに「hello」と送りましたか？")
                st.caption("※サーバーに届くまで数秒かかる場合があります。少し待ってから再度押してください。")
                
# --- ログイン・メイン制御 ---
def show_login_screen():
    st.title("🐨 ストックコアラ v2")
    tab1, tab2 = st.tabs(["ログイン", "新規登録"])
    with tab1:
        with st.form("login"):
            un, pw = st.text_input("ユーザー名"), st.text_input("パスワード", type="password")
            if st.form_submit_button("ログイン"):
                # 管理者ログイン判定＝ admin / admin
                if un == "admin" and pw == "admin":
                    st.session_state.logged_in = True
                    st.session_state.is_admin = True
                    st.session_state.user_info = {'username': 'システム管理者', 'group_id': 'ADMIN', 'role': 'admin'}
                    st.session_state.view_group_id = 'ADMIN'
                    st.rerun()
                
                # 通常ログイン
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT id, username, password, group_id, role FROM users WHERE username=:un"), {"un": un}).fetchone()
                
                if row and row[2] == hash_data(pw):
                    st.session_state.logged_in, st.session_state.is_admin = True, False
                    st.session_state.user_info = {'id': row[0], 'username': row[1], 'group_id': row[3], 'role': row[4]}
                    st.session_state.view_group_id = row[3]
                    st.rerun()
                else: 
                    st.error("ログイン失敗：ユーザー名またはパスワードが正しくありません")
with tab2:
        # 新規登録後のLINE連携フラグ
        if "new_user_created" not in st.session_state:
            with st.form("signup"):
                new_un, new_pw = st.text_input("新ユーザー名"), st.text_input("パスワード", type="password")
                if st.form_submit_button("アカウント作成"):
                    new_gid = str(uuid.uuid4())[:8]
                    with engine.begin() as conn:
                        conn.execute(text("INSERT INTO users (username, password, group_id, role) VALUES (:un, :pw, :gid, :r)"),
                                     {"un": new_un, "pw": hash_data(new_pw), "gid": new_gid, "r": 'user'})
                    st.session_state.new_user_created = new_un
                    st.rerun()
        else:
            # 連携フローを表示
            show_line_linking_flow(st.session_state.new_user_created)

def main():
    # グローバルな「一時預かり所」　※メインで実装することで確実に実行する 
    if "webhook_queue" not in st.session_state:
        st.session_state.webhook_queue = []
    # FastAPIサーバーをバックグラウンドで1回だけ起動
    if "api_started" not in st.session_state:
        thread = Thread(target=start_webhook_server, daemon=True)
        thread.start()
        st.session_state.api_started = True
        
    st.set_page_config(page_title="Smart Stock", layout="wide")
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
    if 'is_admin' not in st.session_state: 
        st.session_state.is_admin = False
    
    if not st.session_state.logged_in:
        show_login_screen()
    else:
        user = st.session_state.user_info
        st.sidebar.write(f"👤 {user['username']}")
        
        menu_list = ["ダッシュボード", "登録・スキャン", "編集・削除"]
        if st.session_state.is_admin:
            menu_list.append("🛠 DBメンテナンス")
            
        menu = st.sidebar.radio("メニュー", menu_list)
        if st.sidebar.button("ログアウト"):
            st.session_state.logged_in = False
            st.rerun()
        
        if menu == "ダッシュボード": show_dashboard(user)
        elif menu == "登録・スキャン": show_registration(user)
        elif menu == "編集・削除": show_edit_delete(user)
        elif menu == "🛠 DBメンテナンス": show_admin_tool()

if __name__ == "__main__":
    main()
