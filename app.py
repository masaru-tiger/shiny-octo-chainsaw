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
import urllib.parse

# --- データベース接続設定 ---
def init_connection():
    try:
        db_conf = st.secrets["database"]
        # パスワードを安全にエンコード（特殊文字対策）
        safe_password = urllib.parse.quote_plus(db_conf['password'])
        
        # 【修正ポイント】?pgbouncer=true を削除し、ポート6543を指定
        db_url = (
            f"postgresql://{db_conf['user']}:{safe_password}@"
            f"{db_conf['host']}:6543/{db_conf['database']}"
        )
        
        # SSL接続を必須にする設定を追加して安定化
        return create_engine(
            db_url, 
            connect_args={'sslmode': 'require'}, 
            pool_pre_ping=True
        )
    except Exception as e:
        st.error(f"接続設定エラー: {e}")
        st.stop()

engine = init_connection()

# --- セキュリティ ---
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

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

# --- 画面描画 ---
def show_dashboard(user_info):
    view_id = st.session_state.view_group_id
    update_inventory_by_time(view_id)
    
    st.header(f"📊 在庫ダッシュボード ({view_id})")
    
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM items WHERE group_id=:gid"), conn, params={"gid": view_id})
    
    if df.empty:
        st.info("登録されている在庫はありません。登録・スキャンから始めてください。")
        return

    # ステータス判定
    def get_status(row):
        days_left = row['quantity'] / row['daily_rate'] if row['daily_rate'] > 0 else 999
        if row['quantity'] <= row['threshold']: return "🔴 もうすぐ切れる"
        elif days_left <= 3: return "🟡 そろそろ切れる"
        else: return "🔵 まだ余裕ある"
    
    df['status'] = df.apply(get_status, axis=1)
    
    # 表示項目を「分類」優先に
    cols = st.columns(3)
    states = ["🔴 もうすぐ切れる", "🟡 そろそろ切れる", "🔵 まだ余裕ある"]
    for i, state in enumerate(states):
        with cols[i]:
            st.subheader(state)
            sub_df = df[df['status'] == state]
            if not sub_df.empty:
                # 分類を表示し、詳細として銘柄名を出す
                for _, row in sub_df.iterrows():
                    st.write(f"**{row['category']}** ({row['quantity']}{row['capacity']})")
                    st.caption(f"最終登録銘柄: {row['name']}")
                    st.divider()

