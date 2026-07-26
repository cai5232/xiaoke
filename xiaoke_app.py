#!/usr/bin/env python3
"""xiaoke isolated local development server.
Stage 3A: mock OpenAI-compatible endpoint, no external upstream calls.
"""
import os
from xiaoke_gateway_api import create_app
app = create_app(os.environ.get('XIAOKE_DB_PATH', 'data/xiaoke.sqlite'))
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '3010')), debug=False)
