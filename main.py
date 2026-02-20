import os
import re
import sqlite3
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv
from flask import Flask, request, abort, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, ImageMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from pyngrok import ngrok

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN', '')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET', '')
NGROK_AUTH_TOKEN = os.environ.get('NGROK_AUTH_TOKEN', '')

app = Flask(__name__)

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
port = 8080

STATIC_DIR = os.path.join(os.getcwd(), 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

DB_PATH = 'money_tracker.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,
            memo TEXT NOT NULL,
            amount REAL NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    
    match = re.match(r'(รับ|จ่าย)\s+(.+?)\s+(\d+(\.\d+)?)', text)
    summary_match = re.match(r'สรุป', text)
    clear_match = re.match(r'ล้างข้อมูล', text)
    history_match = re.match(r'ประวัติ', text)
    
    reply_messages = []
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if match:
        action = match.group(1)
        memo = match.group(2)
        amount = float(match.group(3))
        t_type = 'income' if action == 'รับ' else 'expense'
        
        cursor.execute('INSERT INTO transactions (user_id, type, memo, amount) VALUES (?, ?, ?, ?)',
                       (user_id, t_type, memo, amount))
        conn.commit()
        
        reply_text = f"✅ บันทึก{action} '{memo}' จำนวน {amount:,.2f} บาท เรียบร้อยแล้วค่ะ"
        reply_messages.append(TextMessage(text=reply_text))
        
    elif history_match:
        cursor.execute("SELECT type, memo, amount, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5", (user_id,))
        rows = cursor.fetchall()
        if rows:
            history_text = "📜 ประวัติ 5 รายการล่าสุด:\n"
            for r in rows:
                action_th = "รับ" if r[0] == "income" else "จ่าย"
                history_text += f"- {action_th} {r[1]} {r[2]:,.2f} บาท ({r[3]})\n"
            reply_messages.append(TextMessage(text=history_text.strip()))
        else:
            reply_messages.append(TextMessage(text="ยังไม่มีประวัติการบันทึกข้อมูลค่ะ"))
            
    elif summary_match:
        cursor.execute("SELECT type, amount FROM transactions WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        
        total_income = sum(r[1] for r in rows if r[0] == 'income')
        total_expense = sum(r[1] for r in rows if r[0] == 'expense')
        balance = total_income - total_expense
        
        reply_text = f"📊 สรุปรายการ:\n🟢 รายรับรวม: {total_income:,.2f} บาท\n🔴 รายจ่ายรวม: {total_expense:,.2f} บาท\n\n💰 คงเหลือ: {balance:,.2f} บาท"
        reply_messages.append(TextMessage(text=reply_text))
        
        cursor.execute("SELECT memo, amount FROM transactions WHERE user_id = ? AND type = 'expense'", (user_id,))
        expense_rows = cursor.fetchall()
        
        if expense_rows:
            try:
                df = pd.DataFrame(expense_rows, columns=['memo', 'amount'])
                df_grouped = df.groupby('memo').sum().reset_index()
                
                fig = px.pie(df_grouped, values='amount', names='memo', title='สัดส่วนรายจ่าย')
                chart_filename = f"expense_chart_{user_id}.png"
                chart_filepath = os.path.join(STATIC_DIR, chart_filename)
                
                fig.write_image(chart_filepath)
                
                global public_url
                if 'public_url' in globals():
                    safe_url = public_url.replace("http://", "https://")
                    image_url = f"{safe_url}/static/{chart_filename}"
                    reply_messages.append(ImageMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url
                    ))
            except Exception as e:
                print(f"Error generating chart: {e}")
                
    elif clear_match:
        cursor.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
        conn.commit()
        reply_messages.append(TextMessage(text="🗑 ล้างข้อมูลเรียบร้อยแล้ว เริ่มบันทึกใหม่ได้เลยค่ะ"))
        
    else:
        reply_text = "กรุณาพิมพ์ในรูปแบบ:\n'รับ [รายการ] [จำนวนเงิน]'\n'จ่าย [รายการ] [จำนวนเงิน]'\n'สรุป' เพื่อดูยอดรวมและกราฟ\n'ประวัติ' เพื่อดูรายการล่าสุด\n'ล้างข้อมูล' เพื่อล้างข้อมูลปัจจุบัน"
        reply_messages.append(TextMessage(text=reply_text))
        
    conn.close()
    
    if not reply_messages:
        return
        
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=reply_messages
        ))

if __name__ == "__main__":
    if NGROK_AUTH_TOKEN:
        ngrok.set_auth_token(NGROK_AUTH_TOKEN)
    public_url = ngrok.connect(port).public_url
    print(f"Copy this URL and insert it into LINE Webhook.: {public_url}/callback")

    app.run(port=port)