def show_registration(user_info):
    view_id = st.session_state.view_group_id
    st.header("🛒 在庫登録・スキャン")
    
    reg_mode = st.radio("登録モード", ["日常の購入（在庫加算）", "新規分類・商品の登録"], horizontal=True)
    img_file = st.camera_input("バーコードスキャン")
    
    scanned_jan, auto_name = "", ""
    if img_file:
        img = Image.open(img_file)
        decoded_objs = decode(img)
        if decoded_objs:
            scanned_jan = decoded_objs[0].data.decode('utf-8')
            st.success(f"JAN検知: {scanned_jan}")
            with engine.connect() as conn:
                item = conn.execute(text("SELECT category, name FROM items WHERE name LIKE :name AND group_id=:gid"), 
                                    {"name": f"%{scanned_jan}%", "gid": view_id}).fetchone()
            if item:
                st.info(f"登録済み：【{item[0]}】{item[1]}")
                auto_name = item[1]
                auto_cat = item[0]
            else:
                with st.spinner('商品名検索中...'):
                    auto_name = search_product_by_jan(scanned_jan)
                    auto_cat = ""

    st.divider()

    with st.form("inventory_form", clear_on_submit=True):
        if reg_mode == "新規分類・商品の登録":
            f_cat = st.text_input("分類 (例: 牛乳, ティッシュ)", placeholder="同じ分類なら在庫が合算されます")
            f_jan = st.text_input("JANコード", value=scanned_jan)
            f_name = st.text_input("具体的な商品名", value=auto_name)
            f_cap = st.text_input("単位 (例: 本, パック, kg)", value="個")
            col1, col2, col3 = st.columns(3)
            f_qty = col1.number_input("現在数", min_value=0.0, value=1.0)
            f_rate = col2.number_input("1日の消費", min_value=0.0, value=0.1)
            f_alert = col3.number_input("警告しきい値", min_value=0.0, value=1.0)
            
            if st.form_submit_button("新規マスタ登録"):
                if not f_cat or not f_name:
                    st.error("分類と商品名は必須です")
                else:
                    with engine.begin() as conn:
                        conn.execute(text("""
                            INSERT INTO items (group_id, category, name, jan_code,capacity, quantity, daily_rate, threshold, last_updated) 
                            VALUES (:gid, :cat, :name, :cap, :qty, :rate, :alert, :today)
                        """), {"gid": view_id, "cat": f_cat, "name": f_name, "jan": f_jan, "cap": f_cap, "qty": f_qty, "rate": f_rate, "alert": f_alert, "today": datetime.now().date()})
                    st.success(f"「{f_cat}」を新規登録しました！")
        
        else:
            # 在庫加算モード：分類名で検索して合算
            with engine.connect() as conn:
                cats = conn.execute(text("SELECT DISTINCT category FROM items WHERE group_id=:gid"), {"gid": view_id}).fetchall()
            cat_list = [c[0] for c in cats]
            
            f_search_cat = st.selectbox("加算する分類を選択", ["直接入力/スキャンで検索"] + cat_list)
            f_manual = st.text_input("または分類名を直接入力", value=auto_name if auto_name else "")
            f_add_qty = st.number_input("追加数", min_value=1.0, value=1.0, step=1.0)
            
            if st.form_submit_button("在庫を加算"):
                target = f_search_cat if f_search_cat != "直接入力/スキャンで検索" else f_manual
                with engine.begin() as conn:
                    # 分類名(category)または商品名(name)で部分一致検索
                    res = conn.execute(text("""
                        UPDATE items SET quantity = quantity + :add, last_updated = :today 
                        WHERE (category = :target OR name LIKE :t_like) AND group_id = :gid
                    """), {"add": f_add_qty, "today": datetime.now().date(), "target": target, "t_like": f"%{target}%", "gid": view_id})
                
                if res.rowcount > 0:
                    st.success(f"「{target}」の在庫を増やしました！")
                else:
                    st.error("該当する分類が見つかりません。「新規登録」を先に行ってください。")

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

def show_login_screen():
    st.title("📦 Smart Stock System v2")
    tab1, tab2 = st.tabs(["ログイン", "新規登録"])
    with tab1:
        with st.form("login"):
            un = st.text_input("ユーザー名")
            pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("ログイン"):
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT id, username, password, group_id, role FROM users WHERE username=:un"), {"un": un}).fetchone()
                if row and row[2] == hash_password(pw):
                    st.session_state.logged_in = True
                    st.session_state.user_info = {'id': row[0], 'username': row[1], 'group_id': row[3], 'role': row[4]}
                    st.session_state.view_group_id = row[3]
                    st.rerun()
                else:
                    st.error("ログイン失敗")
    with tab2:
        with st.form("signup"):
            new_un = st.text_input("新ユーザー名")
            new_pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("作成"):
                new_gid = str(uuid.uuid4())[:8]
                with engine.begin() as conn:
                    conn.execute(text("INSERT INTO users (username, password, group_id, role) VALUES (:un, :pw, :gid, :r)"),
                                 {"un": new_un, "pw": hash_password(new_pw), "gid": new_gid, "r": 'user'})
                st.success("作成しました！ログインしてください")

def main():
    st.set_page_config(page_title="Smart Stock", layout="wide")
    if 'logged_in' not in st.session_state: st.session_state.logged_in = False
    if not st.session_state.logged_in:
        show_login_screen()
    else:
        user = st.session_state.user_info
        st.sidebar.write(f"👤 {user['username']}")
        menu = st.sidebar.radio("メニュー", ["ダッシュボード", "登録・スキャン", "編集・削除"])
        if st.sidebar.button("ログアウト"):
            st.session_state.logged_in = False
            st.rerun()
        
        if menu == "ダッシュボード": show_dashboard(user)
        elif menu == "登録・スキャン": show_registration(user)
        elif menu == "編集・削除": show_edit_delete(user)

if __name__ == "__main__":
    main()
