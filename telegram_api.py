import os, requests, logging
TOKEN=os.getenv('CUSTOMER_BOT_TOKEN','')
LOG=logging.getLogger(__name__)

def call(method, payload=None, timeout=30):
    if not TOKEN: raise RuntimeError('CUSTOMER_BOT_TOKEN missing')
    r=requests.post(f'https://api.telegram.org/bot{TOKEN}/{method}',json=payload or {},timeout=timeout)
    r.raise_for_status()
    data=r.json()
    if not data.get('ok'): raise RuntimeError(str(data)[:250])
    return data['result']

def send(chat_id,text,keyboard=None):
    data={'chat_id':chat_id,'text':text,'disable_web_page_preview':True}
    if keyboard: data['reply_markup']={'inline_keyboard':keyboard}
    return call('sendMessage',data)
