import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime
from PIL import Image
from pyzbar.pyzbar import decode
import requests
from bs4 import BeautifulSoup
import uuid
import hashlib

# --- データベース初期設定 (Supabase対応) ---
def init_connection():
    try:
        db_url = st.secrets["db_url"]
        # パスワードに特殊文字がある場合の対策として、明示的にドライバーを指定
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        
        return create_engine(db_url, pool_pre_ping=True)
    except Exception as e:
        st.error(f"接続設定エラー: {e}")
        st.stop()

def init_db():
    """テーブルの初期化（PostgreSQL形式）"""
    with engine.begin() as conn:
        # ユーザーテーブル (idはSERIALを使用)
        conn.execute(text('''CREATE TABLE IF NOT EXISTS users 
                           (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT, group_id TEXT, role TEXT)'''))
        # 在庫テーブル
        conn.execute(text('''CREATE TABLE IF NOT EXISTS items 
                           (id SERIAL PRIMARY KEY, group_id TEXT, name TEXT, capacity TEXT, 
                            quantity REAL, daily_rate REAL, threshold REAL, last_updated DATE)'''))

init_db()

# --- セキュリティ: パスワードのハッシュ化 ---
def hash_password(password):
    """パスワードをSHA-256でハッシュ化する"""
    return hashlib.sha256(password.encode()).hexdigest()

# --- 自動計算ロジック ---
def update_inventory_by_time(group_id):
    with engine.begin() as conn:
        items = conn.execute(text("SELECT id, name, quantity, daily_rate, last_updated FROM items WHERE group_id=:gid"), 
                             {"gid": group_id}).fetchall()
        
        today = datetime.now().date()
        for item in items:
            item_id, name, qty, rate, last_up = item
            # PostgreSQLのDate型は既にdateオブジェクトとして返ることが多いですが念のため
            last_up_date = last_up if isinstance(last_up, datetime) or hasattr(last_up, 'days') else datetime.strptime(str(last_up), '%Y-%m-%d').date()
            if hasattr(last_up_date, 'date'): last_up_date = last_up_date.date()

            days_passed = (today - last_up_date).days
            if days_passed > 0:
                new_qty = max(0.0, float(qty) - (float(rate) * days_passed))
                conn.execute(text("UPDATE items SET quantity=:q, last_updated=:d WHERE id=:id"), 
                             {"q": new_qty, "d": today, "id": item_id})

# --- ヘルパー関数 ---
def get_status(quantity, daily_rate, threshold):
    days_left = quantity / daily_rate if daily_rate > 0 else 999
    if quantity <= threshold: return "🔴 もうすぐ切れる"
    elif days_left <= 3: return "🟡 そろそろ切れる"
    else: return "🔵 まだ余裕ある"

def search_product_by_jan(jan_code):
    url = f"https://www.google.com/search?q=JAN+{jan_code}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.find("h3").get_text() if soup.find("h3") else f"JAN:{jan_code}"
        return title, ""
    except:
        return f"JAN:{jan_code}", ""

# --- 画面描画関数 ---

def show_dashboard(user_info):
    view_id = st.session_state.view_group_id
    update_inventory_by_time(view_id)
    
    st.header(f"📊 在庫ダッシュボード (家庭ID: {view_id})")
    
    query = text("SELECT * FROM items WHERE group_id=:gid")
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"gid": view_id})
    
    if df.empty:
        st.info("登録されている在庫はありません。")
        return

    df['status'] = df.apply(lambda row: get_status(row['quantity'], row['daily_rate'], row['threshold']), axis=1)
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.subheader("🔴 もうすぐ切れる")
        urgent = df[df['status'] == "🔴 もうすぐ切れる"]
        if not urgent.empty:
            st.write(urgent[['name', 'quantity', 'capacity']])
            st.warning("🛒 買い物リスト対象")
    with col2:
        st.subheader("🟡 そろそろ切れる")
        st.write(df[df['status'] == "🟡 そろそろ切れる"][['name', 'quantity']])
    with col3:
        st.subheader("🔵 まだ余裕ある")
        st.write(df[df['status'] == "🔵 まだ余裕ある"][['name', 'quantity']])

