import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime
from PIL import Image
from pyzbar.pyzbar import decode
import requests
from bs4 import BeautifulSoup
import uuid

# --- データベース初期設定 ---
def init_db():
    conn = sqlite3.connect('data/inventory.db', check_same_thread=False)
    c = conn.cursor()
    # ユーザーテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY, username TEXT, password TEXT, group_id TEXT, role TEXT)''')
    # 在庫テーブル
    c.execute('''CREATE TABLE IF NOT EXISTS items 
                 (id INTEGER PRIMARY KEY, group_id TEXT, name TEXT, capacity TEXT, 
                  quantity REAL, daily_rate REAL, threshold REAL, last_updated DATE)''')
    conn.commit()
    return conn

conn = init_db()

# --- 自動計算ロジック (案A) ---
def update_inventory_by_time(group_id):
    """前回の更新日から今日までの経過日数分、在庫を自動で減らす"""
    c = conn.cursor()
    c.execute("SELECT id, name, quantity, daily_rate, last_updated FROM items WHERE group_id=?", (group_id,))
    items = c.fetchall()
    
    today = datetime.now().date()
    updated = False
    
    for item in items:
        item_id, name, qty, rate, last_up = item
        # 文字列の日付をdateオブジェクトに変換
        last_up_date = datetime.strptime(last_up, '%Y-%m-%d').date()
        
        days_passed = (today - last_up_date).days
        
        if days_passed > 0:
            # 在庫減少の計算（0以下にはしない）
            new_qty = max(0.0, qty - (rate * days_passed))
            c.execute("UPDATE items SET quantity=?, last_updated=? WHERE id=?", 
                      (new_qty, today, item_id))
            updated = True
    
    if updated:
        conn.commit()

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
    # 画面表示前に在庫を自動更新
    update_inventory_by_time(view_id)
    
    st.header(f"📊 在庫ダッシュボード (閲覧中の家庭ID: {view_id})")
    
    query = f"SELECT * FROM items WHERE group_id='{view_id}'"
    df = pd.read_sql(query, conn)
    
    if df.empty:
        st.info("登録されている在庫はありません。")
        return

    df['status'] = df.apply(lambda row: get_status(row['quantity'], row['daily_rate'], row['threshold']), axis=1)
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.subheader("🔴 もうすぐ切れる")
        urgent = df[df['status'] == "🔴 もうすぐ切れる"]
        st.write(urgent[['name', 'quantity', 'capacity']])
        if not urgent.empty:
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
            c = conn.cursor()
            c.execute("SELECT name, capacity FROM items WHERE name LIKE ? AND group_id=?", 
                      (f"%{scanned_jan}%", view_id))
            existing_item = c.fetchone()
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
                c = conn.cursor()
                c.execute("INSERT INTO items (group_id, name, capacity, quantity, daily_rate, threshold, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                          (view_id, f"{f_name} ({f_jan})", f_cap, f_qty, f_rate, f_alert, datetime.now().date()))
                conn.commit()
                st.success("マスタ登録完了！")
        else:
            f_name_daily = st.text_input("商品名/JAN", value=auto_name if auto_name else scanned_jan)
            f_add_qty = st.number_input("追加数量", min_value=1.0, value=1.0)
            if st.form_submit_button("在庫を加算"):
                c = conn.cursor()
                c.execute("UPDATE items SET quantity = quantity + ?, last_updated = ? WHERE name LIKE ? AND group_id = ?", 
                          (f_add_qty, datetime.now().date(), f"%{f_name_daily}%", view_id))
                if c.rowcount > 0:
                    conn.commit()
                    st.success("加算しました！")
                else:
                    st.error("商品が見つかりません。マスタ登録を先にしてください。")

def show_edit_delete(user_info):
    view_id = st.session_state.view_group_id
    st.header("🔧 在庫の編集・削除")
    c = conn.cursor()
    c.execute("SELECT id, name, capacity, quantity, daily_rate, threshold FROM items WHERE group_id=?", (view_id,))
    items = c.fetchall()

    for item in items:
        item_id, name, cap, qty, rate, thresh = item
        with st.expander(f"📦 {name} (現在: {qty}{cap})"):
            if st.button("🗑️ 削除", key=f"del_{item_id}"):
                c.execute("DELETE FROM items WHERE id=?", (item_id,))
                conn.commit()
                st.rerun()
            new_qty = st.number_input("修正数量", value=float(qty), key=f"edit_{item_id}")
            if st.button("保存", key=f"save_{item_id}"):
                c.execute("UPDATE items SET quantity=?, last_updated=? WHERE id=?", (new_qty, datetime.now().date(), item_id))
                conn.commit()
                st.rerun()

def show_admin_settings():
    st.header("🛡️ システム管理者ダッシュボード")
    # CSV出力
    all_items = pd.read_sql("SELECT * FROM items", conn)
    csv = all_items.to_csv(index=False).encode('utf-8_sig')
    st.download_button("全データをCSVで保存", data=csv, file_name="all_inventory.csv", mime="text/csv")
    st.subheader("データプレビュー")
    st.dataframe(all_items)

# --- ログイン・ナビゲーション ---

def main():
    st.set_page_config(page_title="Smart Stock PoV", layout="wide")
    
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.user_info = None

    if not st.session_state.logged_in:
        show_login_screen()
    else:
        show_sidebar_navigation()

def show_login_screen():
    st.title("📦 Smart Stock System")
    tab1, tab2 = st.tabs(["ログイン", "新規アカウント作成"])
    with tab1:
        with st.form("login"):
            un = st.text_input("ユーザー名")
            pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("ログイン"):
                c = conn.cursor()
                c.execute("SELECT * FROM users WHERE username=? AND password=?", (un, pw))
                row = c.fetchone()
                if row:
                    st.session_state.logged_in = True
                    st.session_state.user_info = {'id': row[0], 'username': row[1], 'group_id': row[3], 'role': row[4]}
                    st.session_state.view_group_id = row[3]
                    st.rerun()
                else:
                    st.error("不一致")
    with tab2:
        with st.form("signup"):
            new_un = st.text_input("新ユーザー名")
            new_pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("作成"):
                # 重複チェック
                c = conn.cursor()
                c.execute("SELECT * FROM users WHERE username=?", (new_un,))
                if c.fetchone():
                    st.error("そのユーザー名は既に使用されています")
                else:
                    new_gid = str(uuid.uuid4())[:8]
                    
                    # --- 権限の判定ロジック ---
                    # ユーザー名が 'admin' の場合のみ admin、それ以外は user
                    role = 'admin' if new_un == 'admin' else 'user'
                    
                    c.execute("INSERT INTO users (username, password, group_id, role) VALUES (?, ?, ?, ?)", 
                              (new_un, new_pw, new_gid, role))
                    conn.commit()
                    st.success(f"作成完了！ 権限: {role} / グループID: {new_gid}")

def show_sidebar_navigation():
    user = st.session_state.user_info
    st.sidebar.write(f"👤 {user['username']} ({user['role']})")
    
    # メニュー
    menu_options = ["ダッシュボード", "在庫登録・スキャン", "在庫の編集・削除"]
    if user['role'] == 'admin':
        menu_options.append("管理者設定")
    
    menu = st.sidebar.radio("メニュー", menu_options)
    
    if st.sidebar.button("ログアウト"):
        st.session_state.logged_in = False
        st.rerun()

    # 管理者用切替
    if user['role'] == 'admin':
        st.sidebar.divider()
        c = conn.cursor()
        c.execute("SELECT DISTINCT group_id, username FROM users")
        all_groups = {f"{r[1]}の家庭": r[0] for r in c.fetchall()}
        sel_label = st.sidebar.selectbox("表示切替", list(all_groups.keys()))
        if all_groups[sel_label] != st.session_state.view_group_id:
            st.session_state.view_group_id = all_groups[sel_label]
            st.rerun()

    # 表示分岐
    if menu == "ダッシュボード": show_dashboard(user)
    elif menu == "在庫登録・スキャン": show_registration(user)
    elif menu == "在庫の編集・削除": show_edit_delete(user)
    elif menu == "管理者設定": show_admin_settings()

if __name__ == "__main__":
    main()
