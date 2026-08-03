#!/usr/bin/env python3
"""xiaoke isolated local development server.
Stage 3A: mock OpenAI-compatible endpoint, no external upstream calls.
"""
import os
import threading
import time
from xiaoke_gateway_api import create_app

app = create_app(os.environ.get('XIAOKE_DB_PATH', 'data/xiaoke.sqlite'))

def _cron_loop():
    """Background thread: run cron.py logic every 3 hours."""
    # 启动后等15分钟再跑第一次，避免服务刚起来就占资源
    time.sleep(15 * 60)
    while True:
        try:
            from cron import main as cron_main
            cron_main()
        except Exception as e:
            print(f'[cron-thread] error: {e}')
        time.sleep(3 * 60 * 60)

# 只在非reload进程里启动（避免gunicorn --reload双启）
if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    _t = threading.Thread(target=_cron_loop, daemon=True)
    _t.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '3010')), debug=False)
