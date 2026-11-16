"""Copy this module next to nokhba_live2.py, which will call emit on each trading event."""
import os,requests,logging
LOG=logging.getLogger('rajih.bridge')

def emit(tr,kind,message):
    url=os.getenv('RAJIH_BRIDGE_URL','').rstrip('/')
    key=os.getenv('RAJIH_BRIDGE_KEY','')
    if not url or not key:return False
    signal_id=f"{tr['date']}:{tr['ticker']}:{tr['time_ny']}"
    event_key=signal_id+':'+kind
    try:
        r=requests.post(url+'/events',json={'event_key':event_key,'signal_id':signal_id,'symbol':tr['ticker'],
                                            'kind':kind,'message':message},headers={'X-Bridge-Key':key},timeout=8)
        r.raise_for_status();return True
    except Exception as exc:
        LOG.error('Bridge event %s not sent: %s',event_key,exc)
        return False