def show_registration(user_info):
    view_id = st.session_state.view_group_id
    st.header(f"🛒 在庫登録・スキャン (対象: {view_id})")
    
    reg_mode = st.radio("登録モードを選択", ["日常の購入登録", "新規商品のマスタ登録"], horizontal=True)
    img_file = st.camera_input("バーコードをスキャン")
    
    scanned_jan, auto_name, auto_cap = "", "", ""
    if img_file:
        img = Image.open(img_file)
        decoded_objs = decode(img)
        if decoded_objs:
            scanned_jan = decoded_objs[0].data.decode('utf-8')
            st.success(f"JANコードを検知: {scanned_jan}")
            with engine.connect() as conn:
                existing_item = conn.execute(text("SELECT name, capacity FROM items WHERE name LIKE :name AND group_id=:gid"), 
                                             {"name": f"%{scanned_jan}%", "gid": view_id}).fetchone()
            if existing_item:
                auto_name, auto_cap = existing_item
                st.info(f"登録済み商品: {auto_name}")
            else:
                with st.spinner('ネット検索中...'):
                    auto_name, auto_cap = search_product_by_jan(scanned_jan)

    st.divider()
    with st.form("inventory_form", clear_on_submit=True):
        if reg_mode == "新規商品のマスタ登録":
            f_jan = st.text_input("JANコード", value=scanned_jan)
            f_name = st.text_input("商品名", value=auto_name)
            f_cap = st.text_input("容量", value=auto_cap)
            f_qty = st.number_input("現在の在庫数量", min_value=0.0, value=1.0, step=0.1)
            f_rate = st.number_input("1日の消費ペース", min_value=0.0, value=0.1)
            f_alert = st.number_input("アラート閾値", min_value=0.0, value=1.0)
            if st.form_submit_button("新規マスタとして登録"):
                with engine.begin() as conn:
                    conn.execute(text("INSERT INTO items (group_id, name, capacity, quantity, daily_rate, threshold, last_updated) VALUES (:gid, :name, :cap, :qty, :rate, :alert, :today)"), 
                                 {"gid": view_id, "name": f"{f_name} ({f_jan})", "cap": f_cap, "qty": f_qty, "rate": f_rate, "alert": f_alert, "today": datetime.now().date()})
                st.success("マスタ登録完了！")
        else:
            f_name_daily = st.text_input("商品名/JAN", value=auto_name if auto_name else scanned_jan)
            f_add_qty = st.number_input("追加数量", min_value=1.0, value=1.0)
            if st.form_submit_button("在庫を加算"):
                with engine.begin() as conn:
                    res = conn.execute(text("UPDATE items SET quantity = quantity + :add, last_updated = :today WHERE name LIKE :name AND group_id = :gid"), 
                                       {"add": f_add_qty, "today": datetime.now().date(), "name": f"%{f_name_daily}%", "gid": view_id})
                if res.rowcount > 0:
                    st.success("加算しました！")
                else:
                    st.error("商品が見つかりません。マスタ登録を先にしてください。")

def show_edit_delete(user_info):
    view_id = st.session_state.view_group_id
    st.header("🔧 在庫の編集・削除")
    with engine.connect() as conn:
        items = conn.execute(text("SELECT id, name, capacity, quantity, daily_rate, threshold FROM items WHERE group_id=:gid"), {"gid": view_id}).fetchall()

    for item in items:
        item_id, name, cap, qty, rate, thresh = item
        with st.expander(f"📦 {name} (現在: {qty}{cap})"):
            if st.button("🗑️ 削除", key=f"del_{item_id}"):
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM items WHERE id=:id"), {"id": item_id})
                st.rerun()
            new_qty = st.number_input("修正数量", value=float(qty), key=f"edit_{item_id}")
            if st.button("保存", key=f"save_{item_id}"):
                with engine.begin() as conn:
                    conn.execute(text("UPDATE items SET quantity=:qty, last_updated=:today WHERE id=:id"), 
                                 {"qty": new_qty, "today": datetime.now().date(), "id": item_id})
                st.rerun()

