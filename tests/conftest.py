import os

# 讓 import 時會建構 OpenAI client 的模組（parser/discoverer/recommender）不會因缺 key 而爆。
# 這些測試不會真的呼叫任何外部 API。
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")
