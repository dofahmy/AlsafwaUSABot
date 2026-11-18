import os, json, requests, logging
TOKEN=os.getenv('CUSTOMER_BOT_TOKEN','')
LOG=logging.getLogger(__name__)

def call(method, payload=None, timeout=30):
    if not TOKEN: raise RuntimeError('CUSTOMER_BOT_TOKEN missing')
    r=requests.post(f'https://api.telegram.org/bot{TOKEN}/{method}',json=payload or {},timeout=timeout)
    if not r.ok:   # من غير ما التوكن يظهر في اللوج
        raise RuntimeError(f'Telegram {method}: {r.status_code} {r.text[:200]}')
    data=r.json()
    if not data.get('ok'): raise RuntimeError(str(data)[:250])
    return data['result']

def send(chat_id,text,keyboard=None):
    data={'chat_id':chat_id,'text':text,'disable_web_page_preview':True}
    if keyboard: data['reply_markup']={'inline_keyboard':keyboard}
    return call('sendMessage',data)

def send_photo(chat_id,image_bytes,caption='',keyboard=None):
    """صورة من البايتات نفسها (multipart)."""
    if not TOKEN: raise RuntimeError('CUSTOMER_BOT_TOKEN missing')
    data={'chat_id':str(chat_id),'caption':caption[:1024]}
    if keyboard: data['reply_markup']=json.dumps({'inline_keyboard':keyboard},ensure_ascii=False)
    r=requests.post(f'https://api.telegram.org/bot{TOKEN}/sendPhoto',data=data,
                    files={'photo':('premarket.png',image_bytes,'image/png')},timeout=60)
    if not r.ok:raise RuntimeError(f'Telegram sendPhoto: {r.status_code} {r.text[:200]}')
    out=r.json()
    if not out.get('ok'): raise RuntimeError(str(out)[:250])
    return out['result']

def send_document(chat_id,data_bytes,filename,caption=''):
    """ملف (زي CSV) من البايتات نفسها."""
    if not TOKEN: raise RuntimeError('CUSTOMER_BOT_TOKEN missing')
    r=requests.post(f'https://api.telegram.org/bot{TOKEN}/sendDocument',data={'chat_id':str(chat_id),'caption':caption[:1024]},
                    files={'document':(filename,data_bytes,'text/csv')},timeout=60)
    if not r.ok:raise RuntimeError(f'Telegram sendDocument: {r.status_code} {r.text[:200]}')
    out=r.json()
    if not out.get('ok'): raise RuntimeError(str(out)[:250])
    return out['result']
