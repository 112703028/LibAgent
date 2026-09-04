import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from library_agent.db.models import Citation
from library_agent.db.session import SessionLocal
from library_agent.agents import validator as V

N = 30  # 固定一批書（別太大，避免燒 Google 配額）
with SessionLocal() as s:
    rows = s.scalars(select(Citation).limit(N)).all()

def bench(workers: int):
    t0 = time.perf_counter()
    errs = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # 空 cache + 空 done_ids → 每筆都真的打 API；B1 之後不寫 DB，可重複跑
        futs = [ex.submit(V._process_one, c, {}, set()) for c in rows]
        for f in as_completed(futs):
            _, error, _ = f.result()
            if error:
                errs += 1
    return time.perf_counter() - t0, errs

print(f"workload = {len(rows)} 筆\n")

for w in (1, 2, 4, 8, 12):
    dt, errs = bench(w)
    print(f"workers={w:>2}  {dt:6.1f}s  {len(rows)/dt:5.1f} 筆/秒  錯誤={errs}")