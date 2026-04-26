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
        # category ごとに quantity を合計（SUM）し、一番新しい情報を取得する
        query = text("""
            SELECT 
                category, 
                SUM(quantity) as total_qty, 
                MAX(capacity) as unit, 
                SUM(daily_rate) as total_rate, 
                MAX(threshold) as max_threshold,
                MAX(name) as latest_name
            FROM items 
            WHERE group_id=:gid 
            GROUP BY category
        """)
        df = pd.read_sql(query, conn, params={"gid": view_id})
    
    if df.empty:
        st.info("登録されている在庫はありません。")
        return

    # ステータス判定（合計値で判定）
    def get_status(row):
        days_left = row['total_qty'] / row['total_rate'] if row['total_rate'] > 0 else 999
        if row['total_qty'] <= row['max_threshold']: return "🔴 もうすぐ切れる"
        elif days_left <= 3: return "🟡 そろそろ切れる"
        else: return "🔵 まだ余裕ある"
    
    df['status'] = df.apply(get_status, axis=1)
    
    # 表示
    cols = st.columns(3)
    states = ["🔴 もうすぐ切れる", "🟡 そろそろ切れる", "🔵 まだ余裕ある"]
    for i, state in enumerate(states):
        with cols[i]:
            st.subheader(state)
            sub_df = df[df['status'] == state]
            for _, row in sub_df.iterrows():
                # 分類名を大きく表示し、合計数量を出す
                st.write(f"**{row['category']}** ({round(row['total_qty'], 1)}{row['unit']})")
                st.caption(f"（最新登録：{row['latest_name']}）")
                st.divider()
