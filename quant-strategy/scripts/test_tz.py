import pytz
from datetime import datetime
utc_now = datetime.utcnow().replace(tzinfo=pytz.utc)
et = utc_now.astimezone(pytz.timezone('US/Eastern'))
cn = utc_now.astimezone(pytz.timezone('Asia/Shanghai'))
print("ET:", et.strftime("%Y-%m-%d %H:%M:%S"))
print("CN:", cn.strftime("%Y-%m-%d %H:%M:%S"))