def show_admin_settings():
    st.header("🛡️ システム管理者ダッシュボード")
    with engine.connect() as conn:
        all_items = pd.read_sql(text("SELECT * FROM items"), conn)
    csv = all_items.to_csv(index=False).encode('utf-8_sig')
    st.download_button("全データをCSVで保存", data=csv, file_name="all_inventory.csv", mime="text/csv")
    st.dataframe(all_items)

def show_login_screen():
    st.title("📦 Smart Stock System")
    tab1, tab2 = st.tabs(["ログイン", "新規アカウント作成"])
    with tab1:
        with st.form("login"):
            un = st.text_input("ユーザー名")
            pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("ログイン"):
                # パスワードをハッシュ化して比較
                hashed_pw = hash_password(pw)
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT id, username, group_id, role FROM users WHERE username=:un AND password=:pw"), 
                                       {"un": un, "pw": hashed_pw}).fetchone()
                if row:
                    st.session_state.logged_in = True
                    st.session_state.user_info = {'id': row[0], 'username': row[1], 'group_id': row[2], 'role': row[3]}
                    st.session_state.view_group_id = row[2]
                    st.rerun()
                else:
                    st.error("ユーザー名またはパスワードが正しくありません")
    with tab2:
        with st.form("signup"):
            new_un = st.text_input("新ユーザー名")
            new_pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("作成"):
                with engine.connect() as conn:
                    exist = conn.execute(text("SELECT * FROM users WHERE username=:un"), {"un": new_un}).fetchone()
                if exist:
                    st.error("そのユーザー名は既に使用されています")
                else:
                    new_gid = str(uuid.uuid4())[:8]
                    role = 'admin' if new_un == 'admin' else 'user'
                    # パスワードをハッシュ化して保存
                    hashed_pw = hash_password(new_pw)
                    with engine.begin() as conn:
                        conn.execute(text("INSERT INTO users (username, password, group_id, role) VALUES (:un, :pw, :gid, :role)"), 
                                     {"un": new_un, "pw": hashed_pw, "gid": new_gid, "role": role})
                    st.success(f"作成完了！ 権限: {role} / グループID: {new_gid}")

def show_sidebar_navigation():
    user = st.session_state.user_info
    st.sidebar.write(f"👤 {user['username']} ({user['role']})")
    menu_options = ["ダッシュボード", "在庫登録・スキャン", "在庫の編集・削除"]
    if user['role'] == 'admin': menu_options.append("管理者設定")
    menu = st.sidebar.radio("メニュー", menu_options)
    
    if st.sidebar.button("ログアウト"):
        st.session_state.logged_in = False
        st.rerun()

    if user['role'] == 'admin':
        st.sidebar.divider()
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT DISTINCT group_id, username FROM users")).fetchall()
        all_groups = {f"{r[1]}の家庭": r[0] for r in rows}
        sel_label = st.sidebar.selectbox("表示切替", list(all_groups.keys()))
        if all_groups[sel_label] != st.session_state.view_group_id:
            st.session_state.view_group_id = all_groups[sel_label]
            st.rerun()

    if menu == "ダッシュボード": show_dashboard(user)
    elif menu == "在庫登録・スキャン": show_registration(user)
    elif menu == "在庫の編集・削除": show_edit_delete(user)
    elif menu == "管理者設定": show_admin_settings()

def main():
    st.set_page_config(page_title="Smart Stock PoV", layout="wide")
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
    if not st.session_state.logged_in:
        show_login_screen()
    else:
        show_sidebar_navigation()

if __name__ == "__main__":
    main()