def show_registration(user_info):
    view_id = st.session_state.view_group_id
    st.header("🛒 在庫登録・スキャン")
    
    reg_mode = st.radio("登録モード", ["日常の購入（在庫加算）", "新規分類・商品の登録"], horizontal=True)
    img_file = st.camera_input("バーコードスキャン")
    
    # 自動入力用の変数初期化
    scanned_jan, auto_name, auto_cat = "", "", ""
    
    # 既存データをDBから取得
    with engine.connect() as conn:
        existing_data = conn.execute(
            text("SELECT category, name, jan_code FROM items WHERE group_id=:gid"), 
            {"gid": view_id}
        ).fetchall()
    
    cat_list = sorted(list(set([r[0] for r in existing_data if r[0]])))

    # スキャン処理と自動検索
    if img_file:
        img = Image.open(img_file)
        decoded_objs = decode(img)
        if decoded_objs:
            scanned_jan = decoded_objs[0].data.decode('utf-8')
            st.success(f"JAN検知: {scanned_jan}")
            
            # DB内をJANコードで検索
            match = next((r for r in existing_data if r[2] == scanned_jan), None)
            
            if match:
                # ヒットした場合：分類と商品名を自動セット
                auto_cat = match[0]
                auto_name = match[1]
                st.info(f"💡 登録済み商品が見つかりました：【{auto_cat}】{auto_name}")
            else:
                # ヒットしなかった場合：Webから商品名を取得（新規登録用）
                with st.spinner('新規商品として検索中...'):
                    auto_name = search_product_by_jan(scanned_jan)

    st.divider()

    def announce_next_date(qty, rate, threshold, label):
        if rate > 0:
            days_left = (qty - threshold) / rate
            next_date = datetime.now() + pd.Timedelta(days=max(0, days_left))
            date_str = next_date.strftime('%Y/%m/%d')
            st.success(f"✅ 「{label}」の登録が完了しました！")
            st.info(f"📅 次回の購入目安は **{date_str}** ごろです。")
        else:
            st.success(f"✅ 「{label}」の登録が完了しました！")

    if reg_mode == "新規分類・商品の登録":
        with st.form("registration_form", clear_on_submit=True):
            st.subheader("🆕 新規マスタ登録")
            sel_cat = st.selectbox("既存の分類から選択", ["（直接入力する）"] + cat_list)
            new_cat = st.text_input("分類を直接入力（新規の場合）")
            f_cat = new_cat if sel_cat == "（直接入力する）" else sel_cat
            
            f_jan = st.text_input("JANコード", value=scanned_jan)
            f_name = st.text_input("具体的な商品名", value=auto_name)
            f_cap = st.text_input("単位 (例: 本, パック)", value="個")
            
            c1, c2, c3 = st.columns(3)
            f_qty = c1.text_input("現在数", value="1.0")
            f_rate = c2.text_input("1日の消費", value="0.1")
            f_alert = c3.text_input("警告しきい値", value="1.0")
            
            if st.form_submit_button("新規マスタとして登録"):
                if not f_cat or not f_name:
                    st.error("分類と商品名は必須です")
                else:
                    qty_f, rate_f, alert_f = float(f_qty), float(f_rate), float(f_alert)
                    with engine.begin() as conn:
                        conn.execute(text("""
                            INSERT INTO items (group_id, category, name, jan_code, capacity, quantity, daily_rate, threshold, last_updated) 
                            VALUES (:gid, :cat, :name, :jan, :cap, :qty, :rate, :alert, :today)
                        """), {"gid": view_id, "cat": f_cat, "name": f_name, "jan": f_jan, "cap": f_cap, 
                               "qty": qty_f, "rate": rate_f, "alert": alert_f, "today": datetime.now().date()})
                    announce_next_date(qty_f, rate_f, alert_f, f_cat)

    else:
        st.subheader("➕ 在庫の加算")
        col_a, col_b = st.columns(2)
        
        # 1. 分類の自動選択・入力
        sel_cat_add = col_a.selectbox(
            "分類を選択", 
            ["（直接入力）"] + cat_list, 
            index=cat_list.index(auto_cat)+1 if auto_cat in cat_list else 0,
            key="cat_selector_add"
        )
        # スキャンでヒットした分類がある場合は、直接入力欄にも反映
        f_cat_manual = col_a.text_input("分類を直接入力", value=auto_cat if auto_cat and auto_cat not in cat_list else "")
        current_cat = sel_cat_add if sel_cat_add != "（直接入力）" else f_cat_manual

        # 2. 選択された分類に基づいて商品リストをフィルタ
        filtered_names = sorted(list(set([r[1] for r in existing_data if r[0] == current_cat and r[1]])))
        
        # 3. 商品名の自動選択・入力
        sel_name_add = col_b.selectbox(
            f"「{current_cat}」の商品名を選択", 
            ["（直接入力）"] + filtered_names,
            index=filtered_names.index(auto_name)+1 if auto_name in filtered_names else 0,
            key="name_selector_add"
        )
        f_name_manual = col_b.text_input("商品名を直接入力", value=auto_name if auto_name and auto_name not in filtered_names else "")

        with st.form("addition_form", clear_on_submit=True):
            # スキャンしたJANはここにも表示
            f_jan_add = st.text_input("JANコード", value=scanned_jan)
            f_add_qty = st.text_input("追加数", value="1.0")
            
            if st.form_submit_button("在庫を加算"):
                t_jan = f_jan_add
                t_cat = current_cat
                t_name = sel_name_add if sel_name_add != "（直接入力）" else f_name_manual
                add_val = float(f_add_qty)
                
                with engine.begin() as conn:
                    # JANが一致するか、分類/名前が一致する場合に加算
                    res = conn.execute(text("""
                        UPDATE items SET quantity = quantity + :add, last_updated = :today 
                        WHERE group_id = :gid AND (
                           (jan_code = :jan AND jan_code <> '') 
                           OR (category = :cat AND :cat <> '')
                           OR (name = :name AND :name <> '')
                        )
                    """), {"add": add_val, "today": datetime.now().date(), 
                           "jan": t_jan, "cat": t_cat, "name": t_name, "gid": view_id})
                
                if res.rowcount > 0:
                    with engine.connect() as conn:
                        new_data = conn.execute(text("""
                            SELECT SUM(quantity), SUM(daily_rate), MAX(threshold) 
                            FROM items WHERE category = :cat AND group_id = :gid
                        """), {"cat": t_cat, "gid": view_id}).fetchone()
                        if new_data:
                            announce_next_date(new_data[0], new_data[1], new_data[2], t_cat)
                else:
                    st.error("商品が特定できません。マスタに存在するか確認してください。")
                    
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
